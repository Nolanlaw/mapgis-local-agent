"""Native MSD 6.0 image collections with portable, guarded MSI dependencies.

CMsiDB close can save the open index. Even inspection therefore opens only a
rebased owned snapshot, never an original. Native ordinals stay in the worker.
"""
from __future__ import annotations

import json,math,os,re,shutil,uuid
from pathlib import Path
import psutil
from pydantic import BaseModel,ConfigDict,StrictInt
from pyproj import CRS

from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import Bridge,BridgeError,json_text
from .native_backend import backend_configuration,_run_child,completed_native_child
from .native_paths import NativePathAlias
from .raster import file_sha


class MsiSource(BaseModel):
    model_config=ConfigDict(extra='forbid')
    file:str
    expected_sha256:str
    expected_witness_sha256:str|None=None


class MsdParameters(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str
    coordinate_frame:str


def _msi(file,digest,witness_digest=None,require_witness=False):
    path=Path(file).resolve(strict=True)
    if path.suffix.lower()!='.msi':raise BridgeError('MSD_MSI_REQUIRED')
    if file_sha(path)!=digest:raise BridgeError('STALE_SOURCE')
    result={'file':str(path),'expected_sha256':digest};sources=[{'file':str(path),'sha256':digest}]
    sidecar=path.with_name(path.name+'.json')
    if sidecar.is_file():
        actual=file_sha(sidecar)
        if require_witness and witness_digest is None:raise BridgeError('MSI_WITNESS_HASH_REQUIRED: pass the .msi.json member hash from the raster receipt')
        if witness_digest is not None and actual!=witness_digest:raise BridgeError('STALE_MSI_WITNESS')
        witness=json.loads(sidecar.read_text(encoding='utf8'))
        if witness.get('native_sha256')!=digest or witness.get('native_coordinate_protocol')!=2:raise BridgeError('MSI_SIDECAR_MISMATCH: use a fingerprinted coordinate protocol 2 witness')
        raster=witness.get('raster',{})
        result.update(expected_witness_sha256=actual,crs_wkt=raster.get('crs_wkt') or raster.get('gcp_crs_wkt'))
        sources.append({'file':str(sidecar),'sha256':actual})
    elif witness_digest is not None:raise BridgeError('STALE_MSI_WITNESS')
    else:result.update(expected_witness_sha256=None,crs_wkt=None)
    return result,sources


def _parse(path):
    if path.suffix.lower()!='.msd' or path.stat().st_size>1024*1024:raise BridgeError('MSD_FORMAT: bounded MSD 6.0 index required')
    try:lines=path.read_text(encoding='gbk').splitlines()
    except UnicodeError as exc:raise BridgeError('MSD_FORMAT: expected native GBK text') from exc
    if len(lines)<3 or lines[0].casefold()!='msidatabase6.0;' or lines[2]!='文件名;':raise BridgeError('MSD_FORMAT: unsupported index version or fields')
    counts=re.fullmatch(r'(\d+);1;',lines[1])
    if not counts or int(counts[1])>1000 or len(lines)!=int(counts[1])+3:raise BridgeError('MSD_FORMAT: expected one filename column and at most 1000 images')
    names=[]
    for line in lines[3:]:
        if not line.endswith(';') or len(line)<2 or ';' in line[:-1] or '\x00' in line:raise BridgeError('MSD_FORMAT: malformed filename row')
        name=line[:-1]
        if Path(name).suffix.lower()!='.msi':raise BridgeError('MSD_FORMAT: expected MSI reference')
        names.append(name)
    return names


def msd_source_info(file,expected_sha256=None):
    path=Path(file).resolve(strict=True);digest=file_sha(path)
    if expected_sha256 is not None and digest!=expected_sha256:raise BridgeError('STALE_SOURCE')
    names=_parse(path);images=[];members={}
    for index,name in enumerate(names,1):
        referenced=Path(name)
        if not referenced.is_absolute():referenced=path.parent/referenced
        if not referenced.is_file():raise BridgeError('MSD_MEMBER_MISSING: '+str(referenced))
        image,sources=_msi(str(referenced),file_sha(referenced))
        images.append({'index':index,'stored_name':name,**image})
        members.update({s['file']:s['sha256'] for s in sources})
    sidecar=path.with_name(path.name+'.json');coordinate_frame=None
    if sidecar.is_file():
        value=json.loads(sidecar.read_text(encoding='utf8'))
        if value.get('schema')!='mapgis-agent/msd/1' or value.get('native_sha256')!=digest:raise BridgeError('MSD_SIDECAR_MISMATCH')
        coordinate_frame=value.get('coordinate_frame');members[str(sidecar)]=file_sha(sidecar)
    if file_sha(path)!=digest or any(file_sha(Path(k))!=v for k,v in members.items()):raise BridgeError('STALE_DURING_MSD_READ')
    return {'file':str(path),'source_sha256':digest,'format':'MsiDataBase6.0','images':images,'members':members,
        'coordinate_frame':coordinate_frame,'scope':'saved image collection; dependencies are identified by resolved absolute path and hash'}


def _bound(file,digest,expected_members):
    info=msd_source_info(file,digest)
    if expected_members!=info['members']:raise BridgeError('STALE_MSD_MEMBER: inspect msd_source_info and pass its complete members map')
    return info,[{'file':info['file'],'sha256':digest}]+[{'file':k,'sha256':v} for k,v in info['members'].items()]


def _sources_unchanged(sources):
    if any(not Path(s['file']).is_file() or file_sha(Path(s['file']))!=s['sha256'] for s in sources):raise BridgeError('STALE_SOURCE')


def _frame(images,parameters):
    if parameters.coordinate_frame!='native':raise BridgeError('MSD_COORDINATE_FRAME: declare coordinate_frame=native only after establishing common physical coordinates')
    known=[v['crs_wkt'] for v in images if v.get('crs_wkt')]
    if known and any(not CRS(known[0]).equals(CRS(v)) for v in known[1:]):raise BridgeError('MSD_COORDINATE_FRAME: known member coordinate systems differ; reproject before composing')
    return {'declaration':'native','crs_wkt':known[0] if known and len(known)==len(images) else None,
        'members_without_crs_witness':sum(not v.get('crs_wkt') for v in images),'policy':'no coordinate transformation; unknown frames rely on the explicit caller declaration'}


def _index(path,names):
    path.write_bytes(('MsiDataBase6.0;\r\n'+str(len(names))+';1;\r\n文件名;\r\n'+''.join(v+';\r\n' for v in names)).encode('gbk'))


def _copy_image(image,folder,name):
    target=folder/name;shutil.copyfile(image['file'],target)
    if file_sha(target)!=image['expected_sha256']:raise BridgeError('STALE_SOURCE')
    if image.get('expected_witness_sha256'):
        src=Path(image['file']+'.json');dst=target.with_name(target.name+'.json');shutil.copyfile(src,dst)
        if file_sha(dst)!=image['expected_witness_sha256']:raise BridgeError('STALE_MSI_WITNESS')


def _invoke(root,folder,operation,input_name='input.msd',output_name='result.msd',control=None,label='native-msd.json'):
    executable,bas=backend_configuration(Path(root))
    with NativePathAlias(folder,record_folder=folder) as alias:
        args=[str(executable),'msd',str(bas),str(bas.parent/'CMsiDB.dll'),operation,
            '-' if operation=='create' else str(alias.path/input_name),'-' if operation=='inspect' else str(alias.path/output_name)]
        if control is not None:args.append(str(alias.path/control))
        return _run_child(args,folder,label)


def _result(events,images):
    members=[e for e in events if e['event']=='msd_member'];info=next((e for e in events if e['event']=='msd'),None)
    if not info or info['members']!=len(images) or [v['name'] for v in members]!=[v['name'] for v in images] or [v['index'] for v in members]!=list(range(1,len(images)+1)):raise BridgeError('NATIVE_MSD_READBACK_DIFF')
    union=None
    if members:
        boxes=[m['native_bounds'] for m in members];union=[min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
        if any(not math.isclose(a,b,abs_tol=1e-7,rel_tol=1e-9) for a,b in zip(union,info['native_bounds'])):raise BridgeError('NATIVE_MSD_EXTENT_DIFF')
    elif info['native_bounds'] is not None:raise BridgeError('NATIVE_MSD_EXTENT_DIFF')
    return {'member_count':len(members),'native_bounds':union,'images':[{**image,**{k:v for k,v in member.items() if k not in {'event','name'}}} for image,member in zip(images,members)]}


def native_msd_info(root,file,expected_sha256,expected_members,query_bounds=None):
    info,sources=_bound(file,expected_sha256,expected_members)
    if query_bounds is not None and (not isinstance(query_bounds,list) or len(query_bounds)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in query_bounds) or query_bounds[0]>query_bounds[2] or query_bounds[1]>query_bounds[3]):raise BridgeError('MSD_QUERY_BOUNDS')
    folder=Path(root)/'work'/('msd-inspect-'+uuid.uuid4().hex);folder.mkdir(parents=True)
    images=[{**image,'name':f'image-{i:04d}.msi'} for i,image in enumerate(info['images'],1)]
    for image in images:_copy_image(image,folder,image['name'])
    _index(folder/'input.msd',[v['name'] for v in images]);host,events=_invoke(root,folder,'inspect')
    result=_result(events,images);_sources_unchanged(sources)
    if any(file_sha(folder/v['name'])!=v['expected_sha256'] for v in images):raise BridgeError('NATIVE_MSD_MEMBER_CHANGED')
    if query_bounds is not None:
        a,b,c,d=query_bounds;selected=[v['index'] for v in result['images'] if v['native_bounds'][0]<=c and v['native_bounds'][2]>=a and v['native_bounds'][1]<=d and v['native_bounds'][3]>=b]
        result['query']={'native_bounds':query_bounds,'member_indexes':selected,'predicate':'boundary-inclusive axis-aligned envelope intersection; not footprint or pixel content'}
    result.update(file=info['file'],sha256=expected_sha256,native_execution=host,source_preserved=True,coordinate_frame=info['coordinate_frame'],
        project_layers=[{'file':v['file'],'expected_sha256':v['expected_sha256']} for v in images])
    return result


def _prepare(bridge,info,adds,removes,parameters,request_id,operation,sources):
    p=parameters if isinstance(parameters,MsdParameters) else MsdParameters.model_validate(parameters)
    if Path(p.name).name!=p.name or Path(p.name).suffix!='.msd' or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,55}\.msd',p.name):raise BridgeError('MSD_OUTPUT_NAME: lowercase ASCII .msd filename, at most 60 bytes')
    originals=info['images'] if info else []
    if len(originals)+len(adds)>1000:raise BridgeError('MSD_MEMBER_LIMIT')
    if any(type(i) is not int or not 1<=i<=len(originals) for i in removes) or len(set(removes))!=len(removes):raise BridgeError('MSD_REMOVE_INDEX: unique existing one-based positions required')
    old=[{**v,'name':f'image-{i:04d}.msi'} for i,v in enumerate(originals,1)]
    new=[{**v,'name':f'image-{i+len(old):04d}.msi'} for i,v in enumerate(adds,1)]
    output=[v for i,v in enumerate(old,1) if i not in removes]+new;frame=_frame(output,p)
    mapping={str(i):None if i in removes else sum(j not in removes for j in range(1,i+1)) for i in range(1,len(old)+1)}
    executable,_=backend_configuration(bridge.root)
    if not sources:sources=[{'file':str(executable),'sha256':file_sha(executable)}]
    sources=list({s['file']:s for s in sources}.values())
    payload={'operation':operation,'parameters':p.model_dump(),'originals':old,'additions':new,'remove_indexes':removes,
        'result_images':output,'coordinate_frame':frame,'source_index_to_output_index':mapping,'msd_protocol':1}
    stage,receipt,created=begin_bundle(bridge,payload,request_id,sources,p.name)
    if not created:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        native=stage/'native';native.mkdir()
        for image in old+new:_copy_image(image,native,image['name'])
        if info:_index(native/'input.msd',[v['name'] for v in old])
        control=native/'changes.txt';control.write_text(f'{len(new)} {len(removes)}\n'+''.join(f'{i-1}\n' for i in removes)+''.join(v['name']+'\n' for v in new),encoding='ascii')
        execution,events=_invoke(bridge.root,native,'edit' if info else 'create',control=control.name)
        return finish_native_msd(bridge,request_id,stage,payload,execution,events)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def prepare_native_msd(bridge,images,parameters,request_id):
    sources=[];bound=[]
    for value in images:
        v=MsiSource.model_validate(value).model_dump();image,dependencies=_msi(v['file'],v['expected_sha256'],v['expected_witness_sha256'],True)
        bound.append(image);sources+=dependencies
    return _prepare(bridge,None,bound,[],parameters,request_id,'native.msd.create',sources)


def prepare_msd_edit(bridge,file,expected_sha256,expected_members,remove_indexes,add_images,parameters,request_id):
    info,sources=_bound(file,expected_sha256,expected_members);bound=[]
    for value in add_images:
        v=MsiSource.model_validate(value).model_dump();image,dependencies=_msi(v['file'],v['expected_sha256'],v['expected_witness_sha256'],True)
        bound.append(image);sources+=dependencies
    return _prepare(bridge,info,bound,remove_indexes,parameters,request_id,'native.msd.edit',sources)


def finish_native_msd(bridge,request_id,stage,payload,execution,events,recovered=False):
    candidate=stage/'native/result.msd';saved=next((e for e in events if e['event']=='msd_saved'),None)
    if not saved or file_sha(candidate)!=saved['sha256']:raise BridgeError('NATIVE_MSD_SAVE_DIFF')
    expected=payload['result_images'];first=_result(events,expected)
    fresh=stage/('reopen-'+uuid.uuid4().hex[:8]);fresh.mkdir()
    for image in expected:
        source=stage/'native'/image['name']
        if file_sha(source)!=image['expected_sha256']:raise BridgeError('NATIVE_MSD_MEMBER_CHANGED')
        shutil.copyfile(source,fresh/image['name'])
    shutil.copyfile(candidate,fresh/'input.msd');reopen,actual=_invoke(bridge.root,fresh,'inspect')
    result=_result(actual,expected)
    if any(file_sha(fresh/v['name'])!=v['expected_sha256'] for v in expected):raise BridgeError('NATIVE_MSD_MEMBER_CHANGED')
    if first!=result:raise BridgeError('NATIVE_MSD_REOPEN_DIFF')
    with bridge.connect() as db:receipt=json.loads(db.execute('SELECT receipt FROM commands WHERE id=?',(request_id,)).fetchone()['receipt'])
    _sources_unchanged(receipt['sources'])
    files=stage/'files';files.mkdir(exist_ok=recovered);target=files/payload['parameters']['name'];shutil.copyfile(candidate,target)
    for image in expected:_copy_image(image,files,image['name'])
    witness={'schema':'mapgis-agent/msd/1','native_sha256':saved['sha256'],'coordinate_frame':payload['coordinate_frame'],
        'source_index_to_output_index':payload['source_index_to_output_index'],'images':[{'index':i,**v} for i,v in enumerate(expected,1)]}
    (files/(target.name+'.json')).write_text(json_text(witness),encoding='utf8')
    return finish_bundle(bridge,request_id,files,{'backend':'MapGIS CMsiDB 6.7 public wrappers and MSI native extent reads',
        'native_execution':execution,'native_reopen':reopen,'member_count':result['member_count'],'native_bounds':result['native_bounds'],
        'images':[{'index':v['index'],'name':v['name'],'native_bounds':v['native_bounds'],'sha256':v['expected_sha256']} for v in result['images']],
        'source_index_to_output_index':payload['source_index_to_output_index'],'coordinate_frame':payload['coordinate_frame'],
        'source_preserved':True,'portable_relative_paths':True,'replayed':False,'reconciled_after_interruption':recovered})


def reconcile_native_msd(bridge,request_id):
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row);stage=Path(receipt['stage'])
    if row['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
    try:
        process=psutil.Process(owner['pid'])
        if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
    except psutil.NoSuchProcess:pass
    result=completed_native_child(stage/'native','native-msd.json')
    if result is None:raise BridgeError('ARTIFACT_INCOMPLETE: no MSD operation was replayed')
    execution,events=result
    return finish_native_msd(bridge,request_id,stage,receipt['payload'],execution,events,True)
