"""Verified MSI conversion, native GCP persistence and coordinate sidecars."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path

import rasterio
from rasterio.control import GroundControlPoint as RioGCP
from rasterio.windows import Window

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,sha
from .native_backend import backend_configuration,_run_child
from .raster import GroundControlPoint,fit_affine,AffineFitError,file_sha


def raster_info(path:Path)->dict:
    with rasterio.open(path) as raster:
        digest=hashlib.sha256()
        for band in range(1,raster.count+1):
            for row in range(0,raster.height,128):
                # Fixed scanline order makes the hash independent of TIFF tile/compression layout.
                digest.update(raster.read(band,window=Window(0,row,raster.width,min(128,raster.height-row))).tobytes())
        gcps,gcp_crs=raster.gcps
        return {'width':raster.width,'height':raster.height,'bands':raster.count,'dtypes':list(raster.dtypes),
            'pixel_sha256':digest.hexdigest(),'crs_wkt':raster.crs.to_wkt() if raster.crs else None,
            'transform':list(raster.transform)[:6],'gcp_crs_wkt':gcp_crs.to_wkt() if gcp_crs else None,
            'gcps':[{'pixel':[p.col,p.row],'world':[p.x,p.y]} for p in gcps],
            'nodata':raster.nodata,'color_interpretation':[v.name for v in raster.colorinterp]}


def _pixels_equal(a,b):
    return all(a[k]==b[k] for k in ('width','height','bands','dtypes','pixel_sha256'))


def _native_image(executable,bas,stage,mode,source,target,command=None):
    stage.mkdir()
    args=[str(executable),'image',str(bas),str(bas.parent/'msibase.dll'),mode,str(source),str(target)]
    if command is not None:args.append(str(command))
    execution,events=_run_child(args,stage,'image.json')
    if not any(e.get('event')=='image_saved' and e.get('native_result')==0 for e in events):
        raise BridgeError('NATIVE_IMAGE_INCOMPLETE')
    return execution,events


def prepare_native_raster(bridge:Bridge,operation:str,file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
    if operation not in ('to_msi','to_tiff'):raise BridgeError('NATIVE_RASTER_OPERATION')
    if set(parameters)-{'name','gcps','max_error'}:raise BridgeError('NATIVE_RASTER_PARAMETERS')
    source=Path(file).resolve(strict=True);suffix='.msi' if operation=='to_msi' else '.tif'
    name=parameters.get('name',source.stem+suffix)
    if Path(name).suffix.lower()!=suffix:raise BridgeError('NATIVE_RASTER_OUTPUT_EXTENSION')
    sources=[{'file':str(source),'sha256':expected_sha256}]
    sidecar=source.with_name(source.name+'.json')
    witness=None
    if operation=='to_tiff' and sidecar.is_file():
        witness=json.loads(sidecar.read_text(encoding='utf8'))
        if witness.get('native_sha256')!=expected_sha256:raise BridgeError('MSI_SIDECAR_MISMATCH')
        sources.append({'file':str(sidecar),'sha256':sha(sidecar.read_bytes())})
    payload={'operation':'native.raster.'+operation,'file':str(source),'expected_sha256':expected_sha256,'parameters':parameters,'sources':sources}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        executable,bas=backend_configuration(bridge.root)
        snapshot=stage/('input'+source.suffix.lower());shutil.copyfile(source,snapshot)
        if file_sha(snapshot)!=expected_sha256:raise BridgeError('STALE_SOURCE')
        files=stage/'files';files.mkdir();output=files/name
        if operation=='to_msi':
            info=raster_info(snapshot)
            if info['dtypes']!=['uint8']*info['bands'] or info['bands'] not in (1,3) or 'palette' in info['color_interpretation'] or info['nodata'] is not None:
                raise BridgeError('NATIVE_RASTER_PROFILE: verified byte gray/RGB without palette/nodata required; convert explicitly first')
            points=parameters.get('gcps',info['gcps']);command=None
            if points:
                if not isinstance(points,list) or not 3<=len(points)<=1000:raise BridgeError('NATIVE_GCPS')
                training=[]
                for p in points:
                    if len(p['pixel'])!=2 or len(p['world'])!=2 or not all(type(v) in (int,float) and math.isfinite(v) for v in p['pixel']+p['world']):raise BridgeError('NATIVE_GCPS')
                    x,y=p['pixel'];X,Y=p['world']
                    if not 0<=x<=info['width'] or not 0<=y<=info['height']:raise BridgeError('GCP_OUTSIDE_RASTER')
                    training.append(GroundControlPoint(x,y,X,Y))
                try:fit=fit_affine(training);fit.solve(*fit.apply(0,0))
                except AffineFitError as exc:raise BridgeError('NATIVE_GCPS: singular/degenerate affine') from exc
                tolerance=parameters.get('max_error',1e-7)
                if type(tolerance) not in (int,float) or not math.isfinite(tolerance) or tolerance<=0 or fit.residual>tolerance:raise BridgeError('NATIVE_GCP_RESIDUAL')
                command=stage/'gcps.txt'
                command.write_text(''.join(' '.join(repr(v) for v in (*p['pixel'],*p['world']))+'\n' for p in points),encoding='ascii')
            execution,_=_native_image(executable,bas,stage/'convert','to_msi',snapshot,output,command)
            reopened=stage/'reopened.tif'
            fresh,events=_native_image(executable,bas,stage/'reopen','to_tiff',output,reopened)
            actual=raster_info(reopened)
            if not _pixels_equal(info,actual):raise BridgeError('NATIVE_RASTER_PIXEL_MISMATCH')
            native_points=[{'pixel':[e['pixel_x'],e['pixel_y']],'world':[e['world_x'],e['world_y']]} for e in events if e['event']=='gcp']
            if points and (len(native_points)!=len(points) or any(math.dist(a[k],b[k])>1e-9 for a,b in zip(points,native_points) for k in ('pixel','world'))):raise BridgeError('NATIVE_GCP_REOPEN_MISMATCH')
            # Native MSI retains the affine extent but drops the CRS identifier.
            if not points and any(abs(a-b)>1e-9 for a,b in zip(info['transform'],actual['transform'])):
                raise BridgeError('NATIVE_RASTER_TRANSFORM_LOSS: rectify rotated rasters explicitly first')
            witness={'format':1,'native_sha256':file_sha(output),'raster':info,'gcps_native':native_points,
                'coordinate_metadata':'fingerprinted source TIFF sidecar; CRS identifier not encoded by the native converter',
                'registration':'GCP_SAVED_NOT_RESAMPLED' if points else 'AFFINE_EXTENT_READBACK'}
            (files/(name+'.json')).write_text(json_text(witness),encoding='utf8')
            metadata={'native_execution':execution,'native_reopen':fresh,'pixel_check':'EXACT',
                'native_gcps':native_points,'registration':witness['registration'],'coordinate_metadata':witness['coordinate_metadata']}
        else:
            if parameters.get('gcps') is not None:raise BridgeError('GCP_IMPORT_REQUIRES_TO_MSI')
            execution,events=_native_image(executable,bas,stage/'convert','to_tiff',snapshot,output)
            if witness:
                expected=witness['raster'];actual=raster_info(output)
                if not _pixels_equal(expected,actual):raise BridgeError('MSI_WITNESS_PIXEL_MISMATCH')
                with rasterio.open(output,'r+') as raster:
                    if expected['gcps']:
                        raster.gcps=([RioGCP(col=p['pixel'][0],row=p['pixel'][1],x=p['world'][0],y=p['world'][1]) for p in expected['gcps']],
                            rasterio.crs.CRS.from_wkt(expected['gcp_crs_wkt']))
                    elif expected['crs_wkt']:raster.crs=rasterio.crs.CRS.from_wkt(expected['crs_wkt'])
                actual=raster_info(output)
                if not _pixels_equal(expected,actual):raise BridgeError('RASTER_METADATA_REWRITE_CHANGED_PIXELS')
            else:actual=raster_info(output)
            metadata={'native_execution':execution,'pixel_check':'EXACT_AGAINST_BOUND_SIDECAR' if witness else 'OUTPUT_READBACK',
                'coordinate_metadata':'restored from bound source sidecar' if witness else 'native affine extent; CRS identifier unknown',
                'raster':actual}
        return finish_bundle(bridge,request_id,files,metadata|{'backend':'MapGIS msibase.dll exact build','source_preserved':True})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise


def prepare_raster_warp(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
    """Rectify/reproject a raster with GDAL, an explicit target grid and bounded warp memory."""
    from .raster import explicit_crs
    from rasterio.transform import from_origin
    from rasterio.warp import reproject,Resampling
    from pyproj import CRS
    from pyproj.transformer import TransformerGroup
    if set(parameters)-{'name','target_crs','bounds','resolution','resampling','allow_unknown_accuracy','max_gcp_error'}:
        raise BridgeError('WARP_PARAMETERS')
    bounds,resolution=parameters.get('bounds'),parameters.get('resolution')
    if (not isinstance(bounds,list) or len(bounds)!=4 or not isinstance(resolution,list) or len(resolution)!=2
        or not all(type(v) in (int,float) and math.isfinite(v) for v in bounds+resolution)
        or bounds[0]>=bounds[2] or bounds[1]>=bounds[3] or min(resolution)<=0):raise BridgeError('WARP_GRID')
    method=parameters.get('resampling','nearest')
    if method not in {'nearest','bilinear','cubic'}:raise BridgeError('WARP_RESAMPLING')
    target_spec=parameters.get('target_crs',{});target=explicit_crs(target_spec)
    if target is None:
        if target_spec['unit']=='unknown':raise BridgeError('WARP_LOCAL_UNIT_REQUIRED')
        name=target_spec['name'].replace('"','""');unit=target_spec['unit'];factor=.001 if unit=='millimetre' else 1
        target=CRS.from_wkt(f'LOCAL_CS["{name}",LOCAL_DATUM["Unspecified native grid",32767],UNIT["{unit}",{factor}],AXIS["X",EAST],AXIS["Y",NORTH]]')
    target_rio=rasterio.crs.CRS.from_wkt(target.to_wkt())
    width=math.ceil((bounds[2]-bounds[0])/resolution[0]);height=math.ceil((bounds[3]-bounds[1])/resolution[1])
    if width*height>2_000_000_000:raise BridgeError('WARP_GRID_TOO_LARGE: split into explicit tiles')
    transform=from_origin(bounds[0],bounds[3],*resolution)
    source=Path(file).resolve(strict=True);name=parameters.get('name','rectified.tif')
    if Path(name).suffix.lower() not in ('.tif','.tiff'):raise BridgeError('WARP_TIFF_OUTPUT')
    stage,receipt,new=begin_bundle(bridge,{'operation':'raster.warp','file':str(source),'expected_sha256':expected_sha256,'parameters':parameters},
        request_id,[{'file':str(source),'sha256':expected_sha256}],name)
    if not new:return receipt
    try:
        files=stage/'files';files.mkdir();output=files/name
        with rasterio.open(source) as raster:
            gcps,gcp_crs=raster.gcps;original=gcp_crs if gcps else raster.crs
            if not original:raise BridgeError('WARP_SOURCE_CRS_REQUIRED')
            original_proj=CRS.from_wkt(original.to_wkt())
            if original_proj!=target:
                group=TransformerGroup(original_proj,target,always_xy=True,allow_ballpark=False)
                if not group.best_available or not group.transformers:raise BridgeError('WARP_PRECISE_TRANSFORM_UNAVAILABLE')
                same_datum=original_proj.geodetic_crs is not None and target.geodetic_crs is not None and original_proj.geodetic_crs.equals(target.geodetic_crs,ignore_axis_order=True)
                if group.transformers[0].accuracy<0 and not same_datum and not parameters.get('allow_unknown_accuracy',False):raise BridgeError('WARP_TRANSFORM_ACCURACY_UNKNOWN')
            if gcps:
                try:
                    fit=fit_affine([GroundControlPoint(p.col,p.row,p.x,p.y) for p in gcps]);fit.solve(*fit.apply(0,0))
                except AffineFitError as exc:raise BridgeError('WARP_GCP_AFFINE_REQUIRED') from exc
                tolerance=parameters.get('max_gcp_error',1e-7)
                if type(tolerance) not in (int,float) or not math.isfinite(tolerance) or tolerance<=0 or fit.residual>tolerance:raise BridgeError('WARP_GCP_AFFINE_RESIDUAL')
            if any(v.name=='palette' for v in raster.colorinterp) and method!='nearest':raise BridgeError('WARP_PALETTE_NEAREST_REQUIRED')
            profile=raster.profile.copy();profile.update(driver='GTiff',width=width,height=height,crs=target_rio,transform=transform,compress='lzw',BIGTIFF='IF_SAFER')
            with rasterio.open(output,'w',**profile) as destination:
                for index in range(1,raster.count+1):
                    georeference={'gcps':gcps} if gcps else {'src_transform':raster.transform}
                    reproject(source=rasterio.band(raster,index),destination=rasterio.band(destination,index),src_crs=original,
                        dst_crs=target_rio,dst_transform=transform,src_nodata=raster.nodata,dst_nodata=raster.nodata,
                        resampling=getattr(Resampling,method),warp_mem_limit=64,num_threads=1,MAX_GCP_ORDER=1,**georeference)
                destination.colorinterp=raster.colorinterp
                for index,value in enumerate(raster.colorinterp,1):
                    if value.name=='palette':destination.write_colormap(index,raster.colormap(index))
        info=raster_info(output)
        with rasterio.open(output) as check:
            if check.crs!=target_rio or check.transform!=transform or (check.width,check.height)!=(width,height) or check.gcps[0]:raise BridgeError('WARP_READBACK_MISMATCH')
        return finish_bundle(bridge,request_id,files,{'backend':'GDAL/Rasterio','source_preserved':True,'raster':info,
            'resampling':method,'georeference':'GCP_AFFINE_RECTIFIED' if gcps else 'AFFINE_REPROJECTED',
            'target_bounds_requested':bounds,'warp_memory_mb':64,'ground_accuracy':'requires independent source checkpoints'})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
