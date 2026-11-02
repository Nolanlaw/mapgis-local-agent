"""Explicit-grid Rasterio mosaics and polygon masks with real validity bands.

An extra temporary coverage channel keeps a valid numeric zero distinct from
unwritten pixels. The final GeoTIFF has its original band count and a GDAL mask.
"""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import ColorInterp,Resampling
from rasterio.features import geometry_mask,geometry_window
from rasterio.merge import merge
from rasterio.windows import Window,from_bounds
from rasterio.errors import WindowError
from rasterio.transform import from_origin
from shapely.geometry import shape,mapping
from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import BridgeError,json_text
from .native_raster import _raster_sources,_snapshot_raster,raster_info
from .raster import explicit_crs,crs_metadata,local_crs_matches
from .raster_crop import _windows,_tags

LIMIT=100_000_000
DTYPES={'uint8','uint16','uint32','uint64','int8','int16','int32','int64','float32','float64'}


def _template(template):
    if not isinstance(template,dict) or set(template)!={'name'} or not isinstance(template['name'],str) or Path(template['name']).name!=template['name'] or Path(template['name']).suffix.lower() not in {'.tif','.tiff'}:
        raise BridgeError('COMPOSITE_TEMPLATE: new TIFF basename required')


def _frame(ds,declared):
    if not isinstance(declared,dict):raise BridgeError('COMPOSITE_FRAME: explicit CRS or referenced local frame required')
    frame=explicit_crs(declared);expected=rasterio.crs.CRS.from_wkt(frame.to_wkt()) if frame else None
    matches=ds.crs==expected
    if frame is None and ds.crs is not None:
        from pyproj import CRS
        matches=local_crs_matches(CRS.from_wkt(ds.crs.to_wkt()),declared)
    if not matches:raise BridgeError('COMPOSITE_FRAME: every source must match the declared common frame; rectify first')
    if ds.gcps[0] or ds.tags(ns='RPC') or ds.tags(ns='GEOLOCATION'):raise BridgeError('COMPOSITE_GEOREFERENCE: rectify GCP/RPC/geolocation sources to an affine grid first')
    if not all(math.isfinite(v) for v in ds.transform) or ds.transform.determinant==0:raise BridgeError('COMPOSITE_AFFINE')
    if len(set(ds.dtypes))!=1:raise BridgeError('COMPOSITE_BAND_TYPES')
    if ColorInterp.alpha in ds.colorinterp:raise BridgeError('COMPOSITE_ALPHA: explicitly composite alpha before this whole-pixel operation')
    if any(not math.isfinite(v) for v in (*ds.scales,*ds.offsets)):raise BridgeError('COMPOSITE_CALIBRATION')
    return crs_metadata(declared,frame)


def _metadata(ds):
    return {'count':ds.count,'dtype':ds.dtypes[0],'scales':ds.scales,'offsets':ds.offsets,'units':ds.units,
        'descriptions':ds.descriptions,'colorinterp':ds.colorinterp,'tags':_tags(ds.tags()),
        'band_tags':[_tags(ds.tags(b)) for b in range(1,ds.count+1)],
        'palette':ds.colormap(1) if ColorInterp.palette in ds.colorinterp else None}


def _pixels(ds,window):
    values=ds.read(window=window);masks=ds.read_masks(window=window)
    if np.any((masks!=0)&(masks!=255)):raise BridgeError('COMPOSITE_BINARY_MASK: fractional opacity needs explicit alpha compositing')
    if not np.array_equal(masks,np.broadcast_to(masks[0],masks.shape)):raise BridgeError('COMPOSITE_BAND_MASKS: whole-pixel composition requires common band validity; do not discard a valid band silently')
    valid=masks[0]==255
    if np.any(~np.isfinite(values[:,valid])):raise BridgeError('COMPOSITE_NONFINITE: mark nonfinite source samples as NoData explicitly')
    return values,valid


def _dtype(source,parameters,mean=False,palette=False):
    try:dtype=np.dtype(parameters.get('dtype','float64' if mean else source)).name
    except (TypeError,ValueError) as exc:raise BridgeError('COMPOSITE_DTYPE') from exc
    if dtype not in DTYPES or (mean and dtype not in {'float32','float64'}):raise BridgeError('COMPOSITE_DTYPE: mean needs floating output; other operations need a supported numeric type')
    if not np.can_cast(np.dtype(source),np.dtype('float64' if mean else dtype),casting='safe'):
        raise BridgeError('COMPOSITE_DTYPE: source conversion would lose sample precision; convert explicitly first')
    if palette and dtype!=source:raise BridgeError('COMPOSITE_PALETTE_DTYPE: explicitly colorize before changing palette index type')
    nodata=parameters.get('nodata')
    if nodata is not None:
        if type(nodata) not in (int,float) or not math.isfinite(nodata):raise BridgeError('COMPOSITE_NODATA: finite marker or null required')
        limits=np.iinfo(dtype) if np.dtype(dtype).kind in 'iu' else np.finfo(dtype)
        if not limits.min<=nodata<=limits.max or (np.dtype(dtype).kind in 'iu' and nodata!=math.floor(nodata)):
            raise BridgeError('COMPOSITE_NODATA: marker does not fit output type')
        normalized=np.asarray(nodata,dtype=dtype).item()
        if normalized!=nodata:raise BridgeError('COMPOSITE_NODATA: marker is not represented exactly by output type')
    return dtype,nodata


def _profile(ds,width,height,transform,dtype,nodata):
    return {'driver':'GTiff','width':width,'height':height,'count':ds.count,'dtype':dtype,'nodata':nodata,
        'crs':ds.crs,'transform':transform,'compress':'deflate','tiled':True,'blockxsize':256,'blockysize':256,'BIGTIFF':'IF_SAFER'}


def _decorate(dst,metadata):
    dst.scales=metadata['scales'];dst.offsets=metadata['offsets'];dst.units=metadata['units'];dst.descriptions=metadata['descriptions'];dst.colorinterp=metadata['colorinterp']
    dst.update_tags(**metadata['tags'])
    for band,values in enumerate(metadata['band_tags'],1):dst.update_tags(band,**values)
    if metadata['palette'] is not None:dst.write_colormap(1,metadata['palette'])


def _write(dst,values,valid,window,dtype,nodata):
    with np.errstate(over='raise',invalid='raise'):
        try:output=values.astype(dtype)
        except FloatingPointError as exc:raise BridgeError('COMPOSITE_OUTPUT_RANGE') from exc
    if np.any(~np.isfinite(output[:,valid])):raise BridgeError('COMPOSITE_OUTPUT_RANGE')
    if nodata is not None and np.any(output[:,valid]==nodata):raise BridgeError('COMPOSITE_NODATA_COLLISION: marker equals a valid result; keep a real mask or choose an explicit wider type and unused marker')
    output[:,~valid]=0 if nodata is None else nodata
    dst.write(output,window=window);dst.write_mask(valid.astype('uint8')*255,window=window)
    return output


def _verify(file,profile,metadata,expected):
    with rasterio.open(file) as ds:
        if (ds.width,ds.height,ds.count,ds.transform,ds.crs)!=(profile['width'],profile['height'],profile['count'],profile['transform'],profile['crs']):raise BridgeError('COMPOSITE_SAVED_GRID_DIFF')
        if ds.dtypes!=(profile['dtype'],)*profile['count'] or ds.nodata!=profile['nodata']:raise BridgeError('COMPOSITE_SAVED_DTYPE_OR_NODATA_DIFF')
        if ds.scales!=metadata['scales'] or ds.offsets!=metadata['offsets'] or ds.units!=metadata['units'] or ds.descriptions!=metadata['descriptions'] or ds.colorinterp!=metadata['colorinterp']:
            raise BridgeError('COMPOSITE_SAVED_BAND_METADATA_DIFF')
        if metadata['palette'] is not None and ds.colormap(1)!=metadata['palette']:raise BridgeError('COMPOSITE_SAVED_PALETTE_DIFF')
        for window in _windows(ds.width,ds.height):
            values,valid=expected(window);values=values.astype(profile['dtype']);values[:,~valid]=0 if profile['nodata'] is None else profile['nodata']
            if not np.array_equal(ds.read(window=window),values) or not np.array_equal(ds.read_masks(window=window),np.broadcast_to(valid.astype('uint8')*255,values.shape)):
                raise BridgeError('COMPOSITE_SAVED_PIXEL_OR_MASK_DIFF')


def _finish(bridge,request_id,files,output,metadata):
    info=raster_info(output);metadata|={'raster':info,'source_preserved':True,'empty_result':not any(info['valid_pixels_by_band'])}
    (files/(output.name+'.json')).write_text(json_text(metadata),encoding='utf8')
    return finish_bundle(bridge,request_id,files,{'backend':'Rasterio1.4.4/GDAL merge or geometry rasterization, explicit real mask',**metadata})


def _integer(value):
    if not math.isfinite(value):raise BridgeError('MOSAIC_GRID: finite pixel grid required')
    rounded=round(value)
    if abs(value-rounded)>1e-7:raise BridgeError('MOSAIC_GRID: explicit integral output size and source pixel alignment required; rectify/resample first')
    return int(rounded)


def prepare_raster_mosaic(bridge,sources,parameters,template,request_id):
    _template(template)
    if not isinstance(parameters,dict) or set(parameters)-{'method','bounds','resolution','crs','dtype','nodata'} or not {'method','bounds','resolution','crs'}<=set(parameters):raise BridgeError('MOSAIC_PARAMETERS')
    method=parameters['method'];bounds=parameters['bounds'];resolution=parameters['resolution']
    if method not in {'first','last','mean'}:raise BridgeError('MOSAIC_METHOD: first, last or equal-weight mean required')
    if not isinstance(bounds,list) or len(bounds)!=4 or not isinstance(resolution,list) or len(resolution)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds+resolution) or not bounds[0]<bounds[2] or not bounds[1]<bounds[3] or min(resolution)<=0:raise BridgeError('MOSAIC_GRID')
    left,bottom,right,top=bounds;dx,dy=resolution;width=_integer((right-left)/dx);height=_integer((top-bottom)/dy)
    if min(width,height)<1:raise BridgeError('MOSAIC_GRID')
    if not isinstance(sources,list) or not 1<=len(sources)<=1000:raise BridgeError('MOSAIC_SOURCES')
    records=[];dependencies=[];common=None;declared=None
    for source in sources:
        if not isinstance(source,dict) or set(source)-{'file','expected_sha256','expected_members'} or not {'file','expected_sha256'}<=set(source):raise BridgeError('MOSAIC_SOURCE')
        path=Path(source['file']).resolve(strict=True);members=_raster_sources(path,source['expected_sha256'],source.get('expected_members'))
        with rasterio.open(path) as ds:
            declared=_frame(ds,parameters['crs']);current=_metadata(ds)
            if ds.transform.b or ds.transform.d or ds.transform.a<=0 or ds.transform.e>=0 or not math.isclose(ds.transform.a,dx,rel_tol=1e-12) or not math.isclose(-ds.transform.e,dy,rel_tol=1e-12):raise BridgeError('MOSAIC_GRID: north-up sources with the exact declared resolution required')
            _integer((ds.transform.c-left)/dx);_integer((top-ds.transform.f)/dy)
            if common is None:common=current
            elif any(common[k]!=current[k] for k in ('count','dtype','scales','offsets','units','colorinterp','palette')) or common['tags'].get('AREA_OR_POINT','Area')!=current['tags'].get('AREA_OR_POINT','Area'):
                raise BridgeError('MOSAIC_BAND_METADATA: band count/types/calibration/units/colors must agree')
            if method=='mean' and current['palette'] is not None:raise BridgeError('MOSAIC_PALETTE_MEAN: explicitly colorize palette indices before averaging')
        records.append((path,members));dependencies.extend(members)
    if width*height*common['count']>LIMIT:raise BridgeError('MOSAIC_CAPACITY: crop/tile first; at most100000000 output band cells')
    dtype,nodata=_dtype(common['dtype'],parameters,method=='mean',common['palette'] is not None)
    payload={'operation':'raster.mosaic','sources':sources,'parameters':parameters,'template':template,'composite_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,dependencies,template['name'])
    if not new:return receipt
    try:
        snapshots=[];inputs=stage/'inputs';inputs.mkdir()
        for index,(source,members) in enumerate(records):
            snapshot=_snapshot_raster(source,members,inputs/str(index));snapshots.append(snapshot)
            with rasterio.open(snapshot) as ds:
                try:window=from_bounds(*bounds,transform=ds.transform).intersection(Window(0,0,ds.width,ds.height))
                except WindowError:continue
                col,row,w,h=map(_integer,(window.col_off,window.row_off,window.width,window.height))
                for part in _windows(w,h):_pixels(ds,Window(col+part.col_off,row+part.row_off,part.width,part.height))
        count=common['count'];assignments=[0]*len(records)
        def accumulate(merged,data,old_mask,new_mask,**context):
            valid=~np.any(np.ma.getmaskarray(data),axis=0);coverage=merged[count]
            take=valid&(coverage==0) if method=='first' else valid
            assignments[context['index']]+=int(take.sum())
            if method=='mean':
                with np.errstate(over='raise',invalid='raise'):
                    try:merged[:count,:, :][:,valid]+=np.ma.getdata(data)[:,valid]
                    except FloatingPointError as exc:raise BridgeError('MOSAIC_MEAN_RANGE') from exc
                coverage[valid]+=1
            else:
                np.copyto(merged[:count],np.ma.getdata(data),where=take[None,:,:]);coverage[take]=1
        temporary=stage/'merged.tif';intermediate_type='float64' if method=='mean' else dtype
        merge(snapshots,bounds=bounds,res=resolution,nodata=0,dtype=intermediate_type,output_count=count+1,method=accumulate,
            resampling=Resampling.nearest,target_aligned_pixels=False,mem_limit=64,dst_path=temporary,
            dst_kwds={'compress':'deflate','tiled':True,'blockxsize':256,'blockysize':256,'BIGTIFF':'IF_SAFER'})
        files=stage/'files';files.mkdir();output=files/template['name'];transform=from_origin(left,top,dx,dy)
        with rasterio.open(snapshots[0]) as first,rasterio.open(temporary) as raw:
            profile=_profile(first,width,height,transform,dtype,nodata)
            if raw.width!=width or raw.height!=height or raw.count!=count+1 or raw.transform!=transform:raise BridgeError('MOSAIC_RUNTIME_GRID_DIFF')
            def expected(window):
                values=raw.read(window=window);coverage=values[count];data=values[:count]
                if method=='mean':np.divide(data,coverage[None,:,:],out=data,where=coverage[None,:,:]>0)
                return data,coverage>0
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True),rasterio.open(output,'w',**profile) as dst:
                _decorate(dst,common)
                for window in _windows(width,height):_write(dst,*expected(window),window,dtype,nodata)
            _verify(output,profile,common,expected)
        return _finish(bridge,request_id,files,output,{'schema':'mapgis-agent/raster-mosaic/1','parameters':parameters,'coordinate_frame':declared,
            'grid_alignment_tolerance_pixels':1e-7,'resampling':'none; explicitly aligned source/output grids',
            'method':method,'source_order':'input list order; no deduplication','write_assignments_by_source':assignments,
            'validity_policy':'source band masks must be the same binary mask; coverage channel never mistakes a valid zero for unwritten data',
            'mean_policy':'equal-weight valid source pixels; raw-sample mean with identical linear calibration; no feathering or alpha blend',
            'dtype_policy':'explicit safe sample widening; mean float64 by default or explicit float32 quantization',
            'source_files':dependencies,'source_profiles':'matching count/types/calibration/units/color interpretation; ordinary tags/descriptions from first source'})
    except Exception as error:fail_bundle(bridge,request_id,error);raise


def prepare_raster_mask(bridge,file,expected_sha256,parameters,template,request_id,expected_members=None):
    _template(template)
    if not isinstance(parameters,dict) or set(parameters)-{'geometry','crs','crop','all_touched','dtype','nodata'} or not {'geometry','crs'}<=set(parameters):raise BridgeError('MASK_PARAMETERS')
    crop=parameters.get('crop',True);touch=parameters.get('all_touched',False)
    if type(crop) is not bool or type(touch) is not bool:raise BridgeError('MASK_PARAMETERS: crop and all_touched are booleans')
    try:geometry=shape(parameters['geometry'])
    except Exception as exc:raise BridgeError('MASK_GEOMETRY') from exc
    if geometry.geom_type not in {'Polygon','MultiPolygon'} or geometry.is_empty or not geometry.is_valid or geometry.has_z or not all(math.isfinite(v) for v in geometry.bounds):raise BridgeError('MASK_GEOMETRY: valid nonempty2D Polygon/MultiPolygon required; no implicit repair')
    source=Path(file).resolve(strict=True);sources=_raster_sources(source,expected_sha256,expected_members)
    with rasterio.open(source) as ds:
        frame=_frame(ds,parameters['crs']);metadata=_metadata(ds);dtype,nodata=_dtype(ds.dtypes[0],parameters,palette=metadata['palette'] is not None)
        try:window=geometry_window(ds,[mapping(geometry)]) if crop else Window(0,0,ds.width,ds.height)
        except WindowError as exc:raise BridgeError('MASK_NO_OVERLAP: crop has no source pixels; crop=false retains an explicitly empty full grid') from exc
        col,row,width,height=map(int,(window.col_off,window.row_off,window.width,window.height))
        if width*height*ds.count>LIMIT:raise BridgeError('MASK_CAPACITY: crop/tile first; at most100000000 output band cells')
        transform=ds.window_transform(window);profile=_profile(ds,width,height,transform,dtype,nodata)
    payload={'operation':'raster.mask','file':str(source),'expected_sha256':expected_sha256,'parameters':parameters,'template':template,'expected_members':expected_members,'composite_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        snapshot=_snapshot_raster(source,sources,stage/'input');files=stage/'files';files.mkdir();output=files/template['name']
        with rasterio.open(snapshot) as src:
            def expected(part):
                values,valid=_pixels(src,Window(col+part.col_off,row+part.row_off,part.width,part.height))
                selected=geometry_mask([mapping(geometry)],out_shape=(int(part.height),int(part.width)),transform=rasterio.windows.transform(part,transform),all_touched=touch,invert=True)
                return values,valid&selected
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True),rasterio.open(output,'w',**profile) as dst:
                _decorate(dst,metadata)
                for part in _windows(width,height):_write(dst,*expected(part),part,dtype,nodata)
            _verify(output,profile,metadata,expected)
        return _finish(bridge,request_id,files,output,{'schema':'mapgis-agent/raster-mask/1','parameters':parameters,'coordinate_frame':frame,
            'window':[col,row,width,height],'pixel_rule':'all_touched' if touch else 'pixel center; GDAL polygon rasterization',
            'resampling':'none; original affine pixel grid retained','mask_policy':'binary source validity AND polygon selection, including holes',
            'source_files':sources,'invalid_samples':'filled with explicit nodata marker, or numeric0 with separate real mask; valid zero stays valid'})
    except Exception as error:fail_bundle(bridge,request_id,error);raise
