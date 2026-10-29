"""Portable native MPJ bundles built and reopened by the installed project DLL."""
from __future__ import annotations

from pathlib import Path
import json
import shutil
import uuid

from .core import Bridge,BridgeError,read_native
from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .native_backend import backend_configuration,_run_child,KINDS
from .raster import file_sha


def _project_call(executable,bas,mode,source,target,log_dir):
    log_dir.mkdir(parents=True)
    execution,events=_run_child([str(executable),'project',mode,str(bas.parent),str(source),str(target)],log_dir,'project.json')
    layers=[e for e in events if e['event']=='project_layer']
    for layer in layers:layer['file']=bytes.fromhex(layer.pop('path_gbk_hex')).decode('gbk')
    if not layers or not any(e['event']=='project_closed' for e in events):raise BridgeError('NATIVE_PROJECT_INCOMPLETE')
    return execution,layers


def native_project_info(root:Path,file:str,expected_sha256:str)->dict:
    executable,bas=backend_configuration(root)
    source=Path(file).resolve(strict=True)
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if source.suffix.lower()!='.mpj':raise BridgeError('NATIVE_PROJECT_MPJ_REQUIRED')
    folder=Bridge(root).destination_path(Path(root)/'work/native-project-info'/uuid.uuid4().hex)
    execution,layers=_project_call(executable,bas,'inspect',source,'-',folder)
    for layer in layers:
        stored=layer['file'];path=Path(stored)
        if not path.is_absolute():path=source.parent/path
        path=path.resolve(strict=True)
        layer.update(stored_file=stored,file=str(path),sha256=file_sha(path))
    if file_sha(source)!=expected_sha256:raise BridgeError('STALE_DURING_PROJECT_READ')
    return {'file':str(source),'source_sha256':expected_sha256,'layers':layers,'native_execution':execution,
        'scope':'saved MPJ in an owned inspection process; no edit/save call; external editor state is separate'}


def prepare_native_project(bridge:Bridge,layers:list[dict],parameters:dict,request_id:str)->dict:
    if set(parameters)-{'name','coordinate_frame'} or parameters.get('coordinate_frame')!='native':raise BridgeError('NATIVE_PROJECT_FRAME: explicitly declare native coordinate frame')
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
    payload={'operation':'native.project.create','layers':layers,'parameters':parameters}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        executable,bas=backend_configuration(bridge.root);files=stage/'files';files.mkdir();names=[]
        for i,item in enumerate(expected,1):
            filename=f'layer-{i:03d}'+item['suffix'];names.append(filename);shutil.copyfile(item['file'],files/filename)
            if file_sha(files/filename)!=item['sha256']:raise BridgeError('STALE_SOURCE')
            if item.get('sidecar'):shutil.copyfile(item['sidecar'],files/(filename+'.json'))
        # The native factory resolves members relative to the list directory.
        listing=files/'layers.txt';listing.write_text(str(len(names))+'\n'+'\n'.join(names)+'\n',encoding='ascii')
        execution,created=_project_call(executable,bas,'create',listing,files/name,stage/'create')
        listing.unlink()  # Task-owned control file only; never a published member.
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
            'source_preserved':True,'coordinate_frame':'native; caller must establish common physical frame'})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise


def prepare_project_plot(bridge:Bridge,file:str,expected_sha256:str,view:dict,request_id:str)->dict:
    """Render a portable MPJ's native vector and MSI layers into EPS plus preview."""
    from .native_plot import _view,_ghostscript,_render_check,validate_eps
    view=_view(view);source=Path(file).resolve(strict=True)
    info=native_project_info(bridge.root,str(source),expected_sha256)
    sources=[{'file':str(source),'sha256':expected_sha256}];names={source.name.casefold()}
    for layer in info['layers']:
        path=Path(layer['file'])
        if path.parent!=source.parent:raise BridgeError('PORTABLE_PROJECT_REQUIRED: create a project bundle with sibling dependencies first')
        if path.name.casefold() in names:raise BridgeError('PROJECT_DEPENDENCY_COLLISION')
        names.add(path.name.casefold());sources.append({'file':str(path),'sha256':layer['sha256']})
    payload={'operation':'native.project.plot','file':str(source),'expected_sha256':expected_sha256,'view':view}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,'map.eps')
    if not new:return receipt
    try:
        executable,bas=backend_configuration(bridge.root);project=stage/'project';project.mkdir()
        for member in sources:
            destination=project/Path(member['file']).name;shutil.copyfile(member['file'],destination)
            if file_sha(destination)!=member['sha256']:raise BridgeError('STALE_SOURCE')
        settings=stage/'view.txt';settings.write_text(' '.join(repr(v) for v in view['native_bounds']+view['paper_mm'])+'\n',encoding='ascii')
        files=stage/'files';files.mkdir();logs=stage/'native';logs.mkdir()
        execution,events=_run_child([str(executable),'project','plot',str(bas.parent),str(project/source.name),
            str(settings),str(files/'map.eps')],logs,'project-plot.json')
        drawn=[e for e in events if e['event']=='layer_drawn']
        if len(drawn)!=len(info['layers']) or any(e['index']!=i or e['kind']!=layer['kind'] or e['records']!=layer['active'] for i,(e,layer) in enumerate(zip(drawn,info['layers']))):raise BridgeError('PROJECT_PLOT_LAYER_MISMATCH')
        eps=validate_eps(files/'map.eps',view['paper_mm']);render=_render_check(_ghostscript(bridge.root),files,files/'map.eps',view['paper_mm'])
        # The interpreter log stays diagnostic evidence; the output set contains
        # the actual EPS and its independently rendered, portable preview.
        (files/'ghostscript.json').rename(stage/'ghostscript.json')
        render['preview']=str(Path(receipt['output']).with_name('preview.png'))
        return finish_bundle(bridge,request_id,files,{'backend':'MapGIS mapprj60/mapdsp60/winout60','native_execution':execution,
            'view':view,'eps':eps,'render_check':render,'source_preserved':True,'layers':info['layers']})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
