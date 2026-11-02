"""Verified MSI conversion, native GCP persistence and coordinate sidecars."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from contextlib import ExitStack
from pathlib import Path

import rasterio
import numpy as np
from rasterio.control import GroundControlPoint as RioGCP
from rasterio.windows import Window

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,sha
from .native_backend import backend_configuration,_run_child
from .raster import GroundControlPoint,fit_affine,AffineFitError,file_sha
from .native_paths import NativePathAlias


def raster_info(path:Path)->dict:
    with rasterio.open(path) as raster:
        digest=hashlib.sha256();mask_digest=hashlib.sha256();valid_pixels=[];opaque_pixels=[]
        for band in range(1,raster.count+1):
            valid=0;opaque=0
            for row in range(0,raster.height,128):
                # Fixed scanline order makes the hash independent of TIFF tile/compression layout.
                digest.update(raster.read(band,window=Window(0,row,raster.width,min(128,raster.height-row))).tobytes())
                mask=raster.read_masks(band,window=Window(0,row,raster.width,min(128,raster.height-row)))
                mask_digest.update(mask.tobytes());valid+=int(np.count_nonzero(mask));opaque+=int(np.count_nonzero(mask==255))
            valid_pixels.append(valid);opaque_pixels.append(opaque)
        gcps,gcp_crs=raster.gcps
        return {'width':raster.width,'height':raster.height,'bands':raster.count,'dtypes':list(raster.dtypes),
            'pixel_sha256':digest.hexdigest(),'crs_wkt':raster.crs.to_wkt() if raster.crs else None,
            'transform':list(raster.transform)[:6],'gcp_crs_wkt':gcp_crs.to_wkt() if gcp_crs else None,
            'gcps':[{'pixel':[p.col,p.row],'world':[p.x,p.y]} for p in gcps],
            'nodata':raster.nodata,'color_interpretation':[v.name for v in raster.colorinterp],
            'mask_sha256':mask_digest.hexdigest(),'valid_pixels_by_band':valid_pixels,'opaque_pixels_by_band':opaque_pixels}


def _pixels_equal(a,b):
    keys=['width','height','bands','dtypes','pixel_sha256']
    keys+=[k for k in ('nodata','mask_sha256') if k in a]
    return all(a[k]==b[k] for k in keys)


def _native_mask_profile(source,info):
    nodata=info['nodata']
    if nodata is None:
        if any(n!=info['width']*info['height'] for n in info['opaque_pixels_by_band']):
            raise BridgeError('NATIVE_RASTER_MASK: explicitly flatten or convert masked pixels before native import; transparency must not silently disappear')
        return
    if not math.isfinite(nodata):raise BridgeError('NATIVE_RASTER_FINITE_NODATA_REQUIRED')
    with rasterio.open(source) as raster:
        for row in range(0,raster.height,256):
            for col in range(0,raster.width,256):
                window=Window(col,row,min(256,raster.width-col),min(256,raster.height-row))
                for band in range(1,raster.count+1):
                    values=raster.read(band,window=window)
                    expected=(values!=nodata).astype('uint8')*255
                    if not np.array_equal(expected,raster.read_masks(band,window=window)):
                        raise BridgeError('NATIVE_RASTER_MASK: an additional mask/alpha cannot be represented by the single native numeric nodata marker')


def _raster_members(source):
    with rasterio.open(source) as raster:paths={Path(p).resolve(strict=True) for p in raster.files}
    paths.add(source)
    candidates=[source.with_name(source.name+suffix) for suffix in ('.aux.xml','.msk','.ovr')]
    candidates+=[source.with_suffix(suffix) for suffix in ('.tfw','.tifw','.wld','.prj','.aux','.rrd')]
    paths.update(p.resolve(strict=True) for p in candidates if p.is_file())
    if any(p.parent!=source.parent for p in paths):raise BridgeError('RASTER_SIBLING_DEPENDENCIES_REQUIRED: materialize external VRT/network sources first')
    return {p.name:file_sha(p) for p in sorted(paths)}


def raster_source_info(file:str,expected_sha256:str|None=None)->dict:
    source=Path(file).resolve(strict=True)
    members=_raster_members(source)
    if expected_sha256 is not None and members[source.name]!=expected_sha256:raise BridgeError('STALE_SOURCE')
    info=raster_info(source)
    if _raster_members(source)!=members:raise BridgeError('STALE_DURING_RASTER_READ')
    return {'file':str(source),'source_sha256':members[source.name],'members':members,'raster':info,
            'dependency_policy':'All GDAL-reported sibling files plus recognized mask/georeference/index sidecars; external VRT/network dependencies must first be materialized'}


def _raster_sources(source,expected_sha256,expected_members):
    members=_raster_members(source)
    if members[source.name]!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if len(members)>1 and expected_members is None:raise BridgeError('RASTER_MEMBER_HASHES_REQUIRED: inspect raster_source_info and pass its complete members map')
    if expected_members is not None and expected_members!=members:raise BridgeError('STALE_RASTER_MEMBER')
    return [{'file':str(source.parent/name),'sha256':digest} for name,digest in members.items()]


def _snapshot_raster(source,sources,folder):
    folder.mkdir()
    for member in sources:
        path=Path(member['file']);copy=folder/path.name;shutil.copyfile(path,copy)
        if file_sha(copy)!=member['sha256']:raise BridgeError('STALE_RASTER_MEMBER')
    snapshot=folder/source.name
    if set(_raster_members(snapshot))!={Path(s['file']).name for s in sources}:
        raise BridgeError('RASTER_SNAPSHOT_DEPENDENCY_MISMATCH')
    return snapshot


def _native_image(executable,bas,stage,mode,source,target,command=None):
    stage.mkdir()
    with ExitStack() as stack:
        aliases={}
        for folder in {Path(p).parent.resolve() for p in (source,target,command) if p is not None}:
            aliases[folder]=stack.enter_context(NativePathAlias(folder,record_folder=stage)).path
        args=[str(executable),'image',str(bas),str(bas.parent/'msibase.dll'),mode,
              str(aliases[Path(source).parent.resolve()]/Path(source).name),
              str(aliases[Path(target).parent.resolve()]/Path(target).name)]
        if command is not None:args.append(str(aliases[Path(command).parent.resolve()]/Path(command).name))
        execution,events=_run_child(args,stage,'image.json')
    if not any(e.get('event')=='image_saved' and e.get('native_result')==0 for e in events):
        raise BridgeError('NATIVE_IMAGE_INCOMPLETE')
    return execution,events


def prepare_native_raster(bridge:Bridge,operation:str,file:str,expected_sha256:str,parameters:dict,request_id:str,expected_members:dict|None=None)->dict:
    if operation not in ('to_msi','to_tiff'):raise BridgeError('NATIVE_RASTER_OPERATION')
    if set(parameters)-{'name','gcps','max_error'}:raise BridgeError('NATIVE_RASTER_PARAMETERS')
    source=Path(file).resolve(strict=True);suffix='.msi' if operation=='to_msi' else '.tif'
    name=parameters.get('name',source.stem+suffix)
    if Path(name).suffix.lower()!=suffix:raise BridgeError('NATIVE_RASTER_OUTPUT_EXTENSION')
    sources=(_raster_sources(source,expected_sha256,expected_members) if operation=='to_msi'
             else [{'file':str(source),'sha256':expected_sha256}])
    sidecar=source.with_name(source.name+'.json')
    witness=None
    if operation=='to_tiff' and sidecar.is_file():
        witness=json.loads(sidecar.read_text(encoding='utf8'))
        if witness.get('native_sha256')!=expected_sha256:raise BridgeError('MSI_SIDECAR_MISMATCH')
        sources.append({'file':str(sidecar),'sha256':sha(sidecar.read_bytes())})
    payload={'operation':'native.raster.'+operation,'file':str(source),'expected_sha256':expected_sha256,'parameters':parameters,'expected_members':expected_members,'sources':sources,'native_coordinate_protocol':2}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        executable,bas=backend_configuration(bridge.root)
        if operation=='to_msi':snapshot=_snapshot_raster(source,sources,stage/'input')
        else:
            snapshot=stage/('input'+source.suffix.lower());shutil.copyfile(source,snapshot)
        if file_sha(snapshot)!=expected_sha256:raise BridgeError('STALE_SOURCE')
        files=stage/'files';files.mkdir();output=files/name
        if operation=='to_msi':
            info=raster_info(snapshot)
            _native_mask_profile(snapshot,info)
            from .raster_tiles import DTYPES,write_native_tiles,read_native_tiles
            if (not 1<=info['bands']<=32 or len(set(info['dtypes']))!=1 or info['dtypes'][0] not in DTYPES
                    or 'palette' in info['color_interpretation']):
                raise BridgeError('NATIVE_RASTER_PROFILE: uniform supported numeric bands without a palette required; convert explicitly first')
            typed=info['dtypes']!=['uint8']*info['bands'] or info['bands'] not in (1,3) or info['nodata'] is not None
            points=parameters.get('gcps',info['gcps']);command=None
            if typed and not points:
                a,b,c,d,e,f=info['transform']
                points=[{'pixel':[x,y],'world':[a*x+b*y+c,d*x+e*y+f]}
                        for x,y in [(0,0),(info['width'],0),(info['width'],info['height']),(0,info['height'])]]
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
            native_input=snapshot;mode='to_msi'
            if typed:
                native_input=stage/'input.tiles';write_native_tiles(snapshot,native_input);mode='from_raw'
            execution,_=_native_image(executable,bas,stage/'convert',mode,native_input,output,command)
            reopened=stage/'reopened.tif'
            if typed:
                raw=stage/'reopened.tiles';fresh,events=_native_image(executable,bas,stage/'reopen','to_raw',output,raw)
                read_native_tiles(raw,reopened,info)
            else:fresh,events=_native_image(executable,bas,stage/'reopen','to_tiff',output,reopened)
            actual=raster_info(reopened)
            if not _pixels_equal(info,actual):raise BridgeError('NATIVE_RASTER_PIXEL_MISMATCH')
            native_points=[{'pixel':[e['pixel_x'],e['pixel_y']],'world':[e['world_x'],e['world_y']]} for e in events if e['event']=='gcp']
            if points and (len(native_points)!=len(points) or any(math.dist(a[k],b[k])>1e-9 for a,b in zip(points,native_points) for k in ('pixel','world'))):raise BridgeError('NATIVE_GCP_REOPEN_MISMATCH')
            native_extent=next((e['native_bounds'] for e in events if e['event']=='image_extent'),None)
            if points:
                corners=[fit.apply(x,y) for x,y in [(0,0),(info['width'],0),(info['width'],info['height']),(0,info['height'])]]
                expected_extent=[min(v[0] for v in corners),min(v[1] for v in corners),max(v[0] for v in corners),max(v[1] for v in corners)]
                if native_extent is None or any(not math.isclose(a,b,abs_tol=1e-7,rel_tol=1e-9) for a,b in zip(native_extent,expected_extent)):
                    raise BridgeError('NATIVE_RASTER_EXTENT_MISMATCH: fresh native extent differs from pixel-to-world control fit')
            # Native MSI retains the affine extent but drops the CRS identifier.
            if not points and any(abs(a-b)>1e-9 for a,b in zip(info['transform'],actual['transform'])):
                raise BridgeError('NATIVE_RASTER_TRANSFORM_LOSS: rectify rotated rasters explicitly first')
            witness={'format':1,'native_coordinate_protocol':2,'native_sha256':file_sha(output),'raster':info,'gcps_native':native_points,
                'coordinate_metadata':'fingerprinted source TIFF sidecar; CRS identifier not encoded by the native converter',
                'registration':'GCP_SAVED_NOT_RESAMPLED' if points else 'AFFINE_EXTENT_READBACK'}
            (files/(name+'.json')).write_text(json_text(witness),encoding='utf8')
            metadata={'native_execution':execution,'native_reopen':fresh,'pixel_check':'EXACT',
                'native_profile':next(e for e in events if e['event']=='image_profile'),
                'native_nodata':actual['nodata'],
                'native_pyramids':[e for e in events if e['event']=='image_pyramid'],
                'pyramid_resampling':'nearest top-left decimation through native tile APIs' if typed else 'native TIFF converter',
                'native_gcps':native_points,'native_extent':native_extent,'native_coordinate_protocol':2,
                'registration':witness['registration'],'coordinate_metadata':witness['coordinate_metadata']}
            profile=metadata['native_profile']
            metadata['native_display']={'rgb_byte':profile['cell_type']==0 and profile['bands']==3 and profile['band_info']==2,
                'numeric_storage_exact':True,
                'nonbyte_multiband_policy':'Native numeric storage is not a true-color display contract. For an explicit RGB display branch reuse mapgis_prepare_raster_processing stretch on three selected bands; output0..254,255 NoData. Preserve the numeric MSI separately.'}
        else:
            if parameters.get('gcps') is not None:raise BridgeError('GCP_IMPORT_REQUIRES_TO_MSI')
            typed=bool(witness and (witness['raster']['dtypes']!=['uint8']*witness['raster']['bands'] or witness['raster']['bands'] not in (1,3) or witness['raster']['nodata'] is not None))
            if typed or witness is None:
                from .raster_tiles import read_native_tiles
                spatial=witness['raster'] if witness else None
                if spatial is None:
                    # The legacy TIFF adapter still provides native spatial
                    # metadata, but its numeric pixel/type conversion is not
                    # trusted. Reconstruct pixels from fresh native tile reads.
                    spatial_file=stage/'native-spatial.tif'
                    _native_image(executable,bas,stage/'spatial','to_tiff',snapshot,spatial_file)
                    spatial=raster_info(spatial_file)
                raw=stage/'export.tiles';execution,events=_native_image(executable,bas,stage/'convert','to_raw',snapshot,raw)
                read_native_tiles(raw,output,spatial)
            else:execution,events=_native_image(executable,bas,stage/'convert','to_tiff',snapshot,output)
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


def prepare_raster_warp(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,request_id:str,expected_members:dict|None=None)->dict:
    """Rectify/reproject a raster with GDAL, an explicit target grid and bounded warp memory."""
    from .raster import explicit_crs,local_crs_matches
    from rasterio.transform import from_origin
    from rasterio.warp import reproject,Resampling
    from pyproj import CRS
    from pyproj.transformer import TransformerGroup
    if set(parameters)-{'name','target_crs','bounds','resolution','resampling','allow_unknown_accuracy','max_gcp_error','gcp_model','checkpoints','max_inverse_error_pixels','max_checkpoint_error'}:
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
    cells=[(bounds[2]-bounds[0])/resolution[0],(bounds[3]-bounds[1])/resolution[1]]
    if not all(math.isfinite(v) for v in cells):raise BridgeError('WARP_GRID')
    # Subtracting large native coordinates can leave an exact integer extent a
    # few ulps above its intended count. Keep genuinely fractional grids ceil'd.
    width,height=[max(1,round(v)) if abs(v-round(v))<=1e-9 else math.ceil(v) for v in cells]
    if width*height>2_000_000_000:raise BridgeError('WARP_GRID_TOO_LARGE: split into explicit tiles')
    transform=from_origin(bounds[0],bounds[3],*resolution)
    source=Path(file).resolve(strict=True);name=parameters.get('name','rectified.tif')
    if Path(name).suffix.lower() not in ('.tif','.tiff'):raise BridgeError('WARP_TIFF_OUTPUT')
    sources=_raster_sources(source,expected_sha256,expected_members)
    stage,receipt,new=begin_bundle(bridge,{'operation':'raster.warp','file':str(source),'expected_sha256':expected_sha256,'parameters':parameters,'expected_members':expected_members,'warp_protocol':3},
        request_id,sources,name)
    if not new:return receipt
    try:
        files=stage/'files';files.mkdir();output=files/name
        snapshot=_snapshot_raster(source,sources,stage/'input')
        with rasterio.open(snapshot) as raster:
            gcps,gcp_crs=raster.gcps;original=gcp_crs if gcps else raster.crs
            if not original:raise BridgeError('WARP_SOURCE_CRS_REQUIRED')
            original_proj=CRS.from_wkt(original.to_wkt())
            if target_spec.get('kind')=='local' and original_proj.is_engineering:
                if not local_crs_matches(original_proj,target_spec):raise BridgeError('WARP_LOCAL_FRAME_MISMATCH: local name, known units and east/north axes must agree; no implicit local datum conversion')
                target=original_proj;target_rio=original
            if original_proj!=target:
                group=TransformerGroup(original_proj,target,always_xy=True,allow_ballpark=False)
                if not group.best_available or not group.transformers:raise BridgeError('WARP_PRECISE_TRANSFORM_UNAVAILABLE')
                same_datum=original_proj.geodetic_crs is not None and target.geodetic_crs is not None and original_proj.geodetic_crs.equals(target.geodetic_crs,ignore_axis_order=True)
                if group.transformers[0].accuracy<0 and not same_datum and not parameters.get('allow_unknown_accuracy',False):raise BridgeError('WARP_TRANSFORM_ACCURACY_UNKNOWN')
            gcp_model=parameters.get('gcp_model','affine');gcp_report=None;gcp_options={}
            if gcps:
                from .gcp_correction import validate_gcp_model
                gcp_report=validate_gcp_model(gcps,gcp_model,parameters.get('checkpoints',[]),parameters.get('max_gcp_error',1e-7),
                    parameters.get('max_inverse_error_pixels',.5),parameters.get('max_checkpoint_error'))
                gcp_report['source_crs_wkt']=original.to_wkt()
                gcp_options={'MAX_GCP_ORDER':{'affine':1,'polynomial2':2,'polynomial3':3,'tps':-1}[gcp_model],
                    'SRC_METHOD':'GCP_TPS' if gcp_model=='tps' else 'GCP_POLYNOMIAL'}
            elif any(k in parameters for k in ('gcp_model','checkpoints','max_checkpoint_error','max_inverse_error_pixels')):
                raise BridgeError('WARP_GCPS_REQUIRED: requested control-point model needs embedded source GCPs')
            if any(v.name=='palette' for v in raster.colorinterp) and method!='nearest':raise BridgeError('WARP_PALETTE_NEAREST_REQUIRED')
            profile=raster.profile.copy();profile.update(driver='GTiff',width=width,height=height,crs=target_rio,transform=transform,compress='lzw',BIGTIFF='IF_SAFER')
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True),rasterio.open(output,'w',**profile) as destination:
                for index in range(1,raster.count+1):
                    georeference={'gcps':gcps} if gcps else {'src_transform':raster.transform}
                    reproject(source=rasterio.band(raster,index),destination=rasterio.band(destination,index),src_crs=original,
                        dst_crs=target_rio,dst_transform=transform,src_nodata=raster.nodata,dst_nodata=raster.nodata,
                        resampling=getattr(Resampling,method),warp_mem_limit=64,num_threads=1,**gcp_options,**georeference)
                destination.colorinterp=raster.colorinterp
                for index,value in enumerate(raster.colorinterp,1):
                    if value.name=='palette':destination.write_colormap(index,raster.colormap(index))
                # GDAL band warps alone can discard an explicit mask when no
                # numeric nodata is declared. Warp a separate common validity
                # plane; valid numeric zero must retain its distinct meaning.
                mask_source=stage/'validity-source.tif';mask_target=stage/'validity-warp.tif'
                spatial={'gcps':gcps,'crs':original} if gcps else {'transform':raster.transform,'crs':original}
                with rasterio.open(mask_source,'w',driver='GTiff',width=raster.width,height=raster.height,
                        count=1,dtype='uint8',nodata=0,compress='lzw',BIGTIFF='IF_SAFER',**spatial) as mask:
                    for row in range(0,raster.height,256):
                        for column in range(0,raster.width,256):
                            window=Window(column,row,min(256,raster.width-column),min(256,raster.height-row))
                            valid=np.all(raster.read_masks(window=window)>0,axis=0)
                            mask.write(valid.astype('uint8')*255,1,window=window)
                with rasterio.open(mask_source) as mask,rasterio.open(mask_target,'w',driver='GTiff',
                        width=width,height=height,count=1,dtype='uint8',crs=target_rio,
                        transform=transform,nodata=0,compress='lzw',BIGTIFF='IF_SAFER') as warped:
                    reproject(source=rasterio.band(mask,1),destination=rasterio.band(warped,1),
                        src_crs=original,dst_crs=target_rio,dst_transform=transform,src_nodata=0,dst_nodata=0,
                        resampling=Resampling.nearest,warp_mem_limit=64,num_threads=1,**gcp_options,**georeference)
                with rasterio.open(mask_target) as warped:
                    for row in range(0,height,256):
                        for column in range(0,width,256):
                            window=Window(column,row,min(256,width-column),min(256,height-row))
                            destination.write_mask(warped.read(1,window=window),window=window)
        info=raster_info(output)
        with rasterio.open(output) as check:
            if check.crs!=target_rio or check.transform!=transform or (check.width,check.height)!=(width,height) or check.gcps[0]:raise BridgeError('WARP_READBACK_MISMATCH')
        (files/(output.name+'.json')).write_text(json_text({'schema':'mapgis-agent/raster-warp/2','sources':sources,'parameters':parameters,
            'gcp_report':gcp_report,'raster':info,'warp_transform_approximation_pixels':.125,'source_preserved':True}),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'GDAL/Rasterio','source_preserved':True,'raster':info,
            'resampling':method,'georeference':'GCP_'+gcp_model.upper()+'_RECTIFIED' if gcps else 'AFFINE_REPROJECTED','gcp_report':gcp_report,
            'mask_policy':'INTERSECTION_OF_WARPED_BAND_MASKS','mask_resampling':'nearest',
            'warp_transform_approximation_pixels':.125,
            'target_bounds_requested':bounds,'warp_memory_mb':64,'ground_accuracy':gcp_report['ground_accuracy'] if gcp_report else 'requires independent source checkpoints'})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
