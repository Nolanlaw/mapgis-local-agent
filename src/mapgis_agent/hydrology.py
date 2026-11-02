"""Pinned PyFlwDir D8 hydrology; existing atomic artifacts/native encoders."""
from __future__ import annotations
import csv,importlib.metadata as metadata,json,math
from pathlib import Path
from typing import Literal
import numpy as np
import psutil,rasterio
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from rasterio.features import shapes
from scipy.ndimage import label
from shapely.geometry import shape,mapping
from shapely.ops import unary_union

from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import Bridge,BridgeError,json_text,sha
from .creation import prepare_creation
from .grid_analysis import _linear_scale
from .grid_vectors import _grid_sources
from .native_raster import _raster_sources,_snapshot_raster
from .raster import file_sha
from .visibility import _checkpoint,_producer_alive


class HydrologyParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    vertical_unit:Literal['metre','foot','us_survey_foot']
    depression_handling:Literal['fill']
    outlets:Literal['edge','min']='edge'
    band:StrictInt=Field(default=1,ge=1)
    nodata_boundary:Literal['error','open']='error'
    max_elevation_rounding_error_m:float=Field(default=0.,ge=0)
    stream_min_cells:StrictInt|None=Field(default=None,ge=1)
    vector_outputs:bool=True


def _runtime():
    versions={n:metadata.version(n) for n in ['pyflwdir','numba','llvmlite','numpy','scipy']}
    if any(versions[n]!=v for n,v in {'pyflwdir':'0.5.12','numba':'0.68.1','llvmlite':'0.50.0'}.items()):
        raise BridgeError('HYDRO_RUNTIME: use pinned project dependency versions')
    # Recovery checks need exact package bytes, not a loaded LLVM compiler.
    package=Path(metadata.distribution('pyflwdir').locate_file('pyflwdir'))
    return None,{'versions':versions,'algorithm':'PyFlwDir fill_depressions D8, upstream_area(cell), basins, vectorize',
        'source_files':{f.name:file_sha(f) for f in sorted(package.glob('*.py'))}}


def _calculate(elevation,valid,transform,options):
    # MCP executes this in an isolated, short-lived Python process. Importing
    # cold Fortran extensions beside its stdin-reader thread can deadlock on
    # Windows; importing in another thread of that same process is insufficient.
    import pyflwdir
    _,runtime=_runtime()
    filled,d8=pyflwdir.dem.fill_depressions(elevation,nodata=np.nan,max_depth=-1.,outlets=options.outlets)
    flow=pyflwdir.from_array(d8,ftype='d8',transform=transform,latlon=False)
    if not flow.isvalid or len(flow.idxs_seq)!=int(valid.sum()):
        raise BridgeError('HYDRO_FLOW_GRAPH: cyclic or incomplete routing')
    counts=flow.upstream_area('cell');basins=flow.basins()
    if np.any(~np.isfinite(filled[valid])) or np.any(filled[valid]<elevation[valid]):
        raise BridgeError('HYDRO_FILLED_ELEVATION_INVALID')
    pits=flow.idxs_pit
    if int(np.sum(counts.ravel()[pits],dtype='int64'))!=int(valid.sum()) or np.any(basins[valid]==0):
        raise BridgeError('HYDRO_CONSERVATION: valid cells must drain to exactly one retained outlet')
    if options.vector_outputs and (len(pits)>20000 or
        options.stream_min_cells is not None and np.count_nonzero(valid&(counts>=options.stream_min_cells))>20000):
        raise BridgeError('HYDRO_VECTOR_LIMIT: increase stream_min_cells or use vector_outputs=false; at most20000 outlet/stream features')
    streams=[]
    if options.vector_outputs and options.stream_min_cells is not None:
        for feature in flow.vectorize(mask=valid&(counts>=options.stream_min_cells)):
            p=feature['properties']
            # PyFlwDir also emits a duplicate-coordinate LineString for a pit.
            # It is represented by the separate outlet WT, not an invalid WL.
            if bool(p['pit']):continue
            i=len(streams)+1
            streams.append({'type':'Feature','id':str(i),'geometry':feature['geometry'],
                'properties':{'ID':i,**{k:int(v) if isinstance(v,(int,np.integer,bool,np.bool_)) else v for k,v in p.items()}}})
    rows=[]
    cell_area=float(abs(transform.a*transform.e))
    for idx in pits:
        row,column=divmod(int(idx),elevation.shape[1]);x,y=rasterio.transform.xy(transform,row,column)
        rows.append({'basin_id':int(basins[row,column]),'cell_id':int(idx)+1,'row':row,'column':column,
            'x':x,'y':y,'upstream_cells':int(counts[row,column]),'upstream_m2':float(counts[row,column])*cell_area})
    arrays={'direction':d8,'filled_elevation':filled,'fill_depth':filled-elevation,
        'upstream_cells':counts,'upstream_area':counts.astype('float64')*cell_area,'basins':basins,'valid':valid}
    return arrays,{'runtime':runtime,'outlets':rows,'streams':streams,'cell_area_m2':cell_area}


def _vectors(bridge,stage,files,arrays,record,template,stem):
    if not HydrologyParameters.model_validate(record['parameters']).vector_outputs:return {}
    encoder=Bridge(stage/'encoder');vectors={}
    def encode(role,features,suffix):
        if not features:return
        name=stem+'-'+role+suffix
        prepare_creation(encoder,{'type':'FeatureCollection','features':features},
            {'name':name,'crs':template['crs'],'map_scale':template['map_scale']},role)
        result=encoder.apply(role);target=files/name;target.write_bytes(Path(result['output']).read_bytes())
        vectors[role]={'file':name,'sha256':file_sha(target),'features':len(features),'geometry_readback':'VERIFIED','native_reopen':'separate original BAS60 resave/readback'}
    encode('streams',record['streams'],'.wl')
    grouped={}
    for geometry,value in shapes(arrays['basins'].astype('int32'),mask=arrays['valid'],transform=rasterio.Affine(*record['transform']),connectivity=4):
        grouped.setdefault(int(value),[]).append(shape(geometry))
    regions=[];basin_counts=np.bincount(arrays['basins'][arrays['valid']])
    for basin,parts in sorted(grouped.items()):
        geometry=unary_union(parts);cells=int(basin_counts[basin])
        if not geometry.is_valid or abs(geometry.area-cells*record['cell_area_m2'])>max(1e-7,geometry.area*1e-10):
            raise BridgeError('HYDRO_BASIN_GEOMETRY: polygonized cell area differs')
        regions.append({'type':'Feature','id':str(basin),'geometry':mapping(geometry),
            'properties':{'ID':basin,'basin_id':basin,'cells':cells,'area_m2':cells*record['cell_area_m2']}})
    encode('basins',regions,'.wp')
    encode('outlets',[{'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':[r['x'],r['y']]},
        'properties':{'ID':i,**{k:v for k,v in r.items() if k not in {'x','y'}}}} for i,r in enumerate(record['outlets'],1)],'.wt')
    return vectors


def _finish_hydrology(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage']);checkpoint=stage/'hydrology-completed.json'
    if not checkpoint.is_file():raise BridgeError('ARTIFACT_INCOMPLETE: completed hydrology was not replayed; use a new request')
    record=json.loads(checkpoint.read_text(encoding='utf8'))
    if record['payload_sha256']!=sha(json_text(receipt['payload']).encode('utf8')):raise BridgeError('HYDRO_CHECKPOINT_CHANGED')
    for item in receipt['sources']:
        if file_sha(Path(item['file']))!=item['sha256']:raise BridgeError('STALE_SOURCE')
    for item in record['staged_members']:
        path=stage/item['name']
        if not path.resolve().is_relative_to(stage.resolve()) or file_sha(path)!=item['sha256']:raise BridgeError('HYDRO_CHECKPOINT_CHANGED')
    if _runtime()[1]!=record['runtime']:raise BridgeError('HYDRO_RUNTIME_CHANGED')
    with np.load(stage/'hydrology-raw.npz',allow_pickle=False) as stored:arrays={k:stored[k] for k in stored.files}
    files=stage/'files';files.mkdir(exist_ok=True);template=receipt['payload']['template'];stem=Path(template['name']).stem
    crs,_=_linear_scale(template['crs'],HydrologyParameters.model_validate(record['parameters']).vertical_unit)
    valid=arrays['valid'];transform=rasterio.Affine(*record['transform']);rasters={}
    for role in ['direction','filled_elevation','fill_depth','upstream_cells','upstream_area','basins']:
        data=arrays[role].copy();name=template['name'] if role=='direction' else stem+'-'+role.replace('_','-')+'.tif'
        nodata={'direction':247,'upstream_cells':-9999,'upstream_area':-1.,'basins':0}.get(role)
        if nodata is None:
            nodata=np.float32(np.finfo('float32').min)
            while np.any(data[valid]==nodata):nodata=np.nextafter(nodata,np.float32(0))
            nodata=float(nodata)
        data[~valid]=nodata;target=files/name
        with rasterio.open(target,'w',driver='GTiff',height=data.shape[0],width=data.shape[1],count=1,
            dtype=data.dtype,crs=crs,transform=transform,compress='deflate',nodata=nodata) as ds:ds.write(data,1)
        with rasterio.open(target) as ds:
            if not np.array_equal(ds.read(1),data) or not np.array_equal(ds.read_masks(1)>0,valid):
                raise BridgeError('HYDRO_SAVED_PIXEL_OR_MASK_CHANGED')
        rasters[role]={'file':name,'sha256':file_sha(target),'dtype':str(data.dtype),'nodata':nodata,
            'range':[float(data[valid].min()),float(data[valid].max())]}
    vectors=_vectors(bridge,stage,files,arrays,record,template,stem)
    csv_name=stem+'-outlets.csv'
    with (files/csv_name).open('w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.DictWriter(stream,fieldnames=['basin_id','cell_id','row','column','x','y','upstream_cells','upstream_m2'])
        writer.writeheader();writer.writerows(record['outlets'])
    depth=arrays['fill_depth'][valid].astype('float64')
    metrics={'shape':list(valid.shape),'valid_cells':int(valid.sum()),'masked_cells':int(valid.size-valid.sum()),
        'basins':len(record['outlets']),'filled_cells':int(np.count_nonzero(depth>0)),'maximum_fill_m':float(depth.max()),
        'fill_volume_m3':float(depth.sum()*record['cell_area_m2']),'cell_area_m2':record['cell_area_m2'],
        'outlet_upstream_cell_sum':sum(r['upstream_cells'] for r in record['outlets']),
        'maximum_upstream_cells':int(arrays['upstream_cells'][valid].max()),'stream_features':len(record['streams'])}
    result={'schema':'mapgis-agent/hydrology/1','crs':template['crs'],'parameters':record['parameters'],
        'runtime':record['runtime'],'rasters':rasters,'vectors':vectors,'outlets_csv':csv_name,'metrics':metrics,
        'source_calibration':record['source_calibration'],'maximum_elevation_rounding_error_m':record['maximum_elevation_rounding_error_m'],
        'direction_codes':{'0':'outlet/pit','1':'E','2':'SE','4':'S','8':'SW','16':'W','32':'NW','64':'N','128':'NE','247':'NoData'},
        'elevation_unit':'metre','routing_precision':'float32, matching pinned upstream priority queue',
        'stream_vector_semantics':'Zero-based source idx/idx_ds retained; terminal duplicate-coordinate pit lines are represented by outlet WT points',
        'boundary_policy':'Selected DEM extent is the domain; edge includes explicit open NoData boundaries. Fill all depressions to their pour points. Equal-height routing follows pinned package tie ordering.',
        'source_preserved':True,'scope':'D8 terrain drainage and chosen stream threshold; no precipitation, runoff, discharge, observed river or flood prediction. Original proprietary MapGIS DTM model not used.'}
    (files/(template['name']+'.json')).write_text(json_text(result),encoding='utf8')
    return finish_bundle(bridge,request_id,files,result)


def prepare_hydrology(bridge,file,expected_sha256,parameters,template,request_id,expected_members=None):
    options=HydrologyParameters.model_validate(parameters)
    if (set(template)!={'name','crs','map_scale'} or Path(template['name']).name!=template['name']
        or Path(template['name']).suffix.lower() not in {'.tif','.tiff'} or isinstance(template['map_scale'],bool)
        or not math.isfinite(template['map_scale']) or template['map_scale']<=0):raise BridgeError('HYDRO_TEMPLATE')
    crs,scale=_linear_scale(template['crs'],options.vertical_unit)
    if crs is None or not math.isclose(crs.axis_info[0].unit_conversion_factor,1.,rel_tol=1e-12):raise BridgeError('HYDRO_PROJECTED_METRE_FRAME')
    source=Path(file).resolve(strict=True);raster_sources=_raster_sources(source,expected_sha256,expected_members)
    sources=raster_sources
    if source.suffix.lower()=='.grd':sources=list({v['file']:v for v in sources+_grid_sources(source,expected_sha256,template['crs'])}.values())
    with rasterio.open(source) as ds:
        if not 1<=options.band<=ds.count:raise BridgeError('HYDRO_BAND')
        if ds.crs and not crs.equals(ds.crs):raise BridgeError('HYDRO_CRS_MISMATCH')
        t=ds.transform
        if (any(not math.isfinite(v) for v in t) or t.a<=0 or t.e>=0 or t.b or t.d
            or not math.isclose(t.a,-t.e,rel_tol=1e-12)):raise BridgeError('HYDRO_GRID: square north-up projected cells required')
        if ds.width*ds.height>2_000_000:raise BridgeError('HYDRO_MEMORY_LIMIT: at most2M DEM cells')
        masks=ds.read_masks(options.band)
        if np.any((masks!=0)&(masks!=255)):raise BridgeError('HYDRO_BINARY_MASK')
        valid=masks==255
        if not valid.any():raise BridgeError('HYDRO_NO_VALID_ELEVATION')
        if not valid.all() and options.nodata_boundary=='error':raise BridgeError('HYDRO_NODATA_BOUNDARY: explicitly choose open or supply a complete DEM')
        if options.outlets=='min' and label(valid,structure=np.ones((3,3),dtype='uint8'))[1]!=1:
            raise BridgeError('HYDRO_MIN_OUTLET_COMPONENTS: use edge or a single connected domain')
        raw=ds.read(options.band,out_dtype='float64');factor,offset=ds.scales[options.band-1],ds.offsets[options.band-1]
        if not math.isfinite(factor) or not math.isfinite(offset):raise BridgeError('HYDRO_VALUE_SCALE')
        elevation=(raw*factor+offset)/scale
        if np.any(~np.isfinite(elevation[valid])):raise BridgeError('HYDRO_NONFINITE_HEIGHT')
        with np.errstate(over='ignore'):routing=elevation.astype('float32')
        if np.any(~np.isfinite(routing[valid])):raise BridgeError('HYDRO_ELEVATION_RANGE')
        rounding=float(np.max(np.abs(routing[valid].astype('float64')-elevation[valid])))
        if rounding>options.max_elevation_rounding_error_m:raise BridgeError('HYDRO_ELEVATION_PRECISION: upstream uses float32; set an explicit acceptable max_elevation_rounding_error_m')
        routing[~valid]=np.nan
    payload={'operation':'terrain.hydrology','file':str(source),'expected_sha256':expected_sha256,'parameters':options.model_dump(),
        'template':template,'expected_members':expected_members,'hydrology_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        process=psutil.Process();_checkpoint(stage/'producer.json',{'pid':process.pid,'start':process.create_time(),'executable':process.exe()})
        _snapshot_raster(source,raster_sources,stage/'input')
        arrays,record=_calculate(routing,valid,t,options)
        np.savez_compressed(stage/'hydrology-raw.npz',**arrays)
        record.update(parameters=options.model_dump(),transform=list(t)[:6],payload_sha256=sha(json_text(payload).encode('utf8')),
            source_calibration={'band':options.band,'scale':factor,'offset':offset,'vertical_to_metre':1/scale},
            maximum_elevation_rounding_error_m=rounding,
            staged_members=[{'name':f.relative_to(stage).as_posix(),'sha256':file_sha(f)} for f in [stage/'hydrology-raw.npz',*sorted((stage/'input').rglob('*'))] if f.is_file()])
        _checkpoint(stage/'hydrology-completed.json',record)
        return _finish_hydrology(bridge,request_id)
    except Exception as error:fail_bundle(bridge,request_id,error);raise


def reconcile_hydrology(bridge,request_id):
    receipt=bridge.status(request_id);stage=bridge.destination_path(receipt['stage'])
    if receipt['state'] not in {'RUNNING','FAILED'} or (stage/'complete.json').is_file():return reconcile_bundle(bridge,request_id)
    if receipt.get('artifact_type')!='terrain.hydrology':raise BridgeError('HYDRO_REQUEST_REQUIRED')
    if _producer_alive(stage):raise BridgeError('HYDRO_PRODUCER_RUNNING')
    return _finish_hydrology(bridge,request_id)


def prepare_hydrology_isolated(bridge,file,expected_sha256,parameters,template,request_id,expected_members=None):
    """Keep compiler memory and cold Fortran loading out of the MCP process."""
    import subprocess
    import sys
    options=parameters if isinstance(parameters,HydrologyParameters) else HydrologyParameters.model_validate(parameters)
    bridge.request_id(request_id)
    request={'root':str(bridge.root),'file':file,'expected_sha256':expected_sha256,
        'parameters':options.model_dump(),'template':template,'request_id':request_id,'expected_members':expected_members}
    code='import sys;sys.path.insert(0,sys.argv[1]);from mapgis_agent.hydrology import _isolated_main;_isolated_main()'
    process=subprocess.Popen([sys.executable,'-s','-B','-X','utf8','-c',code,str(Path(__file__).resolve().parents[1])],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8')
    try:
        stdout,stderr=process.communicate(json_text(request),timeout=600)
    except subprocess.TimeoutExpired:
        # The Windows venv redirector may own a second Python. Stop only the
        # still-live descendants of this exact child before collecting its pipes.
        if process.poll() is None:
            try:children=psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:children=[]
            for child in reversed(children):
                try:child.kill()
                except psutil.NoSuchProcess:pass
            process.kill()
        process.communicate()
        raise BridgeError('HYDRO_WORKER_TIMEOUT: inspect the existing request/producer checkpoint before retrying')
    try:reply=json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BridgeError('HYDRO_WORKER_FAILED: '+stderr[-2000:]) from exc
    if process.returncode or not reply.get('ok'):
        raise BridgeError(reply.get('error','HYDRO_WORKER_FAILED: '+stderr[-2000:]))
    return reply['result']


def _isolated_main():
    import sys
    request=json.load(sys.stdin)
    root=request.pop('root')
    try:result=prepare_hydrology(Bridge(root),**request)
    except Exception as exc:
        print(json_text({'ok':False,'error':str(exc)}),flush=True)
        raise SystemExit(1)
    print(json_text({'ok':True,'result':result}),flush=True)
