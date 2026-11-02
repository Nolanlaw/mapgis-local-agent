"""Copy explicit native selections into a loaded native target, as a new file.

The installed SDK copies geometry and supplies physical output identities. Its
known attribute-reader defect is handled by the same byte-preserving repair as
native subset saving. No input is written, deduplicated or snapped.
"""
from __future__ import annotations
import io,json,os,re,shutil,struct,uuid
from copy import deepcopy
from pathlib import Path
import psutil
from pymapgis import reader as native
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import BridgeError,read_native,native_ids,annotation_text,sha,json_text
from .creation import _replace_sections
from .comparison import _extract_attributes,_extract_style,_diff_dicts
from .native_backend import backend_configuration,_invoke,_validate,completed_native_child,KINDS
from .native_com import private_com_runtime,invoke_com,_table,_wp_copy_mapping
from .raster import file_sha
from .topology import _wp_arc_records,_wp_nodes,validate_wp_topology


def _schema(blob,reader,sections,index=None):
    index,start,data,count,width=_table(blob,reader,sections,index)
    header=bytearray(blob[start:data]);struct.pack_into('<i',header,324,0)
    return bytes(header),count,width,data,index


def _same_schema(left,right):
    # The prefix also stores dates and previous filenames. Field descriptors,
    # padding, count-normalized layout and row width define the raw schema.
    return left[0][322:]==right[0][322:] and left[2]==right[2]


def _envelope(blob):
    if len(blob)<817 or blob[:8] not in (b'WMAP`D21',b'WMAP`D22',b'WMAP`D23'):raise BridgeError('APPEND_NATIVE_TYPE')
    offset=struct.unpack_from('<i',blob,12)[0]
    if offset<16 or offset+160>len(blob):raise BridgeError('APPEND_NATIVE_SECTION_TABLE')
    sections=native._read_header_entries(io.BytesIO(blob),offset,16)
    if any(start<0 or size<0 or start+size>len(blob) for start,size in sections):raise BridgeError('APPEND_NATIVE_SECTION_RANGE')
    return sections


def _primary(source,target,candidate,mapping):
    a,ar,sa=read_native(target);b,br,sb=read_native(source);current=candidate.read_bytes();entries=_envelope(current)
    header,count,width,start,index=_schema(a,ar,sa);peer,number,length,values,_=_schema(b,br,sb)
    if not _same_schema((header,count,width),(peer,number,length)):raise BridgeError('APPEND_SCHEMA: native primary schemas differ')
    section,slot_width={'POINT':(0,93),'LINE':(0,57),'POLYGON':(8,40)}[ar.shapeType]
    slots=entries[section][1]//slot_width
    if entries[section][1]%slot_width or count>slots:raise BridgeError('APPEND_NATIVE_SLOT_LAYOUT')
    old=set(native_ids(ar));new=[v['output_record_id'] for v in mapping]
    if len(set(new))!=len(new) or old.intersection(new) or any(not 1<=v<slots for v in new):raise BridgeError('APPEND_OUTPUT_IDENTITY')
    rows=bytearray(slots*width);rows[:count*width]=a[start:start+count*width]
    for pair in mapping:
        oid,nid=pair['source_record_id'],pair['output_record_id']
        if not 1<=oid<number:raise BridgeError('APPEND_ATTRIBUTE_IDENTITY')
        rows[nid*width:(nid+1)*width]=b[values+oid*width:values+(oid+1)*width]
    header=bytearray(header);struct.pack_into('<i',header,324,slots)
    repaired=bytearray(_replace_sections(current,entries,{index:bytes(header)+rows}));repaired[91:260]=a[91:260]
    candidate.write_bytes(repaired)


def _node_count(blob,sections,arcs):
    if not arcs:return 0
    start=min(struct.unpack_from('<i',blob,sections[0][0]+i*57+14)[0] for i in arcs)
    prefix=blob[sections[1][0]:sections[1][0]+start]
    if len(prefix)%4!=2 or prefix[:2]!=b'\0\0' or any(struct.unpack_from('<2H',prefix,i)!=(0,(i-2)//4+1) for i in range(2,len(prefix),4)):
        raise BridgeError('APPEND_NODE_PREFIX_LAYOUT')
    return (len(prefix)-2)//4


def _wp_inputs(source,target,record_ids):
    a,ar,sa,old=_wp_arc_records(target);b,br,sb,incoming=_wp_arc_records(source)
    # Native deletion can retain physical walks and unowned arcs. Keep their
    # target identities; only the explicitly selected source owners are copied.
    for blob,reader,sections,arcs in [(a,ar,sa,old),(b,br,sb,incoming)]:
        validate_wp_topology(target if blob is a else source)
        for index,(offset,size) in enumerate(sections):
            if index!=native._ATTR_HEADER_INDEX['POLYGON'] and size>=348 and blob[offset:offset+2]==blob[offset+320:offset+322]==b'\x60\x44':
                fields,count,_=struct.unpack_from('<hih',blob,offset+322)
                if fields>0 and count>1 and index not in {2,5}:raise BridgeError('APPEND_AUX_SCHEMA: unknown nonempty attribute section')
    for index in (2,5):
        left,right=_aux_schema(a,ar,sa,index),_aux_schema(b,br,sb,index)
        if (left is None)!=(right is None) or (left and not _same_schema(left,right)):
            raise BridgeError('APPEND_AUX_SCHEMA: both native auxiliary schemas must agree')
    arc_offset=sa[0][1]//57-1;region_offset=sa[8][1]//40-1;node_offset=_node_count(a,sa,old)
    incoming={i:arc for i,arc in incoming.items() if set(record_ids).intersection(arc['owner_region_ids'])}
    old_nodes=_wp_nodes({i:arc for i,arc in old.items() if arc['node_ids']!=[0,0]})
    incoming_nodes=_wp_nodes({i:arc for i,arc in incoming.items() if arc['node_ids']!=[0,0]})
    virtual=deepcopy(old)
    for identity,arc in incoming.items():
        copied=deepcopy(arc);copied['arc_id']=identity+arc_offset
        copied['owner_region_ids']=[v+region_offset for v in arc['owner_region_ids']]
        copied['node_ids']=[v+node_offset if v else 0 for v in arc['node_ids']]
        virtual[identity+arc_offset]=copied
    nodes={v:v for v in old_nodes}
    nodes.update({v+node_offset:node_offset+i for i,v in enumerate(sorted(incoming_nodes),1)})
    if node_offset+len(incoming_nodes)>65535:raise BridgeError('APPEND_NODE_TABLE_LIMIT')
    return virtual,nodes,node_offset+len(incoming_nodes),arc_offset,region_offset,node_offset


def _aux_schema(blob,reader,sections,index):
    start,size=sections[index]
    if size<348 or blob[start:start+2]!=blob[start+320:start+322] or blob[start:start+2]!=b'\x60\x44':return None
    fields=struct.unpack_from('<h',blob,start+322)[0]
    if not fields:return None
    schema=_schema(blob,reader,sections,index);_,count,width,data,_=schema
    # BAS60 materializes its empty built-in ID/length tables when saving a
    # writer file with metadata-only blocks. They have no user payload. Only
    # the exact built-in layout with every payload byte zero qualifies.
    descriptor=native._FIELD_DESCRIPTOR_STRUCT
    layout=[tuple([name.split(b'\0',1)[0],kind,offset,length]) for name,kind,offset,length,_ in
            (descriptor.unpack_from(blob,start+348+i*descriptor.size) for i in range(fields))]
    default=([(b'ID',3,1,8),('长度'.encode('gbk'),5,5,15)] if index==2 else [(b'ID',3,1,8)])
    if layout==default and width==(13 if index==2 else 5) and all(not any(blob[data+i*width+1:data+(i+1)*width]) for i in range(count)):return None
    return schema


def _wp_attributes(source,target,candidate,mapping,*,restore=False):
    virtual,nodes,node_count,arc_offset,region_offset,node_offset=_wp_inputs(source,target,[v['source_record_id'] for v in mapping])
    a,ar,sa=read_native(target);b,br,sb=read_native(source)
    regions=[{'source_record_id':i,'output_record_id':i} for i in native_ids(ar)]+[
        {'source_record_id':v['source_record_id']+region_offset,'output_record_id':v['output_record_id']} for v in mapping]
    proof=_wp_copy_mapping(virtual,candidate,regions,restore_nodes=restore,node_ids=nodes,node_count=node_count,include_unowned=True)
    current,reader,sections=read_native(candidate);replacements={};tables={}
    for index,key,offset in [(2,'arc_mapping',arc_offset),(5,'node_mapping',node_offset)]:
        pairs=[]
        for pair in proof[key]:
            identity=pair['source_record_id'];role='target' if identity<=offset else 'source'
            if role=='target' and pair['output_record_id']!=identity:raise BridgeError('APPEND_TARGET_TOPOLOGY_ID_CHANGED')
            pairs.append({'source_role':role,'source_record_id':identity if role=='target' else identity-offset,'output_record_id':pair['output_record_id']})
        proof[key]=pairs;left=_aux_schema(a,ar,sa,index);right=_aux_schema(b,br,sb,index)
        if (left is None)!=(right is None) or (left and not _same_schema(left,right)):
            raise BridgeError('APPEND_AUX_SCHEMA: both native auxiliary schemas must agree')
        if left is None:
            tables[key.removesuffix('_mapping')]={'section':index,'layout':'no user attribute fields'};continue
        header,count,width,start,_=left;_,number,_,values,_=right
        maximum=(sections[0][1]//57-1) if index==2 else node_count
        if count>maximum+1:raise BridgeError('APPEND_AUXILIARY_SLOT_LAYOUT')
        rows=bytearray((maximum+1)*width);rows[:count*width]=a[start:start+count*width]
        for pair in pairs:
            oid,nid=pair['source_record_id'],pair['output_record_id'];blob,data,limit=(a,start,count) if pair['source_role']=='target' else (b,values,number)
            if not 1<=oid<limit:raise BridgeError('APPEND_AUXILIARY_ATTRIBUTE_IDENTITY')
            rows[nid*width:(nid+1)*width]=blob[data+oid*width:data+(oid+1)*width]
        header=bytearray(header);struct.pack_into('<i',header,324,maximum+1);expected=bytes(header)+rows
        position,size=sections[index]
        if restore:replacements[index]=expected
        elif current[position:position+size]!=expected:raise BridgeError('APPEND_AUXILIARY_ATTRIBUTE_BYTES_CHANGED')
        tables[key.removesuffix('_mapping')]={'section':index,'rows':maximum,'record_width':width}
    if restore:candidate.write_bytes(_replace_sections(current,sections,replacements))
    return {**proof,'tables':tables,'attribute_records_byte_identical':True}


def _check(source,target,candidate,mapping):
    c,cr,sc=read_native(candidate);actual=native_ids(cr);target_ids=native_ids(read_native(target)[1])
    pairs=[('target',i,i) for i in target_ids]+[('source',v['source_record_id'],v['output_record_id']) for v in mapping]
    if actual!=sorted(new for _,_,new in pairs):raise BridgeError('APPEND_REOPEN_IDENTITY')
    output_rows={v:i for i,v in enumerate(actual)};out_schema,_,width,start,_=_schema(c,cr,sc)
    for role,file in [('target',target),('source',source)]:
        a,ar,sa=read_native(file);source_rows={v:i for i,v in enumerate(native_ids(ar))};header,_,length,data,_=_schema(a,ar,sa)
        if ar.shapeType!=cr.shapeType or a[91:260]!=c[91:260] or not _same_schema((header,0,length),(out_schema,0,width)):raise BridgeError('APPEND_SAVED_METADATA_CHANGED')
        for name,old,new in pairs:
            if name!=role:continue
            i,j=source_rows[old],output_rows[new]
            if not ar.geom[i].equals(cr.geom[j]):raise BridgeError('APPEND_GEOMETRY_CHANGED')
            if a[data+old*width:data+(old+1)*width]!=c[start+new*width:start+(new+1)*width]:raise BridgeError('APPEND_PRIMARY_ATTRIBUTE_BYTES_CHANGED')
            if _diff_dicts(_extract_attributes(ar,i),_extract_attributes(cr,j)):raise BridgeError('APPEND_ATTRIBUTES_CHANGED')
            differences=_diff_dicts(_extract_style(ar,i),_extract_style(cr,j))
            if ar.shapeType=='POLYGON':differences={k:v for k,v in differences.items() if k not in {'field5','field6','field7'}}
            if differences:raise BridgeError('APPEND_GRAPHIC_PARAMETERS_CHANGED')
            if ar.shapeType=='POINT':
                if annotation_text(a,sa,old)!=annotation_text(c,sc,new):raise BridgeError('APPEND_ANNOTATION_CHANGED')
                x,y=sa[0][0]+old*93,sc[0][0]+new*93
                if a[x+7:x+31]!=c[y+7:y+31] or a[x+31:x+93]!=c[y+31:y+93]:raise BridgeError('APPEND_POINT_RAW_DATA_CHANGED')
    subordinate={}
    if cr.shapeType=='POLYGON':
        validate_wp_topology(candidate);subordinate={'polygon_subordinate_records':_wp_attributes(source,target,candidate,mapping)}
    return {'count':len(actual),'mapping':mapping,'target_record_ids':target_ids,'target_record_ids_preserved':True,
        'geometry_equal':True,'primary_attribute_records_byte_identical':True,'annotation_equal':True,
        'graphic_parameters_equal':True,'map_parameters_byte_identical':True,**subordinate}


def append_inputs(file,expected_sha256,record_ids,template,target):
    if not isinstance(target,dict) or set(target)!={'file','expected_sha256'}:raise BridgeError('APPEND_TARGET: file and expected_sha256 required')
    source=Path(file).resolve(strict=True);base=Path(target['file']).resolve(strict=True)
    a,ar,sa=read_native(base);b,br,sb=read_native(source)
    if sha(b)!=expected_sha256 or sha(a)!=target['expected_sha256']:raise BridgeError('STALE_SOURCE')
    if ar.shapeType!=br.shapeType:raise BridgeError('APPEND_KIND: source and target native kinds must agree')
    if a[91:260]!=b[91:260]:raise BridgeError('APPEND_MAP_PARAMETERS: saved coordinate parameters must agree byte-for-byte')
    left,right=_schema(a,ar,sa),_schema(b,br,sb)
    if not _same_schema(left,right):raise BridgeError('APPEND_SCHEMA: native primary schemas differ')
    active=set(native_ids(br))
    if not isinstance(record_ids,list) or not 1<=len(record_ids)<=200000 or any(type(v) is not int or v not in active for v in record_ids) or len(set(record_ids))!=len(record_ids):raise BridgeError('APPEND_RECORD_IDS: ordered unique active physical IDs required')
    if not isinstance(template,dict) or set(template)!={'name'} or not isinstance(template['name'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,55}\.(wt|wl|wp)',template['name']) or Path(template['name']).suffix!=source.suffix.lower():raise BridgeError('APPEND_OUTPUT_NAME')
    if ar.shapeType=='POINT':
        for blob,reader,sections,ids in [(a,ar,sa,native_ids(ar)),(b,br,sb,record_ids)]:
            if any(blob[sections[0][0]+i*93+31] not in {0,1,2,3} for i in ids):raise BridgeError('APPEND_POINT_KIND: embedded resources require an explicit mapping')
    if ar.shapeType=='POLYGON':_wp_inputs(source,base,record_ids)
    return source,base,b,br,a,ar


def prepare_native_append(bridge,file,expected_sha256,record_ids,template,request_id,target):
    source,base,b,br,a,ar=append_inputs(file,expected_sha256,record_ids,template,target)
    _,bas=backend_configuration(bridge.root)
    target={'file':str(base),'expected_sha256':target['expected_sha256']}
    payload={'operation':'native.append','file':str(source),'expected_sha256':expected_sha256,'record_ids':record_ids,'template':template,'target':target,'append_protocol':1}
    stage,receipt,created=begin_bundle(bridge,payload,request_id,[{'file':str(source),'sha256':expected_sha256},{'file':str(base),'sha256':target['expected_sha256']}],template['name'])
    if not created:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        runtime=stage/'sdk-runtime';private_com_runtime(bas.parent,runtime)
        folder=stage/'native';folder.mkdir();suffix=source.suffix.lower()
        (folder/('input'+suffix)).write_bytes(b);(folder/('base'+suffix)).write_bytes(a)
        control=f'{KINDS[br.shapeType]} {len(record_ids)}\n'+' '.join(map(str,record_ids))+'\nbase base'+suffix
        (folder/'selection.txt').write_text(control,encoding='ascii')
        execution,events=invoke_com(bridge.root,folder,runtime,'append','input'+suffix,'copied'+suffix,'selection.txt','native-com.json')
        return finish_native_append(bridge,request_id,stage,payload,execution,events)
    except Exception as error:fail_bundle(bridge,request_id,error);raise


def finish_native_append(bridge,request_id,stage,payload,execution,events,recovered=False):
    suffix=Path(payload['file']).suffix.lower();source=stage/'native'/('input'+suffix);base=stage/'native'/('base'+suffix);copied=stage/'native'/('copied'+suffix)
    if file_sha(source)!=payload['expected_sha256'] or file_sha(base)!=payload['target']['expected_sha256']:raise BridgeError('APPEND_SOURCE_SNAPSHOT_CHANGED')
    saved=next((e for e in events if e['event']=='com_append_saved'),None)
    mapping=[{k:v for k,v in e.items() if k!='event'} for e in events if e['event']=='com_entity_mapping']
    base_count=len(native_ids(read_native(base)[1]))
    if not saved or file_sha(copied)!=saved['sha256'] or [v['source_record_id'] for v in mapping]!=payload['record_ids'] or saved['count']!=len(mapping) or saved['base_count']!=base_count or saved['total_count']!=base_count+len(mapping):raise BridgeError('APPEND_NATIVE_RECEIPT_DIFF')
    job=stage/('verified-'+uuid.uuid4().hex[:8]);job.mkdir();canonical=job/('input'+suffix);shutil.copyfile(copied,canonical)
    _primary(source,base,canonical,mapping);_,reader,_=read_native(canonical)
    if reader.shapeType=='POLYGON':_wp_attributes(source,base,canonical,mapping,restore=True)
    _check(source,base,canonical,mapping)
    exe,bas=backend_configuration(bridge.root);result=job/('result'+suffix)
    host=_invoke(exe,bas,job,reader.shapeType,'resave',result,'native-save.json');_validate(canonical,result,'resave',{})
    verification=_check(source,base,result,mapping)
    fresh=stage/('reopen-'+uuid.uuid4().hex[:8]);fresh.mkdir();shutil.copyfile(result,fresh/('input'+suffix))
    reopened=_invoke(exe,bas,fresh,reader.shapeType,'inspect',log_name='native-reopen.json')
    if reopened['active']!=verification['count'] or reopened['pid']==execution['pid']:raise BridgeError('APPEND_FRESH_REOPEN_FAILED')
    for file,digest in [(payload['file'],payload['expected_sha256']),(payload['target']['file'],payload['target']['expected_sha256'])]:
        if file_sha(Path(file))!=digest:raise BridgeError('STALE_SOURCE')
    files=stage/'files';files.mkdir(exist_ok=recovered);output=files/payload['template']['name'];shutil.copyfile(result,output)
    (files/(output.name+'.json')).write_text(json_text({'schema':'mapgis-agent/native-append/1','native_sha256':file_sha(output),**verification}),encoding='utf8')
    return finish_bundle(bridge,request_id,files,{'backend':'MapGIS 6.7 SDK CopyList into loaded target; BAS60 save and fresh-process reopen',
        'native_execution':execution,'native_save':host,'native_reopen':reopened,'source_preserved':True,'target_preserved':True,
        'source_ids_are_physical':True,'verification':verification,'registry_changed':False,'mouse_or_keyboard_input':False,
        'replayed':False,'reconciled_after_interruption':recovered})


def reconcile_native_append(bridge,request_id):
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row);stage=Path(receipt['stage'])
    if row['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if row['state']=='RUNNING':
        owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
        try:
            process=psutil.Process(owner['pid'])
            if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
        except psutil.NoSuchProcess:pass
    completed=completed_native_child(stage/'native','native-com.json')
    if completed is None:raise BridgeError('ARTIFACT_INCOMPLETE: no native copy was replayed')
    return finish_native_append(bridge,request_id,stage,receipt['payload'],*completed,recovered=True)
