"""MapGIS 6.x system-library records, with stable IDs and opaque preservation.

The installed SymEdit6x reader/writer defines these packed little-endian tables.
Existing primitive streams and all line variants are copied byte for byte. New
records use native polyline/polygon, circle/arc and simple dash forms. Fonts and
unknown parameter forms remain opaque. The installed libraries are never edited.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import struct

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,sha


FILES={'symbol':'subgraph.lib','line':'linesty.lib','pattern':'fillgrph.lib'}
_CURVES={3:'arc',4:'circle_filled',5:'circle_filled_outline',6:'circle'}
_TYPES={'polygon':0,'polygon_outline':1,'polyline':2}|{name:kind for kind,name in _CURVES.items()}


def _colors(blob:bytes)->list[bytes]:
    # Installed MAPGISLib::_OpenColorLib: 124-byte PCOLOR 3 header,
    # 32-byte records, active unsigned-short count at +8. Trailing records,
    # spot channels and reserved bytes stay opaque and are never rebuilt.
    if len(blob)<124 or blob[:8]!=b'PCOLOR 3':raise BridgeError('LIBRARY_COLOR_FORMAT')
    count=struct.unpack_from('<H',blob,8)[0]
    if not 1<=count<=16384 or len(blob)<124+32*count:raise BridgeError('LIBRARY_COLOR_TRUNCATED')
    return [blob[124+32*i:156+32*i] for i in range(count)]


def _glyph(blob:bytes,start:int)->bytes:
    if start<0 or start+4>len(blob):raise BridgeError('LIBRARY_OFFSET')
    count,floats=struct.unpack_from('<hh',blob,start)
    if count<0 or floats<0 or start+4+10*count>len(blob):raise BridgeError('LIBRARY_PRIMITIVE_COUNT')
    parts=0
    for i in range(count):
        n=struct.unpack_from('<h',blob,start+4+i*10+8)[0]
        if n<0:raise BridgeError('LIBRARY_PART_COUNT')
        parts+=n
    end=start+4+count*10+parts*2+floats*4
    if end>len(blob):raise BridgeError('LIBRARY_TRUNCATED')
    if parts and any(x<0 for x in struct.unpack_from('<'+'h'*parts,blob,start+4+count*10)):
        raise BridgeError('LIBRARY_VERTEX_COUNT')
    values=struct.unpack_from('<'+'f'*floats,blob,end-floats*4)
    if any(not math.isfinite(x) for x in values):raise BridgeError('LIBRARY_NONFINITE')
    return blob[start:end]


def _read(blob:bytes,kind:str)->list:
    if len(blob)<12:raise BridgeError('LIBRARY_TRUNCATED')
    count=struct.unpack_from('<h',blob)[0]
    stride=4 if kind=='symbol' else 6
    if count<1 or count>32767 or stride*count>len(blob):raise BridgeError('LIBRARY_INDEX_COUNT')
    records=[]
    for identity in range(1,count):
        pos=struct.unpack_from('<i',blob,identity*stride)[0]
        if pos<stride*count:raise BridgeError('LIBRARY_OFFSET')
        if kind=='symbol':records.append(_glyph(blob,pos));continue
        variants=struct.unpack_from('<h',blob,identity*stride+4)[0]
        if variants<1:raise BridgeError('LIBRARY_VARIANTS')
        if kind=='pattern':
            if variants!=1:raise BridgeError('LIBRARY_PATTERN_VARIANTS_UNSUPPORTED')
            records.append(_glyph(blob,pos));continue
        if pos+variants*6>len(blob):raise BridgeError('LIBRARY_LINE_INDEX')
        entries=[]
        for v in range(variants):
            offset,length=struct.unpack_from('<ih',blob,pos+v*6)
            if offset<stride*count or length<2 or offset+length>len(blob):raise BridgeError('LIBRARY_LINE_PAYLOAD')
            part=blob[offset:offset+length];n=struct.unpack_from('<h',part)[0]
            if n<0 or 2+6*n>len(part) or (len(part)-2-6*n)%4:raise BridgeError('LIBRARY_LINE_PAYLOAD')
            floats=(len(part)-2-6*n)//4
            values=struct.unpack_from('<'+'f'*floats,part,2+6*n)
            if any(not math.isfinite(x) for x in values):raise BridgeError('LIBRARY_NONFINITE')
            entries.append(part)
        records.append(entries)
    return records


def _write(records:list,kind:str,reserved:bytes)->bytes:
    count=len(records)+1;stride=4 if kind=='symbol' else 6
    if count>32767:raise BridgeError('LIBRARY_FULL')
    header=bytearray(stride*(count+10));header[2:stride]=reserved[2:stride]
    struct.pack_into('<h',header,0,count);cursor=len(header);body=bytearray()
    for identity,part in enumerate(records,1):
        struct.pack_into('<i',header,identity*stride,cursor)
        if kind=='line':
            struct.pack_into('<h',header,identity*stride+4,len(part))
            index=bytearray(6*len(part));cursor+=len(index)
            for v,payload in enumerate(part):
                struct.pack_into('<ih',index,6*v,cursor,len(payload));cursor+=len(payload)
            body+=index+b''.join(part)
        else:
            if kind=='pattern':struct.pack_into('<h',header,identity*stride+4,1)
            body+=part;cursor+=len(part)
    return bytes(header+body)


def _decode_glyph(payload:bytes)->list[dict]:
    count,floats=struct.unpack_from('<hh',payload);nparts=sum(struct.unpack_from('<h',payload,4+i*10+8)[0] for i in range(count))
    sizes=list(struct.unpack_from('<'+'h'*nparts,payload,4+10*count))
    values=list(struct.unpack_from('<'+'f'*floats,payload,len(payload)-4*floats))
    answer=[];vertex=0;part_index=0
    for i in range(count):
        kind,fill,color,width,n=struct.unpack_from('<5h',payload,4+10*i)
        lengths=sizes[part_index:part_index+n];part_index+=n
        record={'native_type':kind,'fill_color':fill,'color':color,'width_index':width,'part_lengths':lengths}
        if kind in (0,1,2):
            parts=[]
            for length in lengths:
                if vertex+length*2>len(values):raise BridgeError('LIBRARY_VERTEX_STREAM')
                parts.append([values[j:j+2] for j in range(vertex,vertex+length*2,2)]);vertex+=length*2
            record.update(type={0:'polygon',1:'polygon_outline',2:'polyline'}[kind],parts=parts)
        else:
            # Native circle/arc primitives have parameter streams rather than
            # a polyline. Preserve and report their parameters without changing
            # their meaning to vertices.
            length=2*sum(lengths)
            if vertex+length>len(values):raise BridgeError('LIBRARY_PARAMETER_STREAM')
            parameters=values[vertex:vertex+length];vertex+=length
            record.update(type='native_parameters',values=parameters)
            # SymEdit6x::sub_10005C90 converts types3..6 into exact PNT_INFO
            # curves. Three pairs store cx,cy,r,start_deg,end_deg,reserved;
            # circles ignore the last three floats. Keep that stream visible.
            if kind in _CURVES and lengths==[3] and len(parameters)==6 and parameters[2]>0:
                if kind!=3 or 0<=parameters[3]<parameters[4]<=360:
                    record.update(type=_CURVES[kind],center=parameters[:2],radius=parameters[2])
                    if kind==3:record.update(start_angle=parameters[3],end_angle=parameters[4])
        answer.append(record)
    return answer


def system_library_info(folder:str,expected_manifest_sha256:str|None=None,kind:str|None=None,ids:list[int]|None=None)->dict:
    root=Path(folder).resolve(strict=True)
    if not root.is_dir():raise BridgeError('LIBRARY_FOLDER')
    entries=[];paths={};data={};total=0
    for path in sorted(root.iterdir(),key=lambda p:p.name.casefold()):
        if path.suffix.casefold()!='.lib':continue
        if path.is_symlink() or not path.is_file() or path.resolve().parent!=root:raise BridgeError('LIBRARY_MEMBER_TYPE')
        if path.name.casefold() in paths:raise BridgeError('LIBRARY_NAME_COLLISION')
        total+=path.stat().st_size
        if total>128*1024*1024:raise BridgeError('LIBRARY_SIZE')
        blob=path.read_bytes();name=path.name.casefold();paths[name]=path;data[name]=blob
        entries.append({'name':path.name,'sha256':sha(blob),'bytes':len(blob)})
    if not (set(FILES.values())|{'colorlib.lib'}) <= paths.keys():raise BridgeError('LIBRARY_REQUIRED_MEMBERS')
    fingerprint=sha(json_text(entries).encode('utf8'))
    if expected_manifest_sha256 is not None and expected_manifest_sha256!=fingerprint:raise BridgeError('STALE_LIBRARY')
    records={key:_read(data[name],key) for key,name in FILES.items()}
    result={'folder':str(root),'manifest_sha256':fingerprint,'members':entries,
            'counts':{key:len(value) for key,value in records.items()},
            'scope':'saved system-library folder; font and opaque ancillary records are preserved'}
    colors=_colors(data['pcolor.lib']) if 'pcolor.lib' in data else None
    if colors is not None:result['color_count']=len(colors)
    if kind is not None or ids is not None:
        if kind not in (*FILES,'color') or not isinstance(ids,list) or not 1<=len(ids)<=100 or any(type(i) is not int for i in ids) or len(set(ids))!=len(ids):raise BridgeError('LIBRARY_QUERY')
        if kind=='color':
            if colors is None:raise BridgeError('LIBRARY_COLOR_MISSING')
            if any(not 0<=i<len(colors) for i in ids):raise BridgeError('LIBRARY_COLOR_ID')
            result['records']=[{'id':i,'kcmy':list(colors[i][:4]),'sha256':sha(colors[i]),'opaque_tail_hex':colors[i][4:].hex()} for i in ids]
            return result
        answer=[]
        for identity in ids:
            if type(identity) is not int or not 1<=identity<=len(records[kind]):raise BridgeError('LIBRARY_ID')
            value=records[kind][identity-1]
            if kind=='line':answer.append({'id':identity,'variants':[{'sha256':sha(p),'native_payload_hex':p.hex()} for p in value]})
            else:answer.append({'id':identity,'sha256':sha(value),'primitives':_decode_glyph(value)})
        result['records']=answer
    return result


def _primitive_payload(primitives,*,library_kind:str='symbol')->bytes:
    if not isinstance(primitives,list) or not 1<=len(primitives)<=2048:raise BridgeError('LIBRARY_PRIMITIVES')
    descriptors=[];sizes=[];values=[]
    for primitive in primitives:
        if not isinstance(primitive,dict) or primitive.get('type') not in _TYPES:
            raise BridgeError('LIBRARY_PRIMITIVE_TYPE')
        kind=_TYPES[primitive['type']]
        fields={'center','radius'}|({'start_angle','end_angle'} if kind==3 else set()) if kind in _CURVES else {'parts'}
        has_fill=kind not in _CURVES or (library_kind=='pattern' and kind in (4,5))
        style_fields={'type','color','width_index'}|({'fill_color'} if has_fill else set())
        if set(primitive)-(style_fields|fields):raise BridgeError('LIBRARY_PRIMITIVE_TYPE')
        color=primitive.get('color',1);fill=primitive.get('fill_color',0);width=primitive.get('width_index',1)
        if any(type(v) is not int or not 0<=v<=32767 for v in (color,fill)) or type(width) is not int or not 0<=width<=15:
            raise BridgeError('LIBRARY_GRAPHIC_PARAMETERS')
        if kind in _CURVES:
            # Original KernelDriver::_DispSubgr uses descriptor word2 for BOTH
            # circle fill and outline, unlike SymEdit's word1 fill display.
            # Pattern::build_pattern instead uses word1 for fill, word2 for
            # outline. Expose separate fill_color there. In a symbol, compose
            # filled + outline primitives for different colors; never accept
            # a silently ignored fill_color.
            center=primitive.get('center');radius=primitive.get('radius')
            if not isinstance(center,list) or len(center)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e6 for v in center):raise BridgeError('LIBRARY_COORDINATES')
            if library_kind=='pattern' and any(not 0<=v<=1 for v in center):raise BridgeError('LIBRARY_PATTERN_CENTER: native pattern centers must be0..1')
            if type(radius) not in (int,float) or not math.isfinite(radius) or not 0<radius<=1e6:raise BridgeError('LIBRARY_RADIUS')
            start,end=(primitive.get('start_angle'),primitive.get('end_angle')) if kind==3 else (0.,0.)
            if kind==3 and (any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or not 0<=start<end<=360):raise BridgeError('LIBRARY_ARC_ANGLES')
            parameters=struct.unpack('<6f',struct.pack('<6f',*center,radius,start,end,0.))
            if parameters[2]<=0 or (kind==3 and not parameters[3]<parameters[4]):raise BridgeError('LIBRARY_CURVE_FLOAT32')
            descriptors.append(struct.pack('<5h',kind,fill,color,width,1));sizes.append(3);values.extend(parameters)
            continue
        parts=primitive.get('parts')
        if not isinstance(parts,list) or not 1<=len(parts)<=32767:raise BridgeError('LIBRARY_PARTS')
        descriptors.append(struct.pack('<5h',kind,fill,color,width,len(parts)))
        for part in parts:
            if not isinstance(part,list) or not 2<=len(part)<=16383 or (kind in (0,1) and (len(part)<4 or part[0]!=part[-1])):
                raise BridgeError('LIBRARY_VERTICES: polygons require closed rings')
            sizes.append(len(part))
            for point in part:
                if not isinstance(point,list) or len(point)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e6 for v in point):
                    raise BridgeError('LIBRARY_COORDINATES')
                values+=point
    if len(values)>32767:raise BridgeError('LIBRARY_VERTEX_STREAM_TOO_LARGE')
    return (struct.pack('<hh',len(primitives),len(values))+b''.join(descriptors)
            +struct.pack('<'+'h'*len(sizes),*sizes)+struct.pack('<'+'f'*len(values),*values))


def prepare_system_library(bridge:Bridge,folder:str,expected_manifest_sha256:str,changes:list[dict],request_id:str)->dict:
    info=system_library_info(folder,expected_manifest_sha256)
    if not isinstance(changes,list) or not 1<=len(changes)<=1000:raise BridgeError('LIBRARY_CHANGES')
    root=Path(info['folder']);original={p.name.casefold():p.read_bytes() for p in root.iterdir() if p.suffix.casefold()=='.lib'}
    if any(sha(original[m['name'].casefold()])!=m['sha256'] for m in info['members']):raise BridgeError('STALE_LIBRARY')
    records={key:_read(original[name],key) for key,name in FILES.items()};updated={key:list(value) for key,value in records.items()}
    edits=[];replaced={key:set() for key in FILES};palette=None
    for change in changes:
        if isinstance(change,dict) and change.get('kind')=='color':
            action=change.get('action');allowed={'kind','action','kcmy'}|({'id'} if action=='replace' else set())
            if action not in {'replace','append'} or set(change)!=allowed:raise BridgeError('LIBRARY_COLOR_CHANGE')
            if palette is None:
                if 'pcolor.lib' not in original:raise BridgeError('LIBRARY_COLOR_MISSING')
                palette=bytearray(original['pcolor.lib'])
            colors=_colors(palette);components=change['kcmy']
            if not isinstance(components,list) or len(components)!=4 or any(type(v) is not int or not 0<=v<=255 for v in components):raise BridgeError('LIBRARY_COLOR_COMPONENTS')
            if action=='append':
                # Original _OpenColorLib reads count@8 records from offset124,
                # capped at0x4000. _UpdateAColor initializes the32-byte ACOLOR
                # to zero before setting KCMY; absent spot channels remain zero.
                # Insert before any opaque trailing records, without reindexing.
                identity=len(colors)
                if identity>=0x4000:raise BridgeError('LIBRARY_COLOR_FULL')
                offset=124+32*identity;palette[offset:offset]=bytes(components)+bytes(28)
                struct.pack_into('<H',palette,8,identity+1)
            else:
                identity=change['id']
                if type(identity) is not int or not 1<=identity<len(colors):raise BridgeError('LIBRARY_COLOR_ID')
                palette[124+32*identity:128+32*identity]=bytes(components)
            edits.append({'kind':'color','action':action,'id':identity,'kcmy':components})
            continue
        if not isinstance(change,dict) or change.get('kind') not in FILES or change.get('action') not in ('append','replace'):
            raise BridgeError('LIBRARY_CHANGE: append or replace with stable IDs required')
        kind=change['kind'];action=change['action'];allowed={'kind','action','id','dash_fraction' if kind=='line' else 'primitives'}
        if set(change)-allowed or (action=='append' and 'id' in change):raise BridgeError('LIBRARY_CHANGE_FIELDS')
        identity=len(updated[kind])+1 if action=='append' else change.get('id')
        if type(identity) is not int or not 1<=identity<=len(updated[kind])+(action=='append'):raise BridgeError('LIBRARY_ID')
        if kind=='line':
            fraction=change.get('dash_fraction')
            if type(fraction) not in (int,float) or not math.isfinite(fraction) or not 0<fraction<=1:raise BridgeError('LIBRARY_DASH_FRACTION')
            value=[struct.pack('<4h4f',1,5 if fraction==1 else 0,2,0,0,0,fraction,0)]
        else:value=_primitive_payload(change.get('primitives'),library_kind=kind)
        if action=='append':updated[kind].append(value)
        else:updated[kind][identity-1]=value;replaced[kind].add(identity)
        edits.append({'kind':kind,'action':action,'id':identity})
    sources=[{'file':str(root/m['name']),'sha256':m['sha256']} for m in info['members']]
    payload={'operation':'native.system_library.edit','folder':str(root),'expected_manifest_sha256':expected_manifest_sha256,'changes':changes}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,'library.json')
    if not new:return receipt
    try:
        files=stage/'files';files.mkdir()
        for member in info['members']:(files/member['name']).write_bytes(original[member['name'].casefold()])
        if palette is not None:
            target=next(p for p in files.iterdir() if p.name.casefold()=='pcolor.lib')
            target.write_bytes(palette)
            if target.read_bytes()!=palette:raise BridgeError('LIBRARY_COLOR_READBACK')
        for kind,name in FILES.items():
            if not any(edit['kind']==kind for edit in edits):continue
            target=next(p for p in files.iterdir() if p.name.casefold()==name)
            target.write_bytes(_write(updated[kind],kind,original[name]))
            actual=_read(target.read_bytes(),kind)
            if actual!=updated[kind] or any(actual[i-1]!=raw for i,raw in enumerate(records[kind],1) if i not in replaced[kind]):
                raise BridgeError('LIBRARY_PAYLOAD_READBACK')
        final=system_library_info(str(files))
        metadata={'manifest_sha256':final['manifest_sha256'],'counts':final['counts'],'color_count':final.get('color_count'),'changes':edits,
                  'existing_payloads_preserved':True,'source_preserved':True,
                  'scope':'saved library branch; use system_library binding for isolated native plots; installed settings unchanged'}
        (files/'library.json').write_text(json_text(metadata),encoding='utf8')
        return finish_bundle(bridge,request_id,files,metadata)
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise


def freeze_system_library(binding:dict|None):
    if binding is None:return None,[]
    if not isinstance(binding,dict) or set(binding)!={'folder','expected_manifest_sha256'}:raise BridgeError('LIBRARY_BINDING')
    info=system_library_info(binding['folder'],binding['expected_manifest_sha256'])
    return info,[{'file':str(Path(info['folder'])/m['name']),'sha256':m['sha256'],'role':'system_library'} for m in info['members']]


def snapshot_system_library(info:dict,stage:Path)->Path:
    folder=stage/'system-library';folder.mkdir()
    for member in info['members']:shutil.copyfile(Path(info['folder'])/member['name'],folder/member['name'])
    system_library_info(str(folder),info['manifest_sha256'])
    return folder
