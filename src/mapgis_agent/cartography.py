"""Explicit PROJ point transforms and native geographic frame components."""
from __future__ import annotations

import math
from pathlib import Path
import tempfile

import geopandas as gpd
from pymapgis import Writer
from pyproj.exceptions import ProjError
from pyproj.transformer import TransformerGroup
from shapely.geometry import Point,LineString,mapping

from .core import Bridge,BridgeError,read_native
from .creation import prepare_creation
from .raster import explicit_crs,crs_metadata


def _transformer(source_crs,target_crs,allow_unknown_accuracy=False):
    source,target=explicit_crs(source_crs),explicit_crs(target_crs)
    if source is None or target is None:raise BridgeError('EARTH_CRS_REQUIRED')
    group=TransformerGroup(source,target,always_xy=True,allow_ballpark=False)
    if not group.best_available:raise BridgeError('MISSING_BEST_GRID')
    if not group.transformers:raise BridgeError('PRECISE_TRANSFORM_UNAVAILABLE')
    transformer=group.transformers[0]
    same=source.geodetic_crs.equals(target.geodetic_crs,ignore_axis_order=True)
    if transformer.accuracy<0 and not same and not allow_unknown_accuracy:raise BridgeError('UNKNOWN_ACCURACY')
    return source,target,transformer


def projection_points(points:list,source_crs:dict,target_crs:dict,allow_unknown_accuracy:bool=False)->dict:
    if (not isinstance(points,list) or not points or len(points)>100000
            or any(not isinstance(p,(list,tuple)) or len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in p) for p in points)):
        raise BridgeError('COORDINATES: finite 2D numeric XY pairs required')
    source,target,transformer=_transformer(source_crs,target_crs,allow_unknown_accuracy)
    if source.is_geographic and any(not -180<=p[0]<=180 or not -90<=p[1]<=90 for p in points):raise BridgeError('GEOGRAPHIC_COORDINATES')
    try:xs,ys=transformer.transform([p[0] for p in points],[p[1] for p in points],errcheck=True)
    except ProjError as exc:raise BridgeError('PROJECTION_DOMAIN') from exc
    result=[[float(x),float(y)] for x,y in zip(xs,ys)]
    if any(not math.isfinite(v) for p in result for v in p):raise BridgeError('PROJECTION_DOMAIN')
    return {'points':result,'source_crs':crs_metadata(source_crs,source),'target_crs':crs_metadata(target_crs,target),
        'operation':transformer.description,'accuracy':transformer.accuracy if transformer.accuracy>=0 else None,
        'transformer':{'definition':transformer.definition,'always_xy':True,'ballpark':False,'best_available':True}}


def _encoding_check(bridge,target,scale):
    # Verify the pinned encoder before creating any request. It approximates
    # GRS80 as WGS84; equivalent coordinate magnitudes do not make the CRSs equal.
    with tempfile.TemporaryDirectory(prefix='frame-crs-',dir=bridge.root/'work') as folder:
        path=Path(folder)/'probe.wt'
        Writer(gpd.GeoDataFrame({'ID':[1]},geometry=[Point(0,0)],crs=target),map_scale=scale).to_file(path)
        _,reader,_=read_native(path)
        if not reader.crs or not reader.crs.equals(target):raise BridgeError('UNSUPPORTED_NATIVE_CRS_ENCODING')


def _ticks(low,high,step):
    start=math.floor(low/step)+1;stop=math.ceil(high/step)
    if stop-start>10000:raise BridgeError('FRAME_GRID_TOO_DENSE')
    return [i*step for i in range(start,stop) if low+1e-12<i*step<high-1e-12]


def _dms(value,longitude):
    seconds=round(abs(value)*3600);degrees,remainder=divmod(seconds,3600);minutes,seconds=divmod(remainder,60)
    hemisphere=('E' if value>=0 else 'W') if longitude else ('N' if value>=0 else 'S')
    return f'{degrees}°{minutes:02d}\'{seconds:02d}"{hemisphere}'


def prepare_map_frame(bridge:Bridge,part:str,parameters:dict,template:dict,request_id:str)->dict:
    if part not in {'border','grid','labels'}:raise BridgeError('UNKNOWN_PART')
    suffix='.wt' if part=='labels' else '.wl'
    if Path(template.get('name','')).suffix.lower()!=suffix:raise BridgeError('INVALID_EXTENSION')
    bounds=parameters.get('geographic_bounds');step=parameters.get('grid_step_degrees');subdivisions=parameters.get('subdivisions',32)
    if (not isinstance(bounds,list) or len(bounds)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds)
        or not -180<=bounds[0]<bounds[2]<=180 or not -90<bounds[1]<bounds[3]<90):raise BridgeError('INVALID_GEOGRAPHIC_BOUNDS')
    if type(step) not in (float,int) or not math.isfinite(step) or step<=0:raise BridgeError('INVALID_GRID_STEP')
    if type(subdivisions) is not int or not 2<=subdivisions<=1000:raise BridgeError('INVALID_SUBDIVISIONS')
    geographic=parameters.get('geographic_crs',{'definition':'EPSG:4326','axis_order':'xy'})
    original=explicit_crs(geographic);target=explicit_crs(template.get('crs',{}))
    if original is None or not original.is_geographic:raise BridgeError('GEOGRAPHIC_CRS_REQUIRED')
    if any(not math.isclose(axis.unit_conversion_factor,math.pi/180,rel_tol=1e-12) for axis in original.axis_info[:2]):raise BridgeError('GEOGRAPHIC_DEGREES_REQUIRED')
    if target is None:raise BridgeError('EARTH_CRS_REQUIRED')
    scale=template.get('map_scale')
    if type(scale) not in (int,float) or not math.isfinite(scale) or scale<=0:raise BridgeError('EXPLICIT_SCALE')
    _encoding_check(bridge,target,scale)
    west,south,east,north=bounds;xs,ys=_ticks(west,east,step),_ticks(south,north,step)
    rows=[];vertices=0;transform_metadata=None
    def add(points,text=None):
        nonlocal vertices,transform_metadata
        transformed=projection_points(points,geographic,template['crs']);transform_metadata=transformed
        geometry=Point(transformed['points'][0]) if text is not None else LineString(transformed['points'])
        row={'type':'Feature','id':str(len(rows)+1),'geometry':mapping(geometry),'properties':{'frame_part':part}}
        if text is not None:row['text']=text
        rows.append(row);vertices+=len(points)
    def segment(a,b):return [[a[0]+(b[0]-a[0])*i/subdivisions,a[1]+(b[1]-a[1])*i/subdivisions] for i in range(subdivisions+1)]
    if part=='border':
        corners=[(west,south),(east,south),(east,north),(west,north),(west,south)]
        points=[p for a,b in zip(corners,corners[1:]) for p in segment(a,b)[:-1]]+[list(corners[0])]
        add(points)
    else:
        if not xs and not ys:raise BridgeError('EMPTY_GRID')
        if (len(xs)+len(ys))*(subdivisions+1)>2_000_000:raise BridgeError('FRAME_GRID_TOO_DENSE')
        for x in xs:
            if part=='grid':add(segment((x,south),(x,north)))
            else:
                for y in (south,north):add([[x,y]],_dms(x,True))
        for y in ys:
            if part=='grid':add(segment((west,y),(east,y)))
            else:
                for x in (west,east):add([[x,y]],_dms(y,False))
    counts={'features':len(rows),'vertices':vertices,'meridians':len(xs),'parallels':len(ys),
        'meridian_ticks':2*len(xs),'parallel_ticks':2*len(ys)}
    qa=dict(template.get('qa',{}))|{'backend':'PROJ/Shapely/mapgis2shp','geographic_bounds':bounds,'counts':counts,
        'physical_scale':f'1:{scale:g}' if target.is_projected and all(math.isclose(a.unit_conversion_factor,1) for a in target.axis_info[:2]) else None,
        'official_sheet_numbering':'NOT_ASSUMED','operation':transform_metadata['operation'],'transformer':transform_metadata['transformer']}
    return prepare_creation(bridge,{'type':'FeatureCollection','features':rows},template|{'qa':qa},request_id)
