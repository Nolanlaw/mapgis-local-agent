"""Guarded installed SDK operations in the existing private native worker.

The 6.7 SDK's attribute reader is incompatible with some saved tables, and
CopyMapParam omits tail fields. Geometry uses native CopyList; the full saved
map parameters and primary attributes come from the verified source bytes.
Known WP arc/node records follow uniquely checked physical topology maps;
BAS60 save and independent reopen validate the repaired owned candidate.
"""
from __future__ import annotations
import io,json,math,os,re,shutil,struct,uuid
from pathlib import Path
import xml.etree.ElementTree as ET
import psutil
from pymapgis import reader as native
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import BridgeError,read_native,native_ids,annotation_text,sha,json_text
from .creation import _replace_sections
from .comparison import _extract_attributes,_extract_style,_diff_dicts
from .native_backend import backend_configuration,_run_child,_invoke,_validate,completed_native_child,KINDS
from .native_paths import NativePathAlias
from .raster import file_sha
from .topology import validate_wp_topology,_wp_arc_records,_wp_nodes
from shapely.affinity import affine_transform
from shapely import remove_repeated_points,STRtree
from shapely.geometry import LineString,Point,box


def private_com_runtime(program:Path,folder:Path):
    """Local installed DLL copies only; never package them or register COM."""
    profile=json.loads(Path(__file__).with_name('data').joinpath('com67-profile.json').read_text(encoding='utf8'))
    folder.mkdir()
    namespace='urn:schemas-microsoft-com:asm.v1'
    ET.register_namespace('',namespace)
    root=ET.Element('{'+namespace+'}assembly',{'manifestVersion':'1.0'})
    ET.SubElement(root,'{'+namespace+'}assemblyIdentity',{'name':'Mapgis.Agent.PrivateCom','version':'1.0.0.0','type':'win32','processorArchitecture':'x86'})
    for item in profile['files']:
        source=program/item['name'];target=folder/item['name']
        if not source.is_file() or file_sha(source)!=item['sha256']:raise BridgeError('COM_SDK_BUILD_MISMATCH: '+item['name'])
        shutil.copyfile(source,target)
        if file_sha(target)!=item['sha256'] or file_sha(source)!=item['sha256']:raise BridgeError('COM_SDK_COPY_CHANGED')
        element=ET.SubElement(root,'{'+namespace+'}file',{'name':item['name']})
        for cls in item['classes']:
            ET.SubElement(element,'{'+namespace+'}comClass',{'clsid':cls['clsid'],'threadingModel':'Apartment','tlbid':item['typelib'],'description':cls['name']})
        ET.SubElement(element,'{'+namespace+'}typelib',{'tlbid':item['typelib'],'version':item['version'],'helpdir':''})
    manifest=folder/'private.manifest';ET.ElementTree(root).write(manifest,encoding='utf-8',xml_declaration=True)
    return manifest


def _table(blob,reader,sections,index=None):
    index=native._ATTR_HEADER_INDEX[reader.shapeType] if index is None else index
    start,size=sections[index]
    count,length,fields=native._read_attribute_header(io.BytesIO(blob),start)
    data=start+native._attribute_record_origin(blob[start:start+348],size)[0]
    if count<1 or length<1 or data+count*length>start+size:raise BridgeError('SUBSET_ATTRIBUTE_LAYOUT')
    return index,start,data,count,length


def _restore_primary_attributes(source:Path,candidate:Path,mapping:list[dict]):
    original,reader,sections=read_native(source);current=candidate.read_bytes()
    # Inspect the native section envelope before repairing its known-bad table;
    # the strict row reader correctly rejects that table's damaged active flags.
    if len(current)<817 or current[:8]!=original[:8]:raise BridgeError('SUBSET_NATIVE_TYPE_CHANGED')
    table=struct.unpack_from('<i',current,12)[0]
    if table<16 or table+160>len(current):raise BridgeError('SUBSET_NATIVE_SECTION_TABLE')
    entries=native._read_header_entries(io.BytesIO(current),table,16)
    if any(offset<0 or size<0 or offset+size>len(current) for offset,size in entries):raise BridgeError('SUBSET_NATIVE_SECTION_RANGE')
    index,start,data,count,length=_table(original,reader,sections)
    maximum=max(v['output_record_id'] for v in mapping)
    if maximum>len(mapping) or set(v['output_record_id'] for v in mapping)!=set(range(1,len(mapping)+1)):raise BridgeError('SUBSET_OUTPUT_IDENTITY')
    header=bytearray(original[start:data]);struct.pack_into('<i',header,324,maximum+1)
    records=bytearray(original[data:data+length]+b'\0'*(maximum*length))
    for item in mapping:
        old,new=item['source_record_id'],item['output_record_id']
        if not 1<=old<count:raise BridgeError('SUBSET_ATTRIBUTE_IDENTITY')
        records[new*length:(new+1)*length]=original[data+old*length:data+(old+1)*length]
    restored=bytearray(_replace_sections(current,entries,{index:bytes(header)+bytes(records)}))
    restored[91:260]=original[91:260]
    candidate.write_bytes(restored)


def _wp_subset_mapping(source:Path,candidate:Path,mapping:list[dict],transform=None,*,restore_nodes=False):
    """Match physical arcs by shape and owners; nodes by incident arcs and XY.

    Spatial indexing only finds candidates. There must be exactly one matching
    source identity, so coincident arcs never silently exchange attributes.
    Closed native ring arcs may have no endpoint-node identities (both zero).
    """
    return _wp_copy_mapping(_wp_arc_records(source)[3],candidate,mapping,transform,restore_nodes=restore_nodes)


def _wp_copy_mapping(old,candidate,mapping,transform=None,*,restore_nodes=False,node_ids=None,node_count=None,include_unowned=False):
    """The same geometry/owner/incidence proof also accepts merged source IDs."""
    _,_,_,new=_wp_arc_records(candidate)
    regions={m['source_record_id']:m['output_record_id'] for m in mapping}
    old={i:a for i,a in old.items() if regions.keys() & set(a['owner_region_ids']) or (include_unowned and not a['owner_region_ids'])}
    identities=list(old);matrix=_subset_matrix(transform) if transform else None
    expected=[affine_transform(LineString(a['coordinates']),matrix) if matrix else LineString(a['coordinates']) for a in old.values()]
    tree=STRtree(expected);used=set();arcs=[]
    for nid,actual in new.items():
        geometry=LineString(actual['coordinates']);tolerance=max(1.,*(abs(v) for v in geometry.bounds))*128*math.ulp(1.)
        x0,y0,x1,y1=geometry.bounds;matches=[]
        for index in tree.query(box(x0-tolerance,y0-tolerance,x1+tolerance,y1+tolerance)):
            oid=identities[index];owners=sorted(regions[i] for i in old[oid]['owner_region_ids'] if i in regions)
            if owners!=actual['owner_region_ids']:continue
            other=expected[index]
            if other.equals(geometry) or remove_repeated_points(other,tolerance).equals_exact(remove_repeated_points(geometry,tolerance),tolerance=tolerance,normalize=True):matches.append(oid)
        if len(matches)!=1 or matches[0] in used:raise BridgeError(f'SUBSET_ARC_IDENTITY_AMBIGUOUS: output arc {nid}, matches {matches}')
        oid=matches[0];used.add(oid)
        if _diff_dicts(old[oid]['style'],actual['style']):raise BridgeError('SUBSET_ARC_GRAPHIC_PARAMETERS_CHANGED')
        arcs.append({'source_record_id':oid,'output_record_id':nid})
    if used!=set(old):raise BridgeError('SUBSET_ARC_IDENTITY_MISSING')
    assigned={i:a for i,a in old.items() if a['node_ids']!=[0,0]}
    old_nodes=_wp_nodes(assigned)
    if node_ids is None:node_ids={identity:i for i,identity in enumerate(sorted(old_nodes),1)}
    elif set(node_ids)!=set(old_nodes) or len(set(node_ids.values()))!=len(node_ids) or any(v<1 for v in node_ids.values()):raise BridgeError('SUBSET_NODE_IDENTITY_MAP')
    nodes=[{'source_record_id':old,'output_record_id':new} for old,new in node_ids.items()]
    endpoint_ids={}
    for pair in arcs:
        source_arc=old[pair['source_record_id']];actual=new[pair['output_record_id']]
        if source_arc['node_ids']==[0,0]:
            if source_arc['coordinates'][0]!=source_arc['coordinates'][-1]:raise BridgeError('SUBSET_SOURCE_NODE_IDENTITIES_MISSING')
            endpoint_ids[pair['output_record_id']]=[0,0];continue
        source_points=[Point(source_arc['coordinates'][i]) for i in (0,-1)]
        expected=[affine_transform(g,matrix) if matrix else g for g in source_points]
        output=[Point(actual['coordinates'][i]) for i in (0,-1)]
        tolerance=max(1.,*(abs(v) for g in expected for v in g.coords[0]))*128*math.ulp(1.)
        forward=all(a.equals_exact(b,tolerance=tolerance) for a,b in zip(expected,output))
        backward=all(a.equals_exact(b,tolerance=tolerance) for a,b in zip(expected,reversed(output)))
        if not(forward or backward) or (forward and backward and len(set(source_arc['node_ids']))>1):raise BridgeError('SUBSET_NODE_IDENTITY_AMBIGUOUS: arc endpoints')
        values=source_arc['node_ids'] if forward else list(reversed(source_arc['node_ids']))
        endpoint_ids[pair['output_record_id']]=[node_ids[i] for i in values]
    if restore_nodes:_restore_subset_nodes(candidate,endpoint_ids,len(node_ids) if node_count is None else node_count)
    else:
        if any(new[aid]['node_ids']!=ids for aid,ids in endpoint_ids.items()):raise BridgeError('SUBSET_NODE_IDENTITY_CHANGED')
        # Incidence is checked from the assigned output topology, independently
        # of the source-to-output endpoint assignment above.
        actual_nodes=_wp_nodes({i:a for i,a in new.items() if a['node_ids']!=[0,0]})
        arc_ids={m['source_record_id']:m['output_record_id'] for m in arcs}
        for old_id,new_id in node_ids.items():
            if set(actual_nodes[new_id]['incident_arc_ids'])!={arc_ids[i] for i in old_nodes[old_id]['incident_arc_ids']}:raise BridgeError('SUBSET_NODE_INCIDENCE_CHANGED')
    return {'arc_mapping':arcs,'node_mapping':nodes}


def _restore_subset_nodes(candidate:Path,endpoints:dict,count:int):
    """Supply the pinned WP node prefix and endpoint IDs omitted by CopyList."""
    blob,reader,sections=read_native(candidate);arc_index=bytearray(blob[sections[0][0]:sum(sections[0])])
    topology=bytearray(blob[sections[3][0]:sum(sections[3])]);coordinates=blob[sections[1][0]:sum(sections[1])]
    regions=bytearray(blob[sections[8][0]:sum(sections[8])])
    if count>65535:raise BridgeError('SUBSET_NODE_TABLE_LIMIT: verified 16-bit node prefix exceeded')
    stored={i:struct.unpack_from('<2i',arc_index,i*57+10) for i in range(1,len(arc_index)//57)}
    if set(endpoints)!={i for i in stored if arc_index[i*57]}:raise BridgeError('SUBSET_NODE_ARC_LAYOUT')
    first=min((offset for count,offset in stored.values() if count),default=2)
    prefix=coordinates[:first]
    if prefix[:2]!=b'\0\0' or len(prefix)%4!=2 or any(struct.unpack_from('<2H',prefix,i)!=(0,(i-2)//4+1) for i in range(2,len(prefix),4)):
        raise BridgeError('SUBSET_NODE_PREFIX_LAYOUT')
    replacement=struct.pack('<H',0)+b''.join(struct.pack('<2H',0,i) for i in range(1,count+1));delta=len(replacement)-first
    for aid,ids in endpoints.items():
        struct.pack_into('<2i',topology,aid*24,*ids)
    for aid,(points,offset) in stored.items():
        if points:
            if offset<first:raise BridgeError('SUBSET_NODE_ARC_OFFSET')
            struct.pack_into('<i',arc_index,aid*57+14,offset+delta)
    for identity in range(1,len(regions)//40):
        if not struct.unpack_from('<i',regions,identity*40+1)[0]:continue
        offset=struct.unpack_from('<i',regions,identity*40+5)[0]
        if offset<first:raise BridgeError('SUBSET_NODE_REGION_WALK_OFFSET')
        struct.pack_into('<i',regions,identity*40+5,offset+delta)
    candidate.write_bytes(_replace_sections(blob,sections,{0:arc_index,1:replacement+coordinates[first:],3:topology,8:regions}))


def _wp_subset_attributes(source:Path,candidate:Path,mapping:list[dict],transform=None,*,restore=False):
    original,reader,sections=read_native(source)
    identities=_wp_subset_mapping(source,candidate,mapping,transform,restore_nodes=restore)
    current,other,entries=read_native(candidate);replacements={};tables={}
    for index,key in [(2,'arc_mapping'),(5,'node_mapping')]:
        offset,size=sections[index]
        if size<348 or original[offset:offset+2]!=b'\x60\x44' or original[offset+320:offset+322]!=b'\x60\x44':
            # The pinned writer also has metadata-only auxiliary blocks here.
            # Their timestamp bytes must not be interpreted as attribute counts.
            tables[key.removesuffix('_mapping')]={'section':index,'source_rows':0,'source_layout':'no native attribute header'}
            continue
        _,start,data,count,width=_table(original,reader,sections,index);pairs=identities[key]
        # A one-row table contains only its reserved slot; it has no per-object attributes.
        pairs=pairs if count>1 else []
        maximum=max((v['output_record_id'] for v in pairs),default=0)
        header=bytearray(original[start:data]);struct.pack_into('<i',header,324,maximum+1)
        records=bytearray(original[data:data+width]+b'\0'*(maximum*width))
        for pair in pairs:
            old,new=pair['source_record_id'],pair['output_record_id']
            if not 1<=old<count:raise BridgeError('SUBSET_AUXILIARY_ATTRIBUTE_IDENTITY')
            records[new*width:(new+1)*width]=original[data+old*width:data+(old+1)*width]
        expected=bytes(header)+bytes(records);offset,size=entries[index]
        if restore:replacements[index]=expected
        elif current[offset:offset+size]!=expected:raise BridgeError('SUBSET_AUXILIARY_ATTRIBUTE_BYTES_CHANGED')
        tables[key.removesuffix('_mapping')]={'section':index,'source_rows':count-1,'output_rows':maximum,'record_width':width}
    if restore:candidate.write_bytes(_replace_sections(current,entries,replacements))
    return {**identities,'tables':tables,'attribute_records_byte_identical':True,
            'identity_basis':'Arc geometry plus selected region owners; node XY plus incident physical arc identities. Ambiguous matches rejected.'}


def subset_transform(parameters):
    if parameters is None:return None
    if not isinstance(parameters,dict) or set(parameters)!={'translation','scale','rotation_degrees','order','coordinate_frame'}:raise BridgeError('SUBSET_TRANSFORM_FIELDS')
    if parameters['coordinate_frame']!='source':raise BridgeError('SUBSET_TRANSFORM_FRAME: source XY coordinate units required')
    for key in ('translation','scale'):
        values=parameters[key]
        if not isinstance(values,list) or len(values)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in values):raise BridgeError('SUBSET_TRANSFORM_NUMBERS: '+key)
    if any(v==0 for v in parameters['scale']):raise BridgeError('SUBSET_TRANSFORM_ZERO_SCALE')
    angle=parameters['rotation_degrees'];order=parameters['order']
    if type(angle) not in (int,float) or not math.isfinite(angle):raise BridgeError('SUBSET_TRANSFORM_ANGLE')
    if not isinstance(order,list) or len(order)!=3 or any(not isinstance(v,str) for v in order) or set(order)!={'scale','rotate','translate'}:raise BridgeError('SUBSET_TRANSFORM_ORDER')
    return {k:list(v) if isinstance(v,list) else v for k,v in parameters.items()}


def _subset_matrix(parameters):
    result=[1.,0.,0.,1.,0.,0.];angle=math.radians(parameters['rotation_degrees']);co,si=math.cos(angle),math.sin(angle)
    sx,sy=parameters['scale'];dx,dy=parameters['translation']
    matrices={'scale':[sx,0,0,sy,0,0],'rotate':[co,-si,si,co,0,0],'translate':[1,0,0,1,dx,dy]}
    for key in parameters['order']:
        a,b,d,e,x,y=matrices[key];c,f,g,h,u,v=result
        result=[a*c+b*g,a*f+b*h,d*c+e*g,d*f+e*h,a*u+b*v+x,d*u+e*v+y]
    if any(not math.isfinite(v) for v in result):raise BridgeError('SUBSET_TRANSFORM_MATRIX_RANGE')
    return result


def _check_subset(source:Path,candidate:Path,mapping:list[dict],transform=None):
    a,b,sa=read_native(source);c,d,sd=read_native(candidate)
    original_ids,actual_ids=native_ids(b),native_ids(d)
    if b.shapeType!=d.shapeType or actual_ids!=[m['output_record_id'] for m in mapping]:raise BridgeError('SUBSET_REOPEN_IDENTITY')
    # The native CopyMapParam contract includes the complete saved parameter
    # block, not just an EPSG name or a guessed local coordinate unit.
    if a[91:260]!=c[91:260]:raise BridgeError('SUBSET_MAP_PARAMETERS_CHANGED')
    _,_,start,_,width=_table(a,b,sa);_,_,other,_,other_width=_table(c,d,sd)
    if width!=other_width or list(b.data.columns)!=list(d.data.columns):raise BridgeError('SUBSET_ATTRIBUTE_SCHEMA_CHANGED')
    rows={rid:i for i,rid in enumerate(original_ids)};output_rows={rid:i for i,rid in enumerate(actual_ids)}
    matrix=_subset_matrix(transform) if transform else None;maximum_error=0.;source_equal=True
    for item in mapping:
        old,new=item['source_record_id'],item['output_record_id'];i,j=rows[old],output_rows[new]
        expected=affine_transform(b.geom[i],matrix) if matrix else b.geom[i]
        source_equal=source_equal and b.geom[i].equals(d.geom[j])
        if matrix:
            # Native shared-arc joins contain repeated vertices. Floating
            # roundoff can change their canonical ring start without changing
            # the shape; remove only adjacent steps within 128 machine ulps.
            tolerance=max(1.,*(abs(v) for v in expected.bounds))*128*math.ulp(1.)
            if not expected.equals(d.geom[j]) and not remove_repeated_points(expected,tolerance).equals_exact(remove_repeated_points(d.geom[j],tolerance),tolerance=tolerance,normalize=True):raise BridgeError('SUBSET_TRANSFORM_GEOMETRY_DIFF')
            maximum_error=max(maximum_error,expected.hausdorff_distance(d.geom[j]))
        elif not expected.equals(d.geom[j]):raise BridgeError('SUBSET_GEOMETRY_CHANGED')
        if a[start+old*width:start+(old+1)*width]!=c[other+new*width:other+(new+1)*width]:raise BridgeError('SUBSET_ATTRIBUTE_BYTES_CHANGED')
        if _diff_dicts(_extract_attributes(b,i),_extract_attributes(d,j)):raise BridgeError('SUBSET_ATTRIBUTES_CHANGED')
        styles=_diff_dicts(_extract_style(b,i),_extract_style(d,j))
        if b.shapeType=='POLYGON':styles={k:v for k,v in styles.items() if k not in {'field5','field6','field7'}}
        if styles:raise BridgeError('SUBSET_GRAPHIC_PARAMETERS_CHANGED')
        if b.shapeType=='POINT' and annotation_text(a,sa,old)!=annotation_text(c,sd,new):raise BridgeError('SUBSET_ANNOTATION_CHANGED')
    subordinate={}
    if b.shapeType=='POLYGON':
        validate_wp_topology(candidate)
        subordinate={'polygon_subordinate_records':_wp_subset_attributes(source,candidate,mapping,transform)}
    return {'count':len(mapping),'mapping':mapping,'geometry_equal':source_equal,'primary_attribute_records_byte_identical':True,
        'annotation_equal':True,'graphic_parameters_equal':True,'map_parameters_byte_identical':True,**subordinate,
        **({'geometry_matches_requested_transform':True,'transform':transform,'source_unit_matrix':matrix,'maximum_geometry_error':maximum_error,
            'coordinate_check':'GEOS topological equality or repeated-step-normalized structural equality within 128 machine ulps times coordinate magnitude'} if matrix else {})}


def invoke_com(root,folder,runtime,operation,source_name,target_name,control_name,label):
    exe,bas=backend_configuration(Path(root))
    with NativePathAlias(runtime,record_folder=folder) as library,NativePathAlias(folder) as data:
        return _run_child([str(exe),'com',str(bas),str(library.path/'private.manifest'),operation,
            str(data.path/source_name),'-' if target_name=='-' else str(data.path/target_name),
            '-' if control_name=='-' else str(data.path/control_name)],folder,label)


def prepare_native_subset(bridge,file,expected_sha256,record_ids,template,request_id,transform=None,target=None):
    if target is not None:
        if transform is not None:raise BridgeError('APPEND_TRANSFORM: first create an explicitly transformed selection, then append it')
        from .native_append import prepare_native_append
        return prepare_native_append(bridge,file,expected_sha256,record_ids,template,request_id,target)
    source=Path(file).resolve(strict=True);blob,reader,sections=read_native(source)
    if sha(blob)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    active=set(native_ids(reader))
    if not isinstance(record_ids,list) or not 1<=len(record_ids)<=200000 or any(type(v) is not int or v not in active for v in record_ids) or len(set(record_ids))!=len(record_ids):
        raise BridgeError('SUBSET_RECORD_IDS: unique active physical IDs required')
    if reader.shapeType=='POLYGON':
        for i,(offset,size) in enumerate(sections):
            if i!=native._ATTR_HEADER_INDEX['POLYGON'] and size>=348 and blob[offset:offset+2]==blob[offset+320:offset+322]==b'\x60\x44':
                fields,count,_=struct.unpack_from('<hih',blob,offset+322)
                if fields>0 and count>1 and i not in {2,5}:raise BridgeError('SUBSET_AUXILIARY_ATTRIBUTES: unknown nonempty attribute section needs an explicit mapping')
                if i in {2,5}:_table(blob,reader,sections,i)
    if not isinstance(template,dict) or set(template)!={'name'} or not isinstance(template['name'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,55}\.(wt|wl|wp)',template['name']) or Path(template['name']).suffix!=source.suffix.lower():
        raise BridgeError('SUBSET_OUTPUT_NAME: lowercase ASCII filename with the source vector extension')
    transform=subset_transform(transform)
    if transform:_subset_matrix(transform)
    exe,bas=backend_configuration(bridge.root)
    payload={'operation':'native.subset','file':str(source),'expected_sha256':expected_sha256,'record_ids':record_ids,'template':template,'subset_protocol':1}
    if transform:payload|={'transform':transform,'subset_protocol':2}
    if reader.shapeType=='POLYGON':payload['subset_protocol']=3
    stage,receipt,created=begin_bundle(bridge,payload,request_id,[{'file':str(source),'sha256':expected_sha256}],template['name'])
    if not created:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        runtime=stage/'sdk-runtime';private_com_runtime(bas.parent,runtime)
        folder=stage/'native';folder.mkdir();suffix=source.suffix.lower();(folder/('input'+suffix)).write_bytes(blob)
        (folder/'input.json').write_text(json_text({'suffix':suffix}),encoding='utf8')
        control=f'{KINDS[reader.shapeType]} {len(record_ids)}\n'+' '.join(map(str,record_ids))
        if transform:
            _,factor=native._read_crs(io.BytesIO(blob))
            if not math.isfinite(factor) or factor<=0:raise BridgeError('SUBSET_TRANSFORM_COORDINATE_FACTOR')
            dx,dy=transform['translation'];sx,sy=transform['scale'];codes={'translate':1,'rotate':2,'scale':3}
            values=[sx,sy,transform['rotation_degrees'],dx/factor,dy/factor,*[codes[k] for k in transform['order']]]
            control+='\ntransform '+' '.join(repr(v) for v in values)
        (folder/'selection.txt').write_text(control,encoding='ascii')
        execution,events=invoke_com(bridge.root,folder,runtime,'subset','input'+suffix,'copied'+suffix,'selection.txt','native-com.json')
        return finish_native_subset(bridge,request_id,stage,payload,execution,events)
    except Exception as error:fail_bundle(bridge,request_id,error);raise


def finish_native_subset(bridge,request_id,stage,payload,execution,events,recovered=False):
    suffix=Path(payload['file']).suffix.lower();source=stage/'native'/('input'+suffix);copied=stage/'native'/('copied'+suffix)
    if file_sha(source)!=payload['expected_sha256']:raise BridgeError('SUBSET_SOURCE_SNAPSHOT_CHANGED')
    saved=next((e for e in events if e['event']=='com_subset_saved'),None)
    mapping=[{k:v for k,v in e.items() if k!='event'} for e in events if e['event']=='com_entity_mapping']
    if not saved or file_sha(copied)!=saved['sha256'] or [v['source_record_id'] for v in mapping]!=payload['record_ids'] or saved['count']!=len(mapping):raise BridgeError('SUBSET_NATIVE_RECEIPT_DIFF')
    if sum(e['event']=='com_subset_transformed' for e in events)!=(1 if payload.get('transform') else 0):raise BridgeError('SUBSET_TRANSFORM_RECEIPT_DIFF')
    job=stage/('verified-'+uuid.uuid4().hex[:8]);job.mkdir();canonical=job/('input'+suffix);shutil.copyfile(copied,canonical)
    transform=payload.get('transform')
    _restore_primary_attributes(source,canonical,mapping)
    _,reader,_=read_native(canonical)
    if reader.shapeType=='POLYGON':_wp_subset_attributes(source,canonical,mapping,transform,restore=True)
    _check_subset(source,canonical,mapping,transform)
    exe,bas=backend_configuration(bridge.root);result=job/('result'+suffix)
    host=_invoke(exe,bas,job,reader.shapeType,'resave',result,'native-save.json');_validate(canonical,result,'resave',{})
    verification=_check_subset(source,result,mapping,transform)
    fresh=stage/('reopen-'+uuid.uuid4().hex[:8]);fresh.mkdir();shutil.copyfile(result,fresh/('input'+suffix))
    reopened=_invoke(exe,bas,fresh,reader.shapeType,'inspect',log_name='native-reopen.json')
    if reopened['active']!=len(mapping) or reopened['pid']==execution['pid']:raise BridgeError('SUBSET_FRESH_REOPEN_FAILED')
    if file_sha(Path(payload['file']))!=payload['expected_sha256']:raise BridgeError('STALE_SOURCE')
    files=stage/'files';files.mkdir(exist_ok=recovered);target=files/payload['template']['name'];shutil.copyfile(result,target)
    witness={'schema':'mapgis-agent/native-subset/1','native_sha256':file_sha(target),'source_sha256':payload['expected_sha256'],**verification}
    (files/(target.name+'.json')).write_text(json_text(witness),encoding='utf8')
    return finish_bundle(bridge,request_id,files,{'backend':'MapGIS 6.7 installed SDK CopyList plus byte-preserved primary attributes and BAS60 save',
        'native_execution':execution,'native_save':host,'native_reopen':reopened,'source_preserved':True,
        'source_ids_are_physical':True,'verification':verification,'registry_changed':False,'mouse_or_keyboard_input':False,
        'sdk_attribute_reader':'Not used: incompatible on observed extended tables; verified source primary records copied verbatim',
        'polygon_subordinate_records':verification.get('polygon_subordinate_records','Not a WP file'),
        'replayed':False,'reconciled_after_interruption':recovered})


def reconcile_native_subset(bridge,request_id):
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row);stage=Path(receipt['stage'])
    if row['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
    try:
        process=psutil.Process(owner['pid'])
        if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
    except psutil.NoSuchProcess:pass
    completed=completed_native_child(stage/'native','native-com.json')
    if completed is None:raise BridgeError('ARTIFACT_INCOMPLETE: no selection copy was replayed')
    return finish_native_subset(bridge,request_id,stage,receipt['payload'],*completed,recovered=True)
