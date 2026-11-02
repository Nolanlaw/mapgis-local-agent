"""Original scalar-volume drawing with guarded saved-layer adoption."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Literal
import numpy as np
import psutil
from pydantic import BaseModel,ConfigDict,Field

from .artifacts import fail_bundle,finish_bundle,reconcile_bundle
from .core import BridgeError,json_text,sha
from .raster import file_sha
from .volume import (VolumeFrame,_name,_source,_unchanged,_start,_checkpoint,
    _save_candidate,_frozen_candidate,_verify_original,_native_route_required,encode_3dm,read_3dm)
from .volume_native import EXE_SHA,VIEW_COMMANDS,VIEW_PLANES,native_volume_render


class NativePlane(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    position_m:float
    max_error_m:float=Field(ge=0,description='Maximum difference between the requested plane and the actual source grid-node plane reached through the legacy0.1m slider. No implicit interpolation.')


class VolumePlotParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    frame:VolumeFrame
    view:Literal['full','stereo_x','stereo_y','stereo_z','flat_x','flat_y','flat_z','block_1','block_2','block_3','scalar_height_z','multilayer']='full'
    planes:dict[Literal['x','y','z'],NativePlane]|None=None
    layers:list[NativePlane]|None=Field(default=None,min_length=2,max_length=100,
        description='Multilayer-only strictly increasing source-Z positions and explicit per-layer error budgets. Actual source grid-node planes must remain distinct. Native display sorts by increasing native elevation, so depth_down draws deeper source layers first.')
    max_coordinate_error_m:float|None=Field(default=None,ge=0,
        description='Required for depth_down: explicit float32 coordinate budget when reversing/negating Z on a task copy. Values are permuted without interpolation.')


def _render_input(source,target,frame,error_budget):
    axes,values,info=read_3dm(source)
    if frame.z_axis=='elevation_up':
        shutil.copyfile(source,target)
        return {'native_z':'elevation in metres; positive up','transform':'identity; exact source bytes',
            'max_coordinate_error_m':0.,'max_value_error':0.}
    if error_budget is None:raise BridgeError('VOLUME_PLOT_DEPTH_BUDGET: explicit max_coordinate_error_m required for the depth-to-up task copy')
    bounds=info['bounds'].copy();bounds[2],bounds[5]=-bounds[5],-bounds[2]
    blob,metrics=encode_3dm(values[::-1],bounds,error_budget,0.)
    target.write_bytes(blob)
    _,check,_=read_3dm(target)
    if not np.array_equal(check,values[::-1]):raise BridgeError('VOLUME_PLOT_DEPTH_VALUES: reversal must preserve every scalar')
    return {'native_z':'negative depth in metres; positive up','transform':'native_z=-source_depth; reverse Z layer order; no resampling',
        'max_coordinate_error_m':metrics['max_coordinate_error_m'],'max_value_error':metrics['max_value_error']}


def _settings(source,render_input,p):
    if p.view=='full':
        if p.planes is not None:raise BridgeError('VOLUME_PLOT_PLANES: full view does not use plane parameters')
        if p.layers is not None:raise BridgeError('VOLUME_PLOT_LAYERS: layers only belong to multilayer view')
        return None
    required=set(VIEW_PLANES[p.view])
    if p.view=='multilayer':
        if p.planes is not None or p.layers is None or any(a.position_m>=b.position_m for a,b in zip(p.layers,p.layers[1:])):
            raise BridgeError('VOLUME_PLOT_LAYERS: provide2..100 strictly increasing source-Z layers and no single-plane parameters')
        requests=[('z',item) for item in p.layers]
    else:
        if p.layers is not None:raise BridgeError('VOLUME_PLOT_LAYERS: layers only belong to multilayer view')
        if p.planes is None or set(p.planes)!=required:raise BridgeError('VOLUME_PLOT_PLANES: provide exactly the required axis planes with explicit max_error_m')
        requests=list(p.planes.items())
    original_axes,_,_=read_3dm(source);native_axes,_,_=read_3dm(render_input);planes={}
    layers=[]
    for axis,item in requests:
        index='xyz'.index(axis);original=original_axes[index];native=native_axes[index]
        if not original[0]<=item.position_m<=original[-1]:raise BridgeError('VOLUME_NATIVE_PLANE_BOUNDS')
        # Every original view samples a source grid-node plane. An impossible
        # source-space budget is known before launching the private application.
        if float(np.min(np.abs(original-item.position_m)))>item.max_error_m:
            raise BridgeError('VOLUME_PLOT_PLANE_ERROR: no source grid-node plane is within the explicit position budget')
        negative=axis=='z' and p.frame.z_axis=='depth_down'
        original_native=-original[::-1] if negative else original
        linear=np.linspace(native[0],native[-1],len(native))
        encoding_difference=float(np.max(np.abs(linear-original_native)))
        setting={'minimum':float(native[0]),'maximum':float(native[-1]),'count':len(native),
            'position':-item.position_m if negative else item.position_m,'max_error':item.max_error_m+encoding_difference}
        if p.view=='multilayer':layers.append(setting)
        else:planes[axis]=setting
    return {'view':p.view,'planes':planes,**({'layers':layers} if p.view=='multilayer' else {})}


def _source_planes(stage,plan,record):
    settings=plan.get('native_settings')
    if settings is None:return []
    multi=settings['view']=='multilayer'
    selections=record.get('layer_planes' if multi else 'planes',[])
    if (multi and (len(selections)!=len(settings['layers']) or any(v['axis']!='z' for v in selections)
            or len({v['grid_index'] for v in selections})!=len(selections))) or (not multi and
            (len(selections)!=len(settings['planes']) or {v['axis'] for v in selections}!=set(settings['planes']))):
        raise BridgeError('VOLUME_PLOT_PLANE_READBACK')
    axes,_,_=read_3dm(Path(plan['source_file']));actual=[]
    for layer,selection in enumerate(selections):
        axis=selection['axis'];index=selection['grid_index'];values=axes['xyz'.index(axis)]
        negative=axis=='z' and plan['source_frame']['z_axis']=='depth_down'
        source_index=len(values)-1-index if negative else index
        if not 0<=source_index<len(values):raise BridgeError('VOLUME_PLOT_PLANE_INDEX')
        position=float(values[source_index]);requested=plan['layer_requests'][layer] if multi else plan['plane_requests'][axis]
        difference=abs(position-requested['position_m'])
        if difference>requested['max_error_m']:raise BridgeError('VOLUME_PLOT_PLANE_ERROR: actual source plane exceeds the explicit position budget')
        actual.append({'axis':axis,'requested_position_m':requested['position_m'],'actual_position_m':position,
            'source_grid_index':source_index,'error_m':difference,'max_error_m':requested['max_error_m'],
            'native_control':selection,'evidence':'original slider state plus pinned native grid-index code; source coordinates read independently',
            **({'layer':layer} if multi else {})})
    return actual


def _plan(bridge,request_id,stage):
    raw=(stage/'plot-plan.json').read_bytes()
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None or bridge._receipt(row).get('volume_plot_plan_sha256')!=sha(raw):raise BridgeError('VOLUME_PLOT_PLAN_CHANGED')
    plan=json.loads(raw);_unchanged(plan['sources'])
    target=stage/'render-input.3dm'
    if file_sha(target)!=plan['render_input_sha256']:raise BridgeError('VOLUME_PLOT_INPUT_CHANGED')
    return plan


def _alive(identity):
    try:
        process=psutil.Process(identity['pid'])
        return abs(process.create_time()-identity['start'])<1e-5 and process.exe()==identity['executable']
    except psutil.NoSuchProcess:return False
    except psutil.AccessDenied as exc:raise BridgeError('VOLUME_PLOT_PROCESS_IDENTITY_UNAVAILABLE') from exc


def _active_native(stage):
    for name in ('reader.started.json','reader.actual.json','application.started.json'):
        path=stage/'native'/name
        if path.exists() and _alive(json.loads(path.read_text(encoding='utf8'))):return True
    return False


def _render_record(stage,plan):
    native=stage/'native';path=native/'readback.json'
    recovered=not path.exists()
    if recovered:path=native/'render-complete.json'
    if not path.exists():raise BridgeError('VOLUME_PLOT_INCOMPLETE: no complete original save set; no native drawing command replayed')
    record=json.loads(path.read_text(encoding='utf8'))
    expected_command=VIEW_COMMANDS[plan.get('native_settings',{}).get('view','full')]
    if (record.get('action')!='render' or record.get('render_command')!=expected_command or record.get('palette')!='automatic'
            or record.get('source_sha256')!=plan['render_input_sha256']
            or record.get('snapshot_sha256')!=plan['render_input_sha256'] or record.get('executable_sha256')!=EXE_SHA
            or record.get('host_input_used') is not False or not record.get('private_desktop') or not record.get('source_preserved')):
        raise BridgeError('VOLUME_PLOT_RENDER_IDENTITY')
    if plan.get('native_settings_sha256') is not None:
        steps=record.get('render_steps',[])
        if (record.get('save_command')!=33004 or len(steps)!=2
                or [s.get('command') for s in steps]!=[VIEW_COMMANDS['full'],expected_command]
                or steps[1].get('before_title')!=steps[0].get('after_title')
                or any(s.get('before_title')==s.get('after_title') for s in steps)):
            raise BridgeError('VOLUME_PLOT_RENDER_ROUTE_UNVERIFIED: section must save the active new document, not the full-model cache')
        if (record.get('render_settings_sha256')!=plan['native_settings_sha256']
                or file_sha(native/'render-settings.json')!=plan['native_settings_sha256']):
            raise BridgeError('VOLUME_PLOT_SETTINGS_CHANGED')
    if _active_native(stage):raise BridgeError('VOLUME_PLOT_NATIVE_STILL_RUNNING')
    identity=json.loads((native/'application.started.json').read_text(encoding='utf8'))
    if identity['pid']!=record['pid']:raise BridgeError('VOLUME_PLOT_RENDER_PID')
    expected={'render.wt','render.wl','render.wp'}
    items=record.get('rendered_files',[])
    if len(items)!=3 or {item['name'] for item in items}!=expected:raise BridgeError('VOLUME_PLOT_RENDER_MEMBERS')
    for item in items:
        path=(native/item['name']).resolve(strict=True)
        if path.parent!=native.resolve() or file_sha(path)!=item['sha256']:raise BridgeError('VOLUME_PLOT_SAVED_LAYER_CHANGED')
    if file_sha(native/'input.3dm')!=plan['render_input_sha256']:raise BridgeError('VOLUME_PLOT_NATIVE_SNAPSHOT_CHANGED')
    record=record|{'own_process_ended':True}
    if recovered:record|={'completed_save_adopted':True,'owned_processes_verified_dead':True,'producing_algorithm_replayed':False}
    _,_,info=read_3dm(stage/'render-input.3dm');_verify_original(info,record)
    return record


def _adopt(bridge,request_id,stage):
    plan=_plan(bridge,request_id,stage);record=_render_record(stage,plan)
    primary=plan['name'];files=stage/'files';layers=[]
    for item in record['rendered_files']:
        suffix=Path(item['name']).suffix;name=str(Path(primary).with_suffix(suffix));source=stage/'native'/item['name'];target=files/name
        if target.exists():
            if file_sha(target)!=item['sha256']:raise BridgeError('VOLUME_PLOT_CANDIDATE_CHANGED')
        else:shutil.copyfile(source,target)
        layers.append({'name':name,'sha256':item['sha256'],'kind':suffix[1:]})
    if {p.name for p in files.iterdir()}!={v['name'] for v in layers}:raise BridgeError('VOLUME_PLOT_UNEXPECTED_MEMBER')
    metadata={'source_frame':plan['source_frame'],'render_transform':plan['render_transform'],
        'output_frame':{'kind':'original native2D projection of3D scalar data','crs':None,'unit':'native drawing units; not source XY map coordinates'},
        'layers':layers,'native_render':record,'backend':'original W60_Anly colour3D, automatic palette',
        'source_preserved':True,'scope':'editable projected WT/WL/WP; no closed solid, resource classification or proprietary solver equivalence inferred'}
    if plan.get('native_settings') is not None:
        metadata|={'view':plan['native_settings']['view'],'actual_planes':_source_planes(stage,plan,record)}
        if metadata['view']=='scalar_height_z':
            metadata['output_frame']['vertical_quantity']='Original drawing height represents the source scalar on the selected Z plane, not source elevation or depth; source value_unit remains in source_frame'
        if metadata['view']=='multilayer':
            metadata['layer_draw_order']=[v['layer'] for v in sorted(metadata['actual_planes'],key=lambda v:v['native_control']['actual_position'])]
    _save_candidate(bridge,request_id,stage,primary,metadata,plan['sources'])


def _seal(bridge,request_id,stage):
    candidate=_frozen_candidate(bridge,request_id,stage)
    validation=json.loads((stage/'plot-validation.json').read_text(encoding='utf8'))
    if validation['members']!=candidate['members']:raise BridgeError('VOLUME_PLOT_VALIDATION_IDENTITY')
    metadata=candidate['metadata']|{'native_readback':validation['layers'],'native_generation_replayed':False}
    primary=candidate['name'];target=stage/'files'/(primary+'.json')
    sidecar={'schema':'mapgis-agent/volume-plot/1','native_sha256':candidate['members'][primary],**metadata}
    encoded=json_text(sidecar)
    if target.exists() and target.read_text(encoding='utf8')!=encoded:raise BridgeError('VOLUME_PLOT_SIDECAR_CHANGED')
    if not target.exists():target.write_text(encoded,encoding='utf8')
    if {p.name for p in (stage/'files').iterdir()}!=set(candidate['members'])|{target.name}:raise BridgeError('VOLUME_PLOT_UNEXPECTED_MEMBER')
    return finish_bundle(bridge,request_id,stage/'files',metadata)


def _validate(bridge,request_id,stage):
    from .native_backend import native_file_info
    from .core import read_native
    candidate=_frozen_candidate(bridge,request_id,stage);validation=[]
    for layer in candidate['metadata']['layers']:
        file=stage/'files'/layer['name']
        native=native_file_info(bridge.root,str(file),layer['sha256'])
        _,reader,_=read_native(file)
        empty_wireframe=(candidate['metadata'].get('view')=='scalar_height_z' and layer['kind']=='wp'
            and native['active']==0 and len(reader.geom)==0)
        if (native['active']<=0 and not empty_wireframe) or not all(g.is_valid for g in reader.geom):
            raise BridgeError('VOLUME_PLOT_GEOMETRY: nonempty valid native layers required; only scalar-height wireframe permits a verified empty WP')
        if empty_wireframe:native|={'empty_layer_reason':'Original scalar-height drawing is a line mesh; native and independent readers both verify zero area records'}
        validation.append(native)
    _checkpoint(stage/'plot-validation.json',{'members':candidate['members'],'layers':validation})
    return _seal(bridge,request_id,stage)


def prepare_volume_plot(bridge,file,expected_sha256,parameters,template,request_id):
    p=parameters if isinstance(parameters,VolumePlotParameters) else VolumePlotParameters.model_validate(parameters)
    _native_route_required(bridge.root,{'native_reopen':True})
    name=_name(template,'.wp');path,sources,_=_source(file,expected_sha256,p.frame.model_dump())
    if path.suffix.lower()!='.3dm':raise BridgeError('VOLUME_PLOT_3DM_REQUIRED')
    stem=Path(name).stem.split('.')[0].upper()
    if stem in {'CON','PRN','AUX','NUL',*[f'{p}{n}' for p in ('COM','LPT') for n in range(1,10)]}:raise BridgeError('VOLUME_PLOT_TEMPLATE_NAME')
    omitted={'planes'} if p.view=='full' else set()
    if p.view!='multilayer':omitted.add('layers')
    serialized=p.model_dump(exclude=omitted)
    if p.view=='full' and p.planes is not None:raise BridgeError('VOLUME_PLOT_PLANES: full view does not use plane parameters')
    if p.view!='multilayer' and p.layers is not None:raise BridgeError('VOLUME_PLOT_LAYERS: layers only belong to multilayer view')
    payload={'operation':'volume.plot','file':str(path),'expected_sha256':expected_sha256,
        'parameters':serialized,'template':template,'native_plot_protocol':1,'native_executable_sha256':EXE_SHA}
    if p.view!='full':payload['native_view_protocol']=3 if p.view in {'block_1','block_2'} else 2
    stage,receipt,new=_start(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        transform=_render_input(path,stage/'render-input.3dm',p.frame,p.max_coordinate_error_m)
        plan={'name':name,'sources':sources,'source_frame':p.frame.model_dump(),'render_transform':transform,
            'render_input_sha256':file_sha(stage/'render-input.3dm')}
        settings=_settings(path,stage/'render-input.3dm',p)
        if settings is not None:
            plan|={'native_settings':settings,'native_settings_sha256':sha(json_text(settings).encode('utf8')),
                'source_file':str(path)}
            if p.view=='multilayer':plan['layer_requests']=[item.model_dump() for item in p.layers]
            else:plan['plane_requests']={axis:plane.model_dump() for axis,plane in p.planes.items()}
        _checkpoint(stage/'plot-plan.json',plan)
        with bridge.connect() as db:
            row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone();receipt=bridge._receipt(row)
            receipt['volume_plot_plan_sha256']=sha((stage/'plot-plan.json').read_bytes());receipt.pop('state',None)
            db.execute('UPDATE commands SET receipt=? WHERE id=?',(json_text(receipt),request_id))
        folder=stage/'native';folder.mkdir()
        if settings is None:native_volume_render(bridge.root,stage/'render-input.3dm',folder)
        else:native_volume_render(bridge.root,stage/'render-input.3dm',folder,settings)
        _adopt(bridge,request_id,stage)
        return _validate(bridge,request_id,stage)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def reconcile_volume_plot(bridge,request_id):
    bridge.request_id(request_id)
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row)
    if receipt.get('artifact_type')!='volume.plot':raise BridgeError('VOLUME_PLOT_REQUEST_REQUIRED')
    if row['state'] not in {'RUNNING','FAILED'}:return bridge.status(request_id)
    stage=bridge.destination_path(receipt['stage'])
    if (stage/'complete.json').exists():return reconcile_bundle(bridge,request_id)
    plan=_plan(bridge,request_id,stage)
    if (stage/'producer.json').exists() and _alive(json.loads((stage/'producer.json').read_text(encoding='utf8'))):
        return receipt|{'producer':'RUNNING','replayed':False}
    if _active_native(stage):return receipt|{'native_renderer':'RUNNING','replayed':False}
    from .native_paths import cleanup_aliases
    if (stage/'native').exists():cleanup_aliases(stage/'native')
    try:
        if not (stage/'candidate.json').exists():_adopt(bridge,request_id,stage)
        if (stage/'plot-validation.json').exists():return _seal(bridge,request_id,stage)
        return _validate(bridge,request_id,stage)
    except Exception as exc:
        # Exact producer and native identities were already observed dead.
        # Retain the partial set as a failed request, never a fictitious run.
        fail_bundle(bridge,request_id,exc);raise
