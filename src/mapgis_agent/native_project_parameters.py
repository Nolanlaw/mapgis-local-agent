"""Native MPJ map parameters, preserving all layer bytes and project layout."""
from __future__ import annotations
import json,math,os,re,shutil,uuid
from pathlib import Path
import psutil
from .core import Bridge,BridgeError,json_text
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .native_backend import backend_configuration,completed_native_child
from .native_com import private_com_runtime,invoke_com
from .native_project import native_project_info,_project_library,_copy_project_library,_project_call
from .raster import file_sha


def parameter_schema():
    return json.loads(Path(__file__).with_name('data').joinpath('com67-map-parameters.json').read_text(encoding='utf8'))


def parameter_changes(changes):
    schema=parameter_schema();fields={v['name']:v for v in schema['fields']}
    if not isinstance(changes,dict) or not set(changes)<=set(fields):raise BridgeError('PROJECT_MAP_PARAMETERS: use names returned in parameter_schema')
    enum_names={'type':'Enum_SpatialRef_Type','levelType':'Enum_SpatialRef_Type','TICtype':'Enum_SpatialRef_Type',
        'unit':'Enum_SpatialUnit_Type','vUnit':'Enum_SpatialUnit_Type','TICunit':'Enum_SpatialUnit_Type','infoUnit':'Enum_SpatialUnit_Type',
        'projType':'Enum_SpatialPrj_Type','earthParam':'Enum_SpatialEP_Type'}
    for name,value in changes.items():
        if type(value) not in (int,float) or not math.isfinite(value):raise BridgeError('PROJECT_MAP_PARAMETER_NUMBER: '+name)
        if fields[name]['type']=='integer':
            values={v['value'] for v in schema['enums'][enum_names[name]]}
            if name in {'unit','vUnit','TICunit','infoUnit'}:values={v for v in values if 0<=v<=16}
            if type(value) is not int or value not in values:raise BridgeError('PROJECT_MAP_PARAMETER_ENUM: '+name)
        if name in {'rate','vRate','mapw','maph'} and value<0:raise BridgeError('PROJECT_MAP_PARAMETER_NONNEGATIVE: '+name)
        if name in {'infoRatex','infoRatey'} and value not in (0,1):raise BridgeError('PROJECT_MAP_PARAMETER_INITIAL_RATE: SDK requires 0 or 1')
    return dict(changes)


def _inputs(root,file,digest):
    source=Path(file).resolve(strict=True)
    if file_sha(source)!=digest:raise BridgeError('STALE_SOURCE')
    info=native_project_info(root,str(source),digest);library,library_sources=_project_library(source,digest)
    members=[];sources=[{'file':str(source),'sha256':digest}]+library_sources
    for layer in info['layers']:
        stored=layer['stored_file'];name=Path(layer['file']).name
        if Path(stored).name!=stored or stored.casefold()!=name.casefold():
            raise BridgeError('PROJECT_PARAMETERS_PORTABLE_REQUIRED: first clone dependencies with prepare_project_edit')
        if name.casefold() in {'input.mpj','result.mpj','parameters.txt'}:raise BridgeError('PROJECT_PARAMETERS_MEMBER_COLLISION')
        member={'file':layer['file'],'sha256':layer['sha256'],'name':name};members.append(member);sources.append(member.copy())
        sidecar=Path(layer['file']+'.json')
        if layer['kind']==11 and sidecar.is_file():
            witness=json.loads(sidecar.read_text(encoding='utf8'))
            if witness.get('native_sha256')!=layer['sha256']:raise BridgeError('MSI_SIDECAR_MISMATCH')
            member={'file':str(sidecar),'sha256':file_sha(sidecar),'name':sidecar.name};members.append(member);sources.append(member.copy())
    if len({v['name'].casefold() for v in members})!=len(members):
        # Repeated references to the same file are harmless and use one copy.
        by_name={}
        for member in members:
            key=member['name'].casefold()
            if key in by_name and by_name[key]['file']!=member['file']:raise BridgeError('PROJECT_PARAMETERS_MEMBER_COLLISION')
            by_name[key]=member
        members=list(by_name.values())
    sources=list({v['file']:v for v in sources}.values())
    return source,info,library,members,sources


def _snapshot(stage,source,members,bas):
    native=stage/'native';native.mkdir();shutil.copyfile(source,native/'input.mpj')
    for member in members:
        shutil.copyfile(member['file'],native/member['name'])
        if file_sha(native/member['name'])!=member['sha256']:raise BridgeError('STALE_SOURCE')
    runtime=stage/'sdk-runtime';private_com_runtime(bas.parent,runtime)
    return native,runtime


def _values(events,stage='before'):
    items=[e['values'] for e in events if e['event']=='com_project_parameters' and e['stage']==stage]
    if len(items)!=1 or set(items[0])!={v['name'] for v in parameter_schema()['fields']}:raise BridgeError('PROJECT_MAP_PARAMETER_RECEIPT')
    return items[0]


def _sources_unchanged(sources):
    if any(not Path(v['file']).is_file() or file_sha(Path(v['file']))!=v['sha256'] for v in sources):raise BridgeError('STALE_SOURCE')


def native_project_parameters_info(root,file,expected_sha256):
    source,info,library,members,sources=_inputs(root,file,expected_sha256);exe,bas=backend_configuration(Path(root))
    folder=Bridge(root).destination_path(Path(root)/'work'/('project-parameters-info-'+uuid.uuid4().hex));folder.mkdir()
    native,runtime=_snapshot(folder,source,members,bas)
    if file_sha(native/'input.mpj')!=expected_sha256:raise BridgeError('STALE_SOURCE')
    host,events=invoke_com(root,native,runtime,'project.params.native.inspect','input.mpj','-','-','native-com.json')
    _sources_unchanged(sources)
    if file_sha(native/'input.mpj')!=expected_sha256 or any(file_sha(native/v['name'])!=v['sha256'] for v in members):raise BridgeError('PROJECT_PARAMETER_INSPECTION_CHANGED_INPUT')
    return {'file':str(source),'sha256':expected_sha256,'parameters':_values(events),'parameter_schema':parameter_schema(),
        'native_execution':host,'source_preserved':True,'registry_changed':False,'mouse_or_keyboard_input':False,
        'scope':'Saved MPJ native metadata; no reprojection, layer edit, GUI state or ground-accuracy inference'}


def prepare_project_parameters(bridge,file,expected_sha256,changes,template,request_id):
    changes=parameter_changes(changes)
    if not changes:raise BridgeError('PROJECT_MAP_PARAMETER_CHANGES_REQUIRED: use the read-only info tool to inspect')
    if not isinstance(template,dict) or set(template)!={'name'} or not isinstance(template['name'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,55}\.mpj',template['name']):raise BridgeError('PROJECT_PARAMETERS_OUTPUT_NAME')
    source,info,library,members,sources=_inputs(bridge.root,file,expected_sha256);exe,bas=backend_configuration(bridge.root)
    reserved={v['name'].casefold() for v in members}|{v['name'].casefold() for v in library['members']} if library else {v['name'].casefold() for v in members}
    if template['name'].casefold() in reserved:raise BridgeError('PROJECT_PARAMETERS_MEMBER_COLLISION')
    payload={'operation':'native.project.parameters','file':str(source),'expected_sha256':expected_sha256,'changes':changes,'template':template,'parameter_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        native,runtime=_snapshot(stage,source,members,bas)
        if file_sha(native/'input.mpj')!=expected_sha256:raise BridgeError('STALE_SOURCE')
        context={'info':info,'library':library,'members':members,'sources':sources}
        (stage/'inputs.json').write_text(json_text(context),encoding='utf8')
        (native/'parameters.txt').write_text(str(len(changes))+'\n'+''.join(f'{k} {v!r}\n' for k,v in changes.items()),encoding='ascii')
        execution,events=invoke_com(bridge.root,native,runtime,'project.params.native.edit','input.mpj','result.mpj','parameters.txt','native-com.json')
        return finish_project_parameters(bridge,request_id,stage,payload,execution,events)
    except Exception as error:fail_bundle(bridge,request_id,error);raise


def finish_project_parameters(bridge,request_id,stage,payload,execution,events,recovered=False):
    context=json.loads((stage/'inputs.json').read_text(encoding='utf8'));members=context['members'];info=context['info'];library=context['library']
    native=stage/'native';candidate=native/'result.mpj';saved=next((e for e in events if e['event']=='com_project_parameters_saved'),None)
    if not saved or file_sha(candidate)!=saved['sha256'] or saved['layers']!=len(info['layers']):raise BridgeError('PROJECT_PARAMETERS_SAVE_DIFF')
    expected=_values(events)|payload['changes']
    if _values(events,'after')!=expected:raise BridgeError('PROJECT_PARAMETERS_ASSIGNMENT_DIFF')
    if file_sha(native/'input.mpj')!=payload['expected_sha256'] or any(file_sha(native/v['name'])!=v['sha256'] for v in members):raise BridgeError('PROJECT_PARAMETERS_LAYER_CHANGED')
    files=bridge.destination_path(stage/'files')
    if files.parent!=stage.resolve():raise BridgeError('PROJECT_PARAMETERS_STAGE_IDENTITY')
    if files.exists():
        if not recovered:raise BridgeError('PROJECT_PARAMETERS_STAGE_EXISTS')
        files.rename(bridge.destination_path(stage/('incomplete-files-'+uuid.uuid4().hex[:8])))
    files.mkdir();target=files/payload['template']['name'];shutil.copyfile(candidate,target)
    for member in members:shutil.copyfile(native/member['name'],files/member['name'])
    _copy_project_library(library,files,target.name)
    moved=bridge.destination_path(stage/('relocated-'+uuid.uuid4().hex[:8]));shutil.copytree(files,moved)
    hidden=bridge.destination_path(stage/('hidden-'+uuid.uuid4().hex[:8]));files.rename(hidden)
    try:
        host,readback=invoke_com(bridge.root,moved,stage/'sdk-runtime','project.params.native.inspect',target.name,'-','-','native-com-reopen.json')
        if _values(readback)!=expected:raise BridgeError('PROJECT_PARAMETERS_REOPEN_DIFF')
        exe,bas=backend_configuration(bridge.root)
        fresh,layers=_project_call(exe,bas,'inspect',moved/target.name,'-',stage/('native-reopen-'+uuid.uuid4().hex[:8]))
    finally:hidden.rename(files)
    if fresh['environment']!=info['environment'] or len(layers)!=len(info['layers']):raise BridgeError('PROJECT_PARAMETERS_LAYOUT_CHANGED')
    for original,actual in zip(info['layers'],layers):
        if any(original.get(k)!=actual.get(k) for k in ('kind','active','state','display_limits','native_bounds')) or actual['file'].casefold()!=original['stored_file'].casefold():raise BridgeError('PROJECT_PARAMETERS_LAYER_METADATA_CHANGED')
    if host['pid']==execution['pid'] or fresh['pid']==execution['pid']:raise BridgeError('PROJECT_PARAMETERS_FRESH_PROCESS_REQUIRED')
    _sources_unchanged(context['sources'])
    return finish_bundle(bridge,request_id,files,{'backend':'MapGIS 6.7 mapprj60 project header with the installed Map_Para SDK object; independent native readback',
        'native_execution':execution,'native_parameter_reopen':host,'native_project_reopen':fresh,'before':_values(events),'changes':payload['changes'],'parameters':expected,
        'layer_bytes_unchanged':True,'project_layout_unchanged':True,'source_preserved':True,'registry_changed':False,'mouse_or_keyboard_input':False,
        'portable_relocation':'PASS_WITH_OLD_DIRECTORY_HIDDEN','replayed':False,'reconciled_after_interruption':recovered,
        'scope':'Native project metadata assignment only; no layer coordinates or layer CRS were transformed'})


def reconcile_project_parameters(bridge,request_id):
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row);stage=bridge.destination_path(receipt['stage'])
    if row['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
    try:
        process=psutil.Process(owner['pid'])
        if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
    except psutil.NoSuchProcess:pass
    complete=completed_native_child(stage/'native','native-com.json')
    if complete is None:raise BridgeError('ARTIFACT_INCOMPLETE: no project parameter edit was replayed')
    return finish_project_parameters(bridge,request_id,stage,receipt['payload'],*complete,recovered=True)
