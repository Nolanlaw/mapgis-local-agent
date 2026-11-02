"""Terrain derivatives through the installed GDAL utility implementation.

Algorithms stay in GDAL. This module binds its public C API, applies the same
source/frame/atomic-artifact contract as existing grid and raster operations,
and preserves valid TPI values that collide with GDAL's -9999 output marker.
"""
from __future__ import annotations

import ctypes as C
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import threading
from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel,ConfigDict,Field
import rasterio

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,sha
from .grid_vectors import _grid_sources
from .native_raster import _raster_sources,_snapshot_raster,_native_mask_profile,raster_info
from .native_terrain import read_grid
from .raster import explicit_crs,file_sha


class SurfaceParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    operation:Literal['slope','aspect','hillshade','tri','tpi','roughness']
    vertical_unit:Literal['metre','foot','us_survey_foot']=Field(description='Declared elevation unit; horizontal units come from the explicit projected/local frame')
    algorithm:Literal['Horn','ZevenbergenThorne','Riley','Wilson']|None=None
    compute_edges:bool=Field(default=False,description='Explicit GDAL interpolation at edges/near missing cells; default requires a complete 3x3 neighborhood')
    slope_format:Literal['degree','percent']='degree'
    aspect_convention:Literal['azimuth','trigonometric']='azimuth'
    flat_as_zero:bool=Field(default=False,description='Aspect only: explicitly return zero for flat cells; default leaves their aspect undefined')
    azimuth:float=Field(default=315,ge=0,le=360)
    altitude:float=Field(default=45,gt=0,le=90)
    z_factor:float=Field(default=1,gt=0)


_LOCK=threading.Lock()
_VERTICAL={'metre':1.,'foot':.3048,'us_survey_foot':1200/3937}


@lru_cache(maxsize=1)
def _gdal():
    if os.name!='nt':raise BridgeError('DEM_RUNTIME_PROFILE: use the supplied Windows Rasterio/GDAL runtime')
    folder=Path(rasterio.__file__).resolve().parent.parent/'rasterio.libs'
    candidates=list(folder.glob('gdal-*.dll'))
    if len(candidates)!=1:raise BridgeError('DEM_RUNTIME_PROFILE: one bundled Rasterio GDAL DLL is required')
    path=candidates[0]
    with os.add_dll_directory(str(folder)):library=C.CDLL(str(path))
    signatures=[('GDALAllRegister',None,[]),('GDALVersionInfo',C.c_char_p,[C.c_char_p]),
        ('GDALOpenEx',C.c_void_p,[C.c_char_p,C.c_uint,C.c_void_p,C.c_void_p,C.c_void_p]),
        ('GDALDEMProcessingOptionsNew',C.c_void_p,[C.POINTER(C.c_char_p),C.c_void_p]),
        ('GDALDEMProcessingOptionsFree',None,[C.c_void_p]),
        ('GDALDEMProcessing',C.c_void_p,[C.c_char_p,C.c_void_p,C.c_char_p,C.c_char_p,C.c_void_p,C.POINTER(C.c_int)]),
        ('GDALClose',C.c_int,[C.c_void_p]),('CPLGetLastErrorMsg',C.c_char_p,[])]
    for name,result,args in signatures:
        function=getattr(library,name);function.restype=result;function.argtypes=args
    version=library.GDALVersionInfo(b'RELEASE_NAME').decode('ascii')
    if version!=rasterio.__gdal_version__:raise BridgeError('DEM_RUNTIME_VERSION_MISMATCH')
    library.GDALAllRegister()
    return library,{'version':version,'dll_sha256':file_sha(path),'api':'GDALDEMProcessing'}


def _linear_scale(frame,vertical_unit):
    crs=explicit_crs(frame)
    if crs is None:
        horizontal={'metre':1.,'millimetre':.001}.get(frame['unit'])
        if horizontal is None:raise BridgeError('DEM_KNOWN_LINEAR_UNITS: unknown local units cannot define terrain angles')
    else:
        if not crs.is_projected or len(crs.axis_info)<2:raise BridgeError('DEM_PROJECTED_FRAME: reproject angular coordinates to a suitable linear frame first')
        horizontal=crs.axis_info[0].unit_conversion_factor
        if not math.isfinite(horizontal) or horizontal<=0 or not math.isclose(horizontal,crs.axis_info[1].unit_conversion_factor,rel_tol=1e-12):
            raise BridgeError('DEM_AXIS_UNITS: matching known linear XY units required')
    return crs,horizontal/_VERTICAL[vertical_unit]


def _options(parameters,scale):
    operation=parameters.operation;common={'operation','vertical_unit','compute_edges'}
    extras={'slope':{'algorithm','slope_format'},'aspect':{'algorithm','aspect_convention','flat_as_zero'},
            'hillshade':{'algorithm','azimuth','altitude','z_factor'},'tri':{'algorithm'},'tpi':set(),'roughness':set()}
    if parameters.model_fields_set-common-extras[operation]:raise BridgeError('DEM_UNEXPECTED_PARAMETERS: remove options for another operation')
    # -q belongs to the CLI-only parser. The library has no progress callback
    # unless one is explicitly installed, so it already stays quiet.
    argv=[{'tri':'TRI','tpi':'TPI'}.get(operation,operation),'-of','GTiff','-co','COMPRESS=DEFLATE']
    if operation in {'slope','aspect','hillshade'}:
        algorithm=parameters.algorithm or 'Horn'
        if algorithm not in {'Horn','ZevenbergenThorne'}:raise BridgeError('DEM_ALGORITHM: gradient operations use Horn or ZevenbergenThorne')
        argv+=['-alg',algorithm]
    elif operation=='tri':
        algorithm=parameters.algorithm or 'Riley'
        if algorithm not in {'Riley','Wilson'}:raise BridgeError('DEM_ALGORITHM: TRI uses Riley or Wilson')
        argv+=['-alg',algorithm]
    if parameters.compute_edges:argv+=['-compute_edges']
    if operation in {'slope','hillshade'}:argv+=['-s',str(scale)]
    if operation=='slope' and parameters.slope_format=='percent':argv+=['-p']
    if operation=='aspect':
        if parameters.aspect_convention=='trigonometric':argv+=['-trigonometric']
        if parameters.flat_as_zero:argv+=['-zero_for_flat']
    if operation=='hillshade':argv+=['-az',str(parameters.azimuth),'-alt',str(parameters.altitude),'-z',str(parameters.z_factor)]
    return argv


def _run_dem(source,target,parameters,argv):
    with _LOCK:
        library,runtime=_gdal();dataset=None;options=None;output=None
        try:
            dataset=library.GDALOpenEx(str(source).encode('utf8'),2,None,None,None)
            if not dataset:raise BridgeError('DEM_OPEN_FAILED: '+library.CPLGetLastErrorMsg().decode('utf8','replace'))
            values=[v.encode('utf8') for v in argv];arguments=(C.c_char_p*(len(values)+1))(*values,None)
            options=library.GDALDEMProcessingOptionsNew(arguments,None)
            if not options:raise BridgeError('DEM_OPTIONS_REJECTED: '+library.CPLGetLastErrorMsg().decode('utf8','replace'))
            usage=C.c_int();mode={'tri':'TRI','tpi':'TPI','roughness':'Roughness'}.get(parameters.operation,parameters.operation)
            output=library.GDALDEMProcessing(str(target).encode('utf8'),dataset,mode.encode('ascii'),None,options,C.byref(usage))
            if not output or usage.value:raise BridgeError('DEM_PROCESSING_FAILED: '+library.CPLGetLastErrorMsg().decode('utf8','replace'))
            closed=library.GDALClose(output);output=None
            if closed:raise BridgeError('DEM_OUTPUT_CLOSE_FAILED')
            return runtime
        finally:
            if output:library.GDALClose(output)
            if options:library.GDALDEMProcessingOptionsFree(options)
            if dataset:library.GDALClose(dataset)


def prepare_grid_analysis(bridge:Bridge,file:str,expected_sha256:str,parameters:dict|SurfaceParameters,
                          template:dict,request_id:str,expected_members:dict|None=None)->dict:
    options=SurfaceParameters.model_validate(parameters)
    if set(template)!={'name','crs'} or Path(template['name']).name!=template['name'] or Path(template['name']).suffix.lower() not in {'.tif','.tiff'}:
        raise BridgeError('DEM_TEMPLATE: a new TIFF basename and explicit source CRS are required')
    crs,scale=_linear_scale(template['crs'],options.vertical_unit);argv=_options(options,scale)
    source=Path(file).resolve(strict=True);grid_sources=_grid_sources(source,expected_sha256,template['crs'])
    _,_,z,grid_info=read_grid(source)
    source_info=raster_info(source);_native_mask_profile(source,source_info)
    valid=np.flipud(~np.ma.getmaskarray(z))
    with rasterio.open(source) as ds:
        if not np.array_equal(valid,ds.read_masks(1)>0):raise BridgeError('DEM_NONFINITE_SOURCE: use an explicit finite NoData marker')
        transform=ds.transform;original_crs=ds.crs
        if original_crs and (crs is None or not crs.equals(original_crs)):raise BridgeError('GRID_CRS_MISMATCH')
    raster_sources=_raster_sources(source,expected_sha256,expected_members)
    sources=list({item['file']:item for item in raster_sources+grid_sources}.values())
    payload={'operation':'terrain.grid_analysis','file':str(source),'expected_sha256':expected_sha256,
             'parameters':options.model_dump(exclude_unset=True),'template':template,'expected_members':expected_members}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        snapshot=_snapshot_raster(source,raster_sources,stage/'input')
        files=stage/'files';files.mkdir();target=files/template['name']
        runtime=_run_dem(snapshot,target,options,argv);collisions=0
        with rasterio.open(target,'r+') as ds:
            if (ds.height,ds.width)!=tuple(grid_info['shape']) or ds.transform!=transform or ds.count!=1:raise BridgeError('DEM_GRID_CHANGED')
            if crs is not None:ds.crs=crs
            if options.operation=='tpi':
                # GDAL reserves -9999, which is also a valid negative TPI.
                # Derive validity from its documented 3x3/edge policy rather
                # than losing a valid cell by testing that numeric marker.
                expected=(valid if options.compute_edges else cv2.erode(valid.astype('uint8'),np.ones((3,3),np.uint8),borderType=cv2.BORDER_CONSTANT,borderValue=0).astype(bool))
                values=ds.read(1)
                if np.any(~np.isfinite(values[expected])):raise BridgeError('DEM_NONFINITE_RESULT')
                collisions=int(np.count_nonzero(expected&(values==ds.nodata)))
                minimum=float(values[expected].min()) if np.any(expected) else 0
                marker=float(np.float32(minimum-max(1,abs(minimum)*1e-5)))
                values[~expected]=marker;ds.nodata=marker;ds.write(values,1)
        with rasterio.open(target) as ds:
            result=ds.read(1,masked=True);data=result.compressed()
            if np.any(~np.isfinite(data)):raise BridgeError('DEM_NONFINITE_RESULT')
            metrics={'shape':[ds.height,ds.width],'valid_cells':int(data.size),'masked_cells':int(result.size-data.size),
                     'range':[float(data.min()),float(data.max())] if data.size else None,'dtype':ds.dtypes[0],
                     'nodata':ds.nodata,'transform':list(ds.transform)[:6]}
        unit=({'slope':'percent' if options.slope_format=='percent' else 'degree','aspect':'degree','hillshade':'shade_1_to_255'}
              .get(options.operation,options.vertical_unit))
        actual_parameters=options.model_dump()|{'horizontal_to_vertical_scale':scale,'gdal_arguments':argv}
        metadata={'schema':'mapgis-agent/grid/1','grid_sha256':file_sha(target),'crs':template['crs'],
                  'value_unit':unit,'source':str(source),'source_sha256':expected_sha256,
                  'analysis':actual_parameters,'metrics':metrics,'runtime':runtime}
        (files/(target.name+'.json')).write_text(json_text(metadata),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'GDALDEMProcessing; native MSI import/reopen is a separate existing operation',
            'gdal':runtime,'parameters':actual_parameters,'metrics':metrics,'value_unit':unit,'crs':template['crs'],
            'nodata_collision_pixels':collisions,'source_preserved':True,'empty_result':not bool(metrics['valid_cells'])})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
