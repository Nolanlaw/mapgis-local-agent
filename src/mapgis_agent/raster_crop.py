"""Lossless integer pixel windows, using Rasterio's existing window API."""
from __future__ import annotations

import math
from pathlib import Path

import rasterio
from rasterio.control import GroundControlPoint
from rasterio.enums import MaskFlags
from rasterio.windows import Window

from .artifacts import begin_bundle, fail_bundle, finish_bundle
from .core import BridgeError, json_text
from .native_raster import _raster_sources, _snapshot_raster, raster_info


def _tags(values):
    # Whole-image statistics cease to describe a subwindow. Other ordinary
    # metadata stays attached; RPC/geolocation arrays need a different crop.
    return {k:v for k,v in values.items() if not k.upper().startswith(('STATISTICS_','HISTOGRAM_'))}


def _windows(width,height):
    for row in range(0,height,256):
        for col in range(0,width,256):
            yield Window(col,row,min(256,width-col),min(256,height-row))


def _safe_info(path):
    info=raster_info(path)
    if info['nodata'] is not None and not math.isfinite(info['nodata']):
        info['nodata']='NaN' if math.isnan(info['nodata']) else ('+Infinity' if info['nodata']>0 else '-Infinity')
        info['nodata_encoding']='nonfinite marker encoded as a string; the TIFF keeps its numeric marker'
    return info


def _gcp_values(points):
    return [(g.col,g.row,g.x,g.y,g.z) for g in points]


def prepare_raster_crop(bridge,file,expected_sha256,window,template,request_id,expected_members=None):
    if (not isinstance(window,list) or len(window)!=4 or any(type(v) is not int for v in window)
        or min(window[:2])<0 or min(window[2:])<1):
        raise BridgeError('CROP_WINDOW: integer [column,row,width,height], nonnegative origin and positive size required')
    if (set(template)!={'name'} or Path(template['name']).name!=template['name']
        or Path(template['name']).suffix.lower() not in {'.tif','.tiff'}):
        raise BridgeError('CROP_TEMPLATE: new TIFF basename required')
    source=Path(file).resolve(strict=True)
    sources=_raster_sources(source,expected_sha256,expected_members)
    col,row,width,height=window
    with rasterio.open(source) as ds:
        if col+width>ds.width or row+height>ds.height:raise BridgeError('CROP_WINDOW: window extends outside source; no rounding, padding or resampling')
        if len(set(ds.dtypes))!=1:raise BridgeError('CROP_BAND_TYPES: explicitly materialize uniform band types first')
        if ds.tags(ns='RPC') or ds.tags(ns='GEOLOCATION'):
            raise BridgeError('CROP_GEOREFERENCE: RPC/geolocation arrays require an explicit rectification before window cropping')
    payload={'operation':'raster.crop','file':str(source),'expected_sha256':expected_sha256,
        'window':window,'template':template,'expected_members':expected_members,'crop_protocol':1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        snapshot=_snapshot_raster(source,sources,stage/'input')
        files=stage/'files';files.mkdir();output=files/template['name'];crop=Window(*window)
        with rasterio.open(snapshot) as src:
            gcps,gcp_crs=src.gcps
            shifted=[GroundControlPoint(col=g.col-col,row=g.row-row,x=g.x,y=g.y,z=g.z,id=g.id,info=g.info) for g in gcps]
            spatial=({'gcps':shifted,'crs':gcp_crs} if gcps else {'transform':src.window_transform(crop),'crs':src.crs})
            profile={'driver':'GTiff','width':width,'height':height,'count':src.count,'dtype':src.dtypes[0],
                'nodata':src.nodata,'compress':'deflate','tiled':True,'blockxsize':256,'blockysize':256,'BIGTIFF':'IF_SAFER',**spatial}
            common_mask=all(MaskFlags.per_dataset in flags and MaskFlags.alpha not in flags for flags in src.mask_flag_enums)
            # A dataset mask overrides nodata for *all* bands. Only copy a real
            # shared mask this way; implicit per-band nodata/alpha stays implicit.
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False):
                with rasterio.open(output,'w',**profile) as dst:
                    dst.scales=src.scales;dst.offsets=src.offsets
                    dst.units=src.units;dst.descriptions=src.descriptions;dst.colorinterp=src.colorinterp
                    dst.update_tags(**_tags(src.tags()))
                    for band in range(1,src.count+1):
                        dst.update_tags(band,**_tags(src.tags(band)))
                        if src.colorinterp[band-1]==rasterio.enums.ColorInterp.palette:
                            dst.write_colormap(band,src.colormap(band))
                    for out_window in _windows(width,height):
                        in_window=Window(col+out_window.col_off,row+out_window.row_off,out_window.width,out_window.height)
                        dst.write(src.read(window=in_window),window=out_window)
                        if common_mask:dst.write_mask(src.read_masks(1,window=in_window),window=out_window)
            with rasterio.open(output) as check:
                if (check.scales!=src.scales or check.offsets!=src.offsets or check.units!=src.units
                    or check.descriptions!=src.descriptions or check.colorinterp!=src.colorinterp):
                    raise BridgeError('CROP_SAVED_BAND_METADATA_DIFF')
                actual_gcps,actual_crs=check.gcps
                if gcps:
                    if actual_crs!=gcp_crs or _gcp_values(actual_gcps)!=_gcp_values(shifted):raise BridgeError('CROP_SAVED_GCP_DIFF')
                elif check.crs!=src.crs or check.transform!=src.window_transform(crop):raise BridgeError('CROP_SAVED_FRAME_DIFF')
                for out_window in _windows(width,height):
                    in_window=Window(col+out_window.col_off,row+out_window.row_off,out_window.width,out_window.height)
                    if (src.read(window=in_window).tobytes()!=check.read(window=out_window).tobytes()
                        or src.read_masks(window=in_window).tobytes()!=check.read_masks(window=out_window).tobytes()):
                        raise BridgeError('CROP_SAVED_PIXEL_OR_MASK_DIFF: mask profile cannot be represented losslessly')
        metadata={'schema':'mapgis-agent/raster-crop/1','crop_window':window,'source_preserved':True,
            'pixel_check':'EXACT_SOURCE_WINDOW_AND_MASKS','resampling':'none; integer pixel copy',
            'coordinate_policy':'GCP pixel positions shifted, world positions and CRS retained; affine pixel origin translated; no invented CRS',
            'mask_policy':'raw per-band validity retained, including partial values; shared mask copied only when present',
            'statistics_policy':'stale whole-image STATISTICS_/HISTOGRAM_ tags omitted',
            'raster':_safe_info(output),'gcp_note':'Controls may fall outside the cropped pixel rectangle; no extrapolated controls invented'}
        (files/(output.name+'.json')).write_text(json_text({'sources':sources,**metadata}),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'Rasterio/GDAL integer Window read/write',**metadata})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
