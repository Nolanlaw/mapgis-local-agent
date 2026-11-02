"""Portable native MPJ bundles built and reopened by the installed project DLL."""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import uuid
import math
import os
import psutil
from contextlib import ExitStack

from .core import Bridge,BridgeError,read_native,json_text,sha
from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .native_backend import backend_configuration,_run_child,KINDS,completed_native_child
from .native_paths import NativePathAlias
from .raster import file_sha
from .symbol_library import freeze_system_library,system_library_info


def _project_library(source:Path,expected_sha256:str):
    """Bind sibling libraries to the exact MPJ, without producer-specific paths."""
    sidecar=source.with_name(source.name+'.agent.json')
    if not sidecar.is_file():return None,[]
    if sidecar.is_symlink():raise BridgeError('PROJECT_LIBRARY_SIDECAR')
    content=json.loads(sidecar.read_text(encoding='utf8'))
    if (set(content)!={'version','native_sha256','library_manifest_sha256'} or content['version']!=1
            or content['native_sha256']!=expected_sha256):raise BridgeError('PROJECT_LIBRARY_SIDECAR_MISMATCH')
    binding={'folder':str(source.parent),'expected_manifest_sha256':content['library_manifest_sha256']}
    info,sources=freeze_system_library(binding)
    return info,sources+[{'file':str(sidecar),'sha256':file_sha(sidecar),'role':'project_library_manifest'}]


def _copy_project_library(info,files:Path,name:str):
    if info is None:return
    for member in info['members']:
        destination=files/member['name']
        if destination.exists():raise BridgeError('PROJECT_LIBRARY_NAME_COLLISION')
        shutil.copyfile(Path(info['folder'])/member['name'],destination)
        if file_sha(destination)!=member['sha256']:raise BridgeError('STALE_LIBRARY')
    system_library_info(str(files),info['manifest_sha256'])
    (files/(name+'.agent.json')).write_text(json.dumps({'version':1,'native_sha256':file_sha(files/name),
        'library_manifest_sha256':info['manifest_sha256']},ensure_ascii=False,sort_keys=True),encoding='utf8')


def _project_call(executable,bas,mode,source,target,log_dir,control=None):
    log_dir.mkdir(parents=True)
    source=Path(source)
    with NativePathAlias(source.parent,record_folder=log_dir) as alias:
        destination='-' if str(target)=='-' else str(alias.path/Path(target).name)
        if str(target)!='-' and Path(target).parent.resolve()!=source.parent.resolve():
            raise BridgeError('PROJECT_OUTPUT_FOLDER')
        args=[str(executable),'project',mode,str(bas.parent),str(alias.path/source.name),destination]
        if control is not None:
            if Path(control).parent.resolve()!=source.parent.resolve():raise BridgeError('PROJECT_CONTROL_FOLDER')
            args.append(str(alias.path/Path(control).name))
        execution,events=_run_child(args,log_dir,'project.json')
    layers=[e for e in events if e['event']=='project_layer']
    states={e['index']:e for e in events if e['event']=='project_state'}
    bounds={e['index']:e['native_bounds'] for e in events if e['event']=='project_layer_bounds'}
    for layer in layers:
        layer['file']=bytes.fromhex(layer.pop('path_gbk_hex')).decode('gbk')
        state=states.get(layer['index'])
        if state is None:raise BridgeError('NATIVE_PROJECT_STATE_MISSING')
        layer.update(state=state['state'],display_limits=state['display_limits'])
        if layer['index'] in bounds:layer['native_bounds']=bounds[layer['index']]
    if not layers or not any(e['event']=='project_closed' for e in events):raise BridgeError('NATIVE_PROJECT_INCOMPLETE')
    execution['native_bounds']=next((e['native_bounds'] for e in events if e['event']=='project_bounds'),None)
    environment=next((e for e in events if e['event']=='project_environment'),None)
    if environment is None:raise BridgeError('NATIVE_PROJECT_ENVIRONMENT_MISSING')
    execution['environment']={k:v for k,v in environment.items() if k!='event'}
    return execution,layers


def native_project_info(root:Path,file:str,expected_sha256:str)->dict:
    executable,bas=backend_configuration(root)
    source=Path(file).resolve(strict=True)
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if source.suffix.lower()!='.mpj':raise BridgeError('NATIVE_PROJECT_MPJ_REQUIRED')
    library,_=_project_library(source,expected_sha256)
    folder=Bridge(root).destination_path(Path(root)/'work/native-project-info'/uuid.uuid4().hex)
    execution,layers=_project_call(executable,bas,'inspect',source,'-',folder)
    for layer in layers:
        stored=layer['file'];path=Path(stored)
        if not path.is_absolute():path=source.parent/path
        path=path.resolve(strict=True)
        layer.update(stored_file=stored,file=str(path),sha256=file_sha(path))
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_DURING_PROJECT_READ')
    return {'file':str(source),'source_sha256':expected_sha256,'layers':layers,'native_bounds':execution['native_bounds'],
        'environment':execution['environment'],'native_execution':execution,
        'system_library':None if library is None else {'folder':library['folder'],'expected_manifest_sha256':library['manifest_sha256']},
        'scope':'saved MPJ in an owned inspection process; no edit/save call; external editor state is separate'}


def prepare_native_project(bridge:Bridge,layers:list[dict],parameters:dict,request_id:str)->dict:
    if set(parameters)-{'name','coordinate_frame','system_library'} or parameters.get('coordinate_frame')!='native':raise BridgeError('NATIVE_PROJECT_FRAME: explicitly declare native coordinate frame')
    if not isinstance(layers,list) or not 1<=len(layers)<=100:raise BridgeError('NATIVE_PROJECT_LAYERS')
    name=parameters.get('name','map.mpj')
    if Path(name).suffix.lower()!='.mpj':raise BridgeError('NATIVE_PROJECT_MPJ_REQUIRED')
    sources=[];expected=[]
    for layer in layers:
        if set(layer)!={'file','expected_sha256'}:raise BridgeError('NATIVE_PROJECT_LAYER_PARAMETERS')
        path=Path(layer['file']).resolve(strict=True)
        sources.append({'file':str(path),'sha256':layer['expected_sha256']})
        if path.suffix.lower()=='.msi':
            item={'kind':11,'active':1,'suffix':'.msi'}
            companion=path.with_name(path.name+'.json')
            if companion.is_file():
                witness=json.loads(companion.read_text(encoding='utf8'))
                if witness.get('native_sha256')!=layer['expected_sha256']:raise BridgeError('MSI_SIDECAR_MISMATCH')
                item['sidecar']=str(companion);sources.append({'file':str(companion),'sha256':file_sha(companion)})
        else:
            _,reader,_=read_native(path);item={'kind':KINDS[reader.shapeType],'active':len(reader.geom),'suffix':path.suffix.lower()}
        expected.append(item|{'file':str(path),'sha256':layer['expected_sha256']})
    from .multimedia import project_media_dependencies,copy_media_dependencies
    media=project_media_dependencies(expected);sources+=media
    library,library_sources=freeze_system_library(parameters.get('system_library'));sources+=library_sources
    payload={'operation':'native.project.create','layers':layers,'parameters':parameters}
    if media:payload['media_dependencies']=media
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        executable,bas=backend_configuration(bridge.root);files=stage/'files';files.mkdir();names=[]
        for i,item in enumerate(expected,1):
            filename=f'layer-{i:03d}'+item['suffix'];names.append(filename);shutil.copyfile(item['file'],files/filename)
            if file_sha(files/filename)!=item['sha256']:raise BridgeError('STALE_SOURCE')
            if item.get('sidecar'):shutil.copyfile(item['sidecar'],files/(filename+'.json'))
        copy_media_dependencies(media,files)
        # The native factory resolves members relative to the list directory.
        listing=files/'layers.txt';listing.write_text(str(len(names))+'\n'+'\n'.join(names)+'\n',encoding='ascii')
        execution,created=_project_call(executable,bas,'create',listing,files/name,stage/'create')
        listing.unlink()  # Task-owned control file only; never a published member.
        _copy_project_library(library,files,name)
        relocated=stage/'relocated';shutil.copytree(files,relocated)
        hidden=stage/'candidate-hidden'
        if files.resolve().parent!=stage or hidden.resolve().parent!=stage:raise BridgeError('PROJECT_RELOCATION_SCOPE')
        files.rename(hidden)
        try:
            fresh,reopened=_project_call(executable,bas,'inspect',relocated/name,'-',stage/'reopen')
        finally:hidden.rename(files)
        if execution['pid']==fresh['pid'] or len(reopened)!=len(expected) or any(
            a['kind']!=b['kind'] or a['active']!=b['active'] for a,b in zip(reopened,expected)):
            raise BridgeError('NATIVE_PROJECT_REOPEN_MISMATCH')
        return finish_bundle(bridge,request_id,files,{'backend':'MapGIS mapprj60.dll exact build','native_execution':execution,
            'native_reopen':fresh,'layers':reopened,'portable_relocation':'PASS_WITH_OLD_DIRECTORY_HIDDEN',
            'source_preserved':True,'coordinate_frame':'native; caller must establish common physical frame',
            'system_library_manifest':None if library is None else library['manifest_sha256']})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise


def _environment(value,original):
    if value is None:return original
    if not isinstance(value,dict) or set(value)-{'translation','scale','rotation_degrees','paper_mm'}:
        raise BridgeError('PROJECT_ENVIRONMENT_PARAMETERS')
    result=original|value
    for key in ('translation','scale','paper_mm'):
        pair=result[key]
        if (not isinstance(pair,list) or len(pair)!=2 or
                any(type(v) not in (int,float) or not math.isfinite(v) for v in pair)):
            raise BridgeError('PROJECT_ENVIRONMENT_VALUES')
    angle=result['rotation_degrees']
    if type(angle) not in (int,float) or not math.isfinite(angle):raise BridgeError('PROJECT_ENVIRONMENT_ANGLE')
    if any(v<0.001 for v in result['scale']) or any(not 0<v<=2000 for v in result['paper_mm']):
        raise BridgeError('PROJECT_ENVIRONMENT_RANGE')
    return result


def prepare_project_edit(bridge:Bridge,file:str,expected_sha256:str,layers:list[dict],request_id:str,environment:dict|None=None)->dict:
    """Edit a copied native MPJ, retaining its project/item metadata and sources.

    The complete ordered layer list uses source_index (one-based) or a new
    file/hash. Omitted source indexes are removed from the new project only.
    State is the native 0..3 value; source state is retained when omitted.
    """
    source=bridge.owned_path(file)
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if not isinstance(layers,list) or not 1<=len(layers)<=100:raise BridgeError('PROJECT_LAYERS')
    info=native_project_info(bridge.root,str(source),expected_sha256)
    library,library_sources=_project_library(source,expected_sha256)
    selected_environment=_environment(environment,info['environment'])
    sources=[{'file':str(source),'sha256':expected_sha256}]+library_sources
    members=[]
    for index,item in enumerate(info['layers'],1):
        members.append({**item,'name':f'input-{index:03d}'+Path(item['file']).suffix.lower()})
        sources.append({'file':item['file'],'sha256':item['sha256']})
    original_count=len(members)
    choices=[];seen=set();current=set()
    for position,choice in enumerate(layers,1):
        if not isinstance(choice,dict):raise BridgeError('PROJECT_LAYER_PARAMETERS')
        if 'source_index' in choice:
            index=choice['source_index']
            if (set(choice)-{'source_index','state','display_limits','replacement'} or type(index) is not int
                    or not 1<=index<=original_count or index in seen):raise BridgeError('PROJECT_SOURCE_INDEX')
            seen.add(index);member=members[index-1]
            if 'replacement' in choice:
                replacement=choice['replacement']
                if not isinstance(replacement,dict) or set(replacement)!={'file','expected_sha256'}:raise BridgeError('PROJECT_REPLACEMENT')
                path=Path(replacement['file']).resolve(strict=True)
                if file_sha(path)!=replacement['expected_sha256']:raise BridgeError('STALE_SOURCE')
                if member['kind']==11:
                    if path.suffix.lower()!='.msi':raise BridgeError('PROJECT_REPLACEMENT_KIND')
                    frames=[]
                    for image,fingerprint in [(Path(member['file']),member['sha256']),(path,replacement['expected_sha256'])]:
                        sidecar=image.with_name(image.name+'.json')
                        if sidecar.is_file():
                            witness=json.loads(sidecar.read_text(encoding='utf8'))
                            if witness.get('native_sha256')!=fingerprint:raise BridgeError('MSI_SIDECAR_MISMATCH')
                            frames.append(witness['raster'].get('crs_wkt') or witness['raster'].get('gcp_crs_wkt'))
                        else:frames.append(None)
                    from pyproj import CRS
                    if bool(frames[0])!=bool(frames[1]) or frames[0] and not CRS(frames[0]).equals(CRS(frames[1])):
                        raise BridgeError('PROJECT_REPLACEMENT_COORDINATE_FRAME')
                    active=1
                else:
                    raw,reader,_=read_native(path)
                    if KINDS[reader.shapeType]!=member['kind']:raise BridgeError('PROJECT_REPLACEMENT_KIND')
                    from pymapgis.reader import _read_crs
                    import io
                    old_crs,old_factor=_read_crs(io.BytesIO(Path(member['file']).read_bytes()))
                    new_crs,new_factor=_read_crs(io.BytesIO(raw))
                    from pyproj import CRS
                    if old_factor!=new_factor or bool(old_crs)!=bool(new_crs) or old_crs and not CRS(old_crs).equals(CRS(new_crs)):
                        raise BridgeError('PROJECT_REPLACEMENT_COORDINATE_FRAME')
                    active=len(reader.geom)
                member={**member,'file':str(path),'sha256':replacement['expected_sha256'],'active':active}
                members[index-1]=member
        else:
            if set(choice)-{'file','expected_sha256','state','display_limits'} or not {'file','expected_sha256'}<=set(choice):
                raise BridgeError('PROJECT_LAYER_PARAMETERS')
            path=Path(choice['file']).resolve(strict=True)
            if file_sha(path)!=choice['expected_sha256']:raise BridgeError('STALE_SOURCE')
            if path.suffix.lower()=='.msi':kind=11;active=1
            else:
                _,reader,_=read_native(path);kind=KINDS[reader.shapeType];active=len(reader.geom)
            member={'file':str(path),'sha256':choice['expected_sha256'],'kind':kind,'active':active,
                    'name':f'new-{position:03d}'+path.suffix.lower(),'state':1,'display_limits':[0,0]}
            members.append(member);index=0
        state=choice.get('state',member['state'])
        if type(state) is not int or not 0<=state<=3 or member['kind']==11 and state>1:
            raise BridgeError('PROJECT_NATIVE_STATE')
        if state==3:
            if member['kind'] in current:raise BridgeError('PROJECT_CURRENT_LAYER_CONFLICT')
            current.add(member['kind'])
        limits=choice.get('display_limits',member['display_limits'])
        if (not isinstance(limits,list) or len(limits)!=2
                or any(type(v) not in (int,float) or not math.isfinite(v) for v in limits)
                or limits[1]>0 and limits[0]>limits[1]):
            raise BridgeError('PROJECT_DISPLAY_LIMITS: finite [minimum,maximum] native drawing scales; maximum <=0 is unbounded')
        choices.append({**member,'source_index':index,'state':state,'display_limits':limits})
    # Every original dependency is cloned before native references change.
    # Only desired files enter the published bundle after the child has exited.
    source_keys={(item['file'],item['sha256']) for item in sources}
    for member in members:
        if (member['file'],member['sha256']) not in source_keys:
            sources.append({'file':member['file'],'sha256':member['sha256']});source_keys.add((member['file'],member['sha256']))
        companion=Path(member['file']).with_name(Path(member['file']).name+'.json')
        if member['kind']==11 and companion.is_file():
            witness=json.loads(companion.read_text(encoding='utf8'))
            if witness.get('native_sha256')!=member['sha256']:raise BridgeError('MSI_SIDECAR_MISMATCH')
            member['sidecar']=str(companion);sources.append({'file':str(companion),'sha256':file_sha(companion)})
    from .multimedia import project_media_dependencies,copy_media_dependencies
    media=project_media_dependencies(choices);sources+=media
    name=source.name
    payload={'operation':'native.project.edit','file':str(source),'expected_sha256':expected_sha256,'layers':layers,'environment':environment}
    if media:payload['media_dependencies']=media
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        executable,bas=backend_configuration(bridge.root);files=stage/'files';files.mkdir()
        snapshot=files/('_snapshot-'+uuid.uuid4().hex+'.mpj');shutil.copyfile(source,snapshot)
        for member in members:
            target=files/member['name'];shutil.copyfile(member['file'],target)
            if file_sha(target)!=member['sha256']:raise BridgeError('STALE_SOURCE')
            if member.get('sidecar'):shutil.copyfile(member['sidecar'],files/(member['name']+'.json'))
        copy_media_dependencies(media,files)
        listing=files/'edit.txt';listing.write_text(str(original_count)+'\n'+
            '\n'.join(m['name'] for m in members[:original_count])+'\n'+str(len(choices))+'\n'+
            '\n'.join(f"{c['source_index']} {c['state']} {c['name']} {c['display_limits'][0]!r} {c['display_limits'][1]!r}" for c in choices)+'\nenvironment '+
            ' '.join(repr(v) for v in selected_environment['translation']+selected_environment['scale']+
                     [selected_environment['rotation_degrees']]+selected_environment['paper_mm'])+'\n',encoding='ascii')
        execution,created=_project_call(executable,bas,'edit',snapshot,files/name,stage/'edit',listing)
        snapshot.unlink();listing.unlink()
        desired={c['name'] for c in choices}
        for member in members:
            if member['name'] not in desired:
                (files/member['name']).unlink()
                if member.get('sidecar'):(files/(member['name']+'.json')).unlink()
        for choice in choices:
            if file_sha(files/choice['name'])!=choice['sha256']:raise BridgeError('PROJECT_LAYER_BYTES_CHANGED')
        _copy_project_library(library,files,name)
        relocated=stage/'relocated';shutil.copytree(files,relocated);hidden=stage/'candidate-hidden'
        files.rename(hidden)
        try:fresh,reopened=_project_call(executable,bas,'inspect',relocated/name,'-',stage/'reopen')
        finally:hidden.rename(files)
        if (execution['environment']!=selected_environment or fresh['environment']!=selected_environment
                or execution['pid']==fresh['pid'] or len(reopened)!=len(choices) or any(
            a['kind']!=b['kind'] or a['active']!=b['active'] or a['state']!=b['state'] or a['display_limits']!=b['display_limits'] or a['file'].casefold()!=b['name'].casefold()
            for a,b in zip(reopened,choices))):
            raise BridgeError('PROJECT_EDIT_REOPEN_MISMATCH')
        return finish_bundle(bridge,request_id,files,{'backend':'MapGIS mapprj60 exact build',
            'native_execution':execution,'native_reopen':fresh,'layers':reopened,'environment':fresh['environment'],
            'layer_sources':[{'index':i,'source_index':c['source_index'],'file':c['file'],'sha256':c['sha256']}
                             for i,c in enumerate(choices,1)],
            'portable_relocation':'PASS_WITH_OLD_DIRECTORY_HIDDEN','source_preserved':True,
            'scope':'saved MPJ and copied dependencies; external GUI state is independent'})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise


def finish_project_plot(bridge,request_id,stage,payload,execution,events,recovered=False):
    """Finish the existing native PS; only its standard-format conversion can repeat."""
    from .native_plot import _ghostscript,_render_check,validate_eps,normalize_native_ps
    from .symbol_library import system_library_info
    context_file=stage/'plot-context.json'
    if file_sha(context_file)!=payload['context_sha256']:raise BridgeError('PLOT_RECOVERY_CONTEXT_CHANGED')
    context=json.loads(context_file.read_text(encoding='utf8'));info=context['info'];library=context['library']
    receipt=bridge.status(request_id)
    for member in receipt['sources']:
        if file_sha(Path(member['file']))!=member['sha256']:raise BridgeError('STALE_SOURCE')
        if member.get('role')!='system_library' and file_sha(stage/'project'/member.get('bundle_name',Path(member['file']).name))!=member['sha256']:
            raise BridgeError('PLOT_PROJECT_SNAPSHOT_CHANGED')
    if library:system_library_info(str(stage/'system-library'),library['manifest_sha256'])
    view=payload['view'];settings=' '.join(repr(v) for v in view['native_bounds']+view['paper_mm'])+'\n'
    if (stage/'view.txt').read_text(encoding='ascii')!=settings:raise BridgeError('PLOT_VIEW_SNAPSHOT_CHANGED')
    drawn=[e for e in events if e.get('event')=='layer_drawn']
    display_scale=min(view['paper_mm'][i]/(view['native_bounds'][i+2]-view['native_bounds'][i]) for i in range(2))*info['environment']['scale'][0]
    visible=[(i,layer) for i,layer in enumerate(info['layers']) if layer['state']!=0 and
             (all(v<=0 for v in layer['display_limits']) or display_scale>=layer['display_limits'][0] and
              (layer['display_limits'][1]<=0 or display_scale<=layer['display_limits'][1]))]
    if len(drawn)!=len(visible) or any(e['index']!=i or e['kind']!=layer['kind'] or e['records']!=layer['active']
        for e,(i,layer) in zip(drawn,visible)):raise BridgeError('PROJECT_PLOT_LAYER_MISMATCH')
    postscript=stage/'map.ps';saved=[e for e in events if e.get('event')=='plot_saved']
    if len(saved)!=1 or saved[0]['sha256']!=file_sha(postscript):raise BridgeError('PLOT_POSTSCRIPT_CHANGED')
    files=bridge.destination_path(stage/'files')
    if files.exists():
        if not recovered:raise BridgeError('PLOT_FILES_ALREADY_EXIST')
        retained=bridge.destination_path(stage/('incomplete-files-'+uuid.uuid4().hex[:8]))
        if files.parent!=stage.resolve() or retained.parent!=stage.resolve():raise BridgeError('PLOT_STAGE_IDENTITY')
        files.rename(retained)
    files.mkdir();diagnostics=bridge.destination_path(stage/('plot-check-'+uuid.uuid4().hex[:8]));diagnostics.mkdir()
    normalization=normalize_native_ps(_ghostscript(bridge.root),diagnostics,postscript,files/'map.eps',view['paper_mm'])
    eps=validate_eps(files/'map.eps',view['paper_mm']);render=_render_check(_ghostscript(bridge.root),files,files/'map.eps',view['paper_mm'])
    (files/'ghostscript.json').rename(diagnostics/'ghostscript.json')
    render['preview']=str(Path(receipt['output']).with_name('preview.png'))
    if any(file_sha(Path(s['file']))!=s['sha256'] for s in receipt['sources']):raise BridgeError('STALE_SOURCE')
    return finish_bundle(bridge,request_id,files,{'backend':'MapGIS mapprj60/mapdsp60/winout60','native_execution':execution,
        'view':view,'native_display_scale':display_scale,'environment':info['environment'],'eps':eps,'render_check':render,'normalization':normalization,
        'system_library':None if library is None else {'folder':library['folder'],'manifest_sha256':library['manifest_sha256']},
        'source_preserved':True,'layers':info['layers'],'replayed':False,'reconciled_after_interruption':recovered,
        'native_postscript_sha256':saved[0]['sha256']})


def reconcile_project_plot(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage'])
    if receipt['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if 'context_sha256' not in receipt['payload']:
        raise BridgeError('PLOT_RECOVERY_CONTEXT_MISSING: retain the old request; no native plot was repeated')
    if receipt['state']=='RUNNING':
        owner=json.loads((stage/'producer.json').read_text(encoding='utf8'))
        try:
            process=psutil.Process(owner['pid'])
            if abs(process.create_time()-owner['start'])<.01 and process.exe()==owner['executable']:raise BridgeError('ARTIFACT_PRODUCER_RUNNING')
        except psutil.NoSuchProcess:pass
    complete=completed_native_child(stage/'native','project-plot.json')
    if complete is None:raise BridgeError('ARTIFACT_INCOMPLETE: original native plot is still running; no call repeated')
    return finish_project_plot(bridge,request_id,stage,receipt['payload'],*complete,recovered=True)


def _plot_inputs(root,file,expected_sha256,system_library=None):
    """One dependency/library contract for both single pages and map series."""
    source=Path(file).resolve(strict=True)
    info=native_project_info(root,str(source),expected_sha256)
    _,sidecar_sources=_project_library(source,expected_sha256)
    if system_library is None:system_library=info.get('system_library')
    sources=[{'file':str(source),'sha256':expected_sha256}];names={source.name.casefold()}
    for layer in info['layers']:
        path=Path(layer['file'])
        if path.parent!=source.parent:raise BridgeError('PORTABLE_PROJECT_REQUIRED: create a project bundle with sibling dependencies first')
        if path.name.casefold() in names:raise BridgeError('PROJECT_DEPENDENCY_COLLISION')
        names.add(path.name.casefold());sources.append({'file':str(path),'sha256':layer['sha256']})
    from .multimedia import project_media_dependencies
    sources+=project_media_dependencies(info['layers'])
    library,library_sources=freeze_system_library(system_library)
    sources+=library_sources+[s for s in sidecar_sources if s.get('role')=='project_library_manifest']
    return source,info,library,sources


def prepare_project_plot(bridge:Bridge,file:str,expected_sha256:str,view:dict,request_id:str,system_library:dict|None=None)->dict:
    """Render a portable MPJ's native vector and MSI layers into EPS plus preview."""
    from .native_plot import _view
    from .symbol_library import snapshot_system_library
    view=_view(view);source,info,library,sources=_plot_inputs(bridge.root,file,expected_sha256,system_library)
    if system_library is None:system_library=info.get('system_library')
    context=json_text({'info':{k:v for k,v in info.items() if k!='native_execution'},'library':library})
    payload={'operation':'native.project.plot','file':str(source),'expected_sha256':expected_sha256,'view':view,'system_library':system_library,
             'plot_protocol':'native_level2_ps_lossless_recoverable','context_sha256':sha(context.encode('utf8'))}
    media=[s for s in sources if 'bundle_name' in s]
    if media:payload['media_dependencies']=media
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,'map.eps')
    if not new:return receipt
    try:
        owner=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':owner.create_time(),'executable':owner.exe()}),encoding='utf8')
        (stage/'plot-context.json').write_text(context,encoding='utf8')
        executable,bas=backend_configuration(bridge.root);project=stage/'project';project.mkdir()
        for member in sources:
            if member.get('role')=='system_library':continue
            destination=project/member.get('bundle_name',Path(member['file']).name)
            destination.parent.mkdir(parents=True,exist_ok=True)
            if destination.exists() and file_sha(destination)!=member['sha256']:raise BridgeError('PROJECT_DEPENDENCY_COLLISION')
            shutil.copyfile(member['file'],destination)
            if file_sha(destination)!=member['sha256']:raise BridgeError('STALE_SOURCE')
        settings=stage/'view.txt';settings.write_text(' '.join(repr(v) for v in view['native_bounds']+view['paper_mm'])+'\n',encoding='ascii')
        logs=stage/'native';logs.mkdir()
        with ExitStack() as stack:
            alias=stack.enter_context(NativePathAlias(stage))
            args=[str(executable),'project','plot.ps',str(bas.parent),str(alias.path/'project'/source.name),str(alias.path/'view.txt'),str(alias.path/'map.ps')]
            if library:
                snapshot_system_library(library,stage);args.append(str(alias.path/'system-library'))
            execution,events=_run_child(args,logs,'project-plot.json')
        return finish_project_plot(bridge,request_id,stage,payload,execution,events)
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
