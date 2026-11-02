"""Native DTM IDW and GRD readback in the existing isolated x86 host.

GDAL independently reads the saved Surfer grid. Native DEM buffers are reopened
in another process and compared cell by cell before the artifact can be applied.
"""
from __future__ import annotations

import json
import math
import shutil
import uuid
import os
from numbers import Real
from pathlib import Path

import numpy as np
import rasterio
import psutil

from .artifacts import begin_bundle, finish_bundle, fail_bundle,reconcile_bundle
from .core import Bridge, BridgeError, json_text, read_native, require_native_crs, sha
from .native_backend import backend_configuration, _run_child,completed_native_child
from .native_paths import NativePathAlias,cleanup_aliases


def read_grid(file: str | Path):
    """Use GDAL's existing Surfer driver; return rows ordered south to north."""
    try:
        with rasterio.open(file) as ds:
            if (ds.driver not in {'GSBG','GSAG'} or ds.count!=1 or ds.width<2 or ds.height<2
                    or ds.width>32767 or ds.height>32767 or ds.width*ds.height>2_000_000):
                raise BridgeError('GRID_PROFILE: bounded Surfer GRD required')
            t=ds.transform
            if t.b!=0 or t.d!=0 or t.a<=0 or t.e>=0:
                raise BridgeError('GRID_TRANSFORM: north-up rectangular GRD required')
            z=np.flipud(ds.read(1).astype(np.float64))
            valid=np.flipud(ds.read_masks(1)>0)&np.isfinite(z)
            if np.any(valid & (np.abs(z)>=3.4e18)):
                raise BridgeError('GRID_NATIVE_VALUE_RANGE')
            if not np.any(valid):raise BridgeError('GRID_NO_VALID_CELLS')
            xs=t.c+(np.arange(ds.width)+.5)*t.a
            ys=t.f+(np.arange(ds.height)+.5)*t.e
            ys=ys[::-1].copy()
            info={'shape':[ds.height,ds.width],
                  'bounds':[float(xs[0]),float(ys[0]),float(xs[-1]),float(ys[-1])],
                  'range':[float(np.min(z[valid])),float(np.max(z[valid]))],
                  'valid_cells':int(np.count_nonzero(valid)),
                  'masked_cells':int(np.count_nonzero(~valid)), 'driver':ds.driver}
            return xs,ys,np.ma.array(z,mask=~valid),info
    except rasterio.errors.RasterioError as exc:
        raise BridgeError('GRID_READ_FAILED') from exc


def _native_grid_read(root: Path, file: Path, folder: Path):
    executable,bas=backend_configuration(root)
    folder.mkdir()
    snapshot=folder/'input.grd'
    shutil.copyfile(file,snapshot)
    original=sha(file.read_bytes())
    with NativePathAlias(folder) as alias:
        host,events=_run_child([str(executable),'terrain','inspect',str(bas.parent/'W60_Tin.dll'),
                               str(alias.path/'input.grd'),str(alias.path/'cells.bin')],folder,'native-read.json')
    _,_,grid,info=read_grid(file)
    event=next((e for e in events if e.get('event')=='grid'),None)
    values=np.fromfile(folder/'cells.bin',dtype='<f8')
    if (not event or [event['ny'],event['nx']]!=info['shape']
            or not np.allclose(event['bounds'],info['bounds'],rtol=0,atol=1e-9)
            or values.size!=grid.size):
        raise BridgeError('NATIVE_GRID_LAYOUT_DIFF')
    values=values.reshape(grid.shape)
    valid=np.isfinite(values)&(np.abs(values)<3.4e18)
    expected=~np.ma.getmaskarray(grid)
    if not np.array_equal(valid,expected) or not np.array_equal(values[valid],grid.data[valid]):
        raise BridgeError('NATIVE_GRID_CELLS_DIFF')
    if sha(snapshot.read_bytes())!=original or sha(file.read_bytes())!=original:
        raise BridgeError('STALE_DURING_GRID_READ')
    return host|{'all_cells_equal':True,'metrics':info,'source_preserved':True}


def native_grid_info(root: str | Path,file: str,expected_sha256: str):
    bridge=Bridge(root)
    path=Path(file).resolve(strict=True)
    if sha(path.read_bytes())!=expected_sha256:raise BridgeError('STALE_SOURCE')
    # Validate dimensions before the old native importer allocates a work area.
    read_grid(path)
    folder=bridge.destination_path(bridge.root/'work/native-grid-info'/uuid.uuid4().hex)
    folder.parent.mkdir(parents=True,exist_ok=True)
    readback=_native_grid_read(bridge.root,path,folder)
    return {'file':str(path),'sha256':expected_sha256,'backend':'MapGIS 6.7 W60_Tin',
            'mode':'isolated_native_x86','metrics':readback['metrics'],
            'native_reopen':readback,'source_preserved':True}


def _idw_parameters(parameters,template):
    keys={'operation','height_field','bounds','shape','power','search_radius',
          'anisotropy_ratio','anisotropy_angle_degrees'}
    if set(parameters)!=keys:raise BridgeError('UNEXPECTED_PARAMETERS')
    if parameters['operation']!='native.idw':raise BridgeError('UNEXPECTED_PARAMETERS')
    name=template.get('name','')
    if Path(name).name!=name or Path(name).suffix.lower()!='.grd':raise BridgeError('INVALID_TEMPLATE_NAME')
    bounds=parameters['bounds'];shape=parameters['shape'];power=parameters['power']
    if (not isinstance(bounds,list) or len(bounds)!=4
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds)
            or bounds[0]>=bounds[2] or bounds[1]>=bounds[3]):raise BridgeError('GRID_BOUNDS')
    if (not isinstance(shape,list) or len(shape)!=2 or any(type(n) is not int or not 2<=n<=32767 for n in shape)
            or math.prod(shape)>2_000_000):raise BridgeError('GRID_SHAPE: explicit [columns,rows], at most 2000000 cells')
    if type(power) is not int or not 1<=power<=16:raise BridgeError('IDW_POWER: native integer power 1..16 required')
    for key in ('search_radius','anisotropy_ratio','anisotropy_angle_degrees'):
        if type(parameters[key]) not in (int,float) or not math.isfinite(parameters[key]):raise BridgeError('IDW_'+key.upper())
    if parameters['search_radius']<=0:raise BridgeError('IDW_SEARCH_RADIUS')
    if parameters['anisotropy_ratio']<1:raise BridgeError('IDW_ANISOTROPY_RATIO: major/minor ratio must be at least one')
    return bounds,shape


def _idw_samples(file,digest,height_field,crs):
    path=Path(file).resolve(strict=True)
    blob,reader,_=read_native(path)
    if sha(blob)!=digest:raise BridgeError('STALE_SOURCE')
    if reader.shapeType!='POINT':raise BridgeError('TERRAIN_POINTS_REQUIRED')
    require_native_crs(reader,crs,'TERRAIN_CRS_MISMATCH')
    if reader.crs and reader.crs.is_geographic:raise BridgeError('TERRAIN_PROJECTED_OR_LOCAL_COORDINATES_REQUIRED')
    if height_field not in reader.data:raise BridgeError('MISSING_HEIGHT_FIELD')
    unique={}
    for g,z in zip(reader.geom,reader.data[height_field].tolist()):
        if not isinstance(z,Real) or isinstance(z,(bool,np.bool_)) or not math.isfinite(z) or abs(z)>=3.4e18:
            raise BridgeError('INVALID_HEIGHT: finite heights within native numeric range required')
        key=(float(g.x),float(g.y))
        if not all(math.isfinite(v) for v in key):raise BridgeError('INVALID_TERRAIN_COORDINATE')
        if key in unique and unique[key]!=float(z):raise BridgeError('INCONSISTENT_DUPLICATE_POINTS')
        unique[key]=float(z)
    if not 3<=len(unique)<=1_000_000:raise BridgeError('INSUFFICIENT_TERRAIN_POINTS')
    return path,[(x,y,z) for (x,y),z in unique.items()],len(reader.geom)


def _idw_commands(bounds,shape,points,parameters):
    header=[*shape,bounds[0],bounds[2],bounds[1],bounds[3],len(points),parameters['power'],
            parameters['search_radius'],parameters['anisotropy_ratio'],math.radians(parameters['anisotropy_angle_degrees'])]
    return ' '.join(format(v,'.17g') for v in header)+'\n'+''.join(' '.join(format(v,'.17g') for v in row)+'\n' for row in points)


def _finish_idw_result(bridge,request_id,stage,payload,execution,events,recovered=False):
    parameters=payload['parameters'];template=payload['template']
    bounds,shape=_idw_parameters(parameters,template)
    path,points,source_count=_idw_samples(payload['file'],payload['expected_sha256'],parameters['height_field'],template['crs'])
    if (stage/'native/input.txt').read_text(encoding='ascii')!=_idw_commands(bounds,shape,points,parameters):
        raise BridgeError('NATIVE_GRID_INPUT_CHANGED')
    candidate=stage/'native/grid.grd'
    saved=next((e for e in events if e.get('event')=='grid_saved'),None)
    if not saved or saved['sha256']!=sha(candidate.read_bytes()):raise BridgeError('NATIVE_GRID_SAVE_DIFF')
    reopened=_native_grid_read(bridge.root,candidate,stage/('reopen-'+uuid.uuid4().hex[:12]))
    metrics=reopened['metrics']|{'source_points':source_count,'unique_points':len(points)}
    if metrics['shape']!=shape[::-1] or not np.allclose(metrics['bounds'],bounds,rtol=0,atol=1e-9):
        raise BridgeError('NATIVE_IDW_GRID_DIFF')
    if reopened['pid']==execution['pid']:raise BridgeError('NATIVE_GRID_REOPEN_IDENTITY')
    metadata={'backend':'MapGIS 6.7 W60_Tin ordinal 153','algorithm':'inverse_distance_power_all_points',
        'parameters':parameters,'metrics':metrics,'native_execution':execution,'native_reopen':reopened,
        'extrapolation':'full rectangular grid; convex-hull clipping is a separate operation',
        'coincidence_tolerance':parameters['search_radius']/1e8,'source_preserved':True,'replayed':False,
        'reconciled_after_interruption':recovered}
    files=stage/'files';files.mkdir(exist_ok=recovered)
    allowed={template['name'],template['name']+'.json'}
    if any(p.name not in allowed for p in files.iterdir()):raise BridgeError('ARTIFACT_STAGE_CONFLICT')
    target=files/template['name']
    if target.exists() and sha(target.read_bytes())!=saved['sha256']:raise BridgeError('NATIVE_GRID_SAVE_DIFF')
    if not target.exists():shutil.copyfile(candidate,target)
    sidecar={'schema':'mapgis-agent/grid/1','grid_sha256':sha(target.read_bytes()),'crs':template['crs'],
             'input_file':str(path),'input_sha256':payload['expected_sha256'],**metadata}
    (files/(template['name']+'.json')).write_text(json_text(sidecar),encoding='utf8')
    return finish_bundle(bridge,request_id,files,metadata)


def prepare_native_idw(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str):
    bounds,shape=_idw_parameters(parameters,template)
    path,points,_=_idw_samples(file,expected_sha256,parameters['height_field'],template.get('crs',{}))
    executable,bas=backend_configuration(bridge.root)
    payload={'operation':'terrain.native_idw','file':str(path),'expected_sha256':expected_sha256,
             'parameters':parameters,'template':template}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],template['name'])
    if not new:return receipt
    try:
        process=psutil.Process()
        (stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':process.create_time(),'executable':process.exe()}),encoding='utf8')
        native=stage/'native';native.mkdir()
        commands=_idw_commands(bounds,shape,points,parameters)
        (native/'input.txt').write_text(commands,encoding='ascii')
        candidate=native/'grid.grd'
        with NativePathAlias(native) as alias:
            execution,events=_run_child([str(executable),'terrain','idw',str(bas.parent/'W60_Tin.dll'),
                str(alias.path/'input.txt'),str(alias.path/'grid.grd')],native,'native-idw.json')
        return _finish_idw_result(bridge,request_id,stage,payload,execution,events)
    except Exception as exc:
        fail_bundle(bridge,request_id,exc)
        raise


def reconcile_native_idw(bridge:Bridge,request_id:str):
    bridge.request_id(request_id)
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row)
    if receipt.get('kind')!='BUNDLE' or receipt.get('artifact_type')!='terrain.native_idw':raise BridgeError('NATIVE_GRID_REQUEST_REQUIRED')
    if row['state'] not in {'RUNNING','FAILED'}:return bridge.status(request_id)
    stage=bridge.destination_path(receipt['stage'])
    if (stage/'complete.json').exists():return reconcile_bundle(bridge,request_id)
    producer_file=stage/'producer.json'
    if producer_file.exists() and row['state']=='RUNNING':
        producer=json.loads(producer_file.read_text(encoding='utf8'))
        try:
            process=psutil.Process(producer['pid'])
            if abs(process.create_time()-producer['start'])<1e-5 and process.exe()==producer['executable'] and process.is_running():
                return receipt|{'producer':'RUNNING','replayed':False}
        except psutil.NoSuchProcess:pass
        except psutil.AccessDenied as exc:raise BridgeError('NATIVE_PRODUCER_IDENTITY_UNAVAILABLE') from exc
    try:
        result=completed_native_child(stage/'native','native-idw.json')
        if result is None:return receipt|{'native_child':'RUNNING','replayed':False}
        host,events=result
        cleanup_aliases(stage/'native')
        execution={'pid':host['pid'],'dep_flags':host['dep_flags'],'desktop':host['desktop'],'private_desktop':True,
                   'log':str(stage/'native/native-idw.json'),'completion_receipt_recovered':True}
        return _finish_idw_result(bridge,request_id,stage,receipt['payload'],execution,events,recovered=True)
    except Exception as exc:
        fail_bundle(bridge,request_id,exc)
        raise
