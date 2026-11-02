"""Headless TIN, grid, contour and cut/fill operations over native WT points.

SciPy supplies triangulation/interpolation, ContourPy supplies isolines; native
vectors are encoded by the existing creation path. XY and heights must share units.
"""
from __future__ import annotations

import math
import json
from numbers import Real
from pathlib import Path

import contourpy
import numpy as np
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import Delaunay, QhullError
from shapely.geometry import LineString, Point, Polygon, mapping

from .core import Bridge, BridgeError, json_text, native_ids, read_native, sha,require_native_crs
from .creation import prepare_creation
from .raster import explicit_crs


def _surface(file, expected_sha256, height_field):
    path=Path(file).resolve(strict=True)
    blob,reader,_=read_native(path)
    if sha(blob)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if reader.shapeType!='POINT':raise BridgeError('TERRAIN_POINTS_REQUIRED')
    if height_field not in reader.data:raise BridgeError('MISSING_HEIGHT_FIELD')
    if reader.crs and reader.crs.is_geographic:raise BridgeError('TERRAIN_PROJECTED_OR_LOCAL_COORDINATES_REQUIRED')
    values=reader.data[height_field].tolist()
    if not all(isinstance(v,Real) and not isinstance(v,(bool,np.bool_)) and math.isfinite(v) for v in values):
        raise BridgeError('INVALID_HEIGHT: finite numeric heights required')
    unique={}
    for rid,g,z in zip(native_ids(reader),reader.geom,values):
        key=(float(g.x),float(g.y))
        if not all(math.isfinite(v) for v in key):raise BridgeError('INVALID_TERRAIN_COORDINATE')
        if key in unique and unique[key][0]!=float(z):raise BridgeError('INCONSISTENT_DUPLICATE_POINTS')
        unique.setdefault(key,(float(z),rid))
    if len(unique)<3:raise BridgeError('INSUFFICIENT_TERRAIN_POINTS')
    xy=np.array(list(unique),dtype=float)
    z=np.array([v[0] for v in unique.values()],dtype=float)
    if np.linalg.matrix_rank(xy-xy[0])<2:raise BridgeError('COLLINEAR_INPUT')
    try:tri=Delaunay(xy)
    except QhullError as exc:raise BridgeError('TERRAIN_TRIANGULATION: degenerate or ill-conditioned coordinates') from exc
    if len(np.unique(tri.simplices))!=len(xy):raise BridgeError('TERRAIN_PRECISION: some height observations were omitted')
    return path,xy,z,tri


def _area(a,b,c):
    return abs(float((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])))*.5


def _positive_volume(vertices):
    # A linear height field crosses each triangle edge at most once. Integrate
    # the positive clipped polygon exactly by a fan, rather than sampling a grid.
    clipped=[]
    for a,b in zip(vertices,np.roll(vertices,-1,axis=0)):
        inside_a,inside_b=a[2]>=0,b[2]>=0
        if inside_a!=inside_b:
            clipped.append(a+(b-a)*(a[2]/(a[2]-b[2])))
        if inside_b:clipped.append(b)
    if len(clipped)<3:return 0.0
    a=clipped[0]
    return math.fsum(_area(a,b,c)*float((a[2]+b[2]+c[2])/3)
                     for b,c in zip(clipped[1:-1],clipped[2:]))


def _metrics(xy,z,tri,base_height=None):
    if base_height is not None and (type(base_height) not in (float,int) or not math.isfinite(base_height)):
        raise BridgeError('INVALID_BASE_HEIGHT')
    xyz=np.column_stack((xy,z))
    area,surface,cut,fill=[],[],[],[]
    for ids in tri.simplices:
        vertices=xyz[ids]
        area.append(_area(*vertices))
        surface.append(float(np.linalg.norm(np.cross(vertices[1]-vertices[0],vertices[2]-vertices[0])))*.5)
        if base_height is not None:
            relative=vertices.copy();relative[:,2]-=base_height
            cut.append(_positive_volume(relative))
            relative[:,2]*=-1
            fill.append(_positive_volume(relative))
    result={'backend':'SciPy/NumPy','counts':{'points':len(xy),'triangles':len(tri.simplices)},
            'planimetric_hull_area':math.fsum(area),'triangulated_surface_area':math.fsum(surface),
            'units':'source XY units; input heights must use the same linear unit'}
    if base_height is not None:result.update(base_height=base_height,cut_volume=math.fsum(cut),fill_volume=math.fsum(fill))
    return result


def analyze_terrain(file:str,expected_sha256:str,height_field:str,base_height:float|None=None)->dict:
    path,xy,z,tri=_surface(file,expected_sha256,height_field)
    result=_metrics(xy,z,tri,base_height)
    if sha(path.read_bytes())!=expected_sha256:raise BridgeError('STALE_DURING_TERRAIN')
    return result|{'source':str(path),'source_sha256':expected_sha256,'source_preserved':True}


def _grid(xy,z,tri,settings):
    if not isinstance(settings,dict) or set(settings)!={'bounds','step'}:raise BridgeError('TERRAIN_GRID: bounds and step required')
    bounds,step=settings['bounds'],settings['step']
    if (not isinstance(bounds,list) or len(bounds)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in bounds)
            or type(step) not in (int,float) or not math.isfinite(step) or step<=0
            or bounds[2]<=bounds[0] or bounds[3]<=bounds[1]):raise BridgeError('TERRAIN_GRID: finite ordered bounds and positive step required')
    intervals=[(bounds[i+2]-bounds[i])/step for i in (0,1)]
    if any(not math.isclose(v,round(v),rel_tol=0,abs_tol=1e-8) for v in intervals):
        raise BridgeError('TERRAIN_GRID_ALIGNMENT: bounds must contain an integral number of steps')
    nx,ny=(int(round(v))+1 for v in intervals)
    if nx*ny>2_000_000:raise BridgeError('TERRAIN_GRID_WINDOW: request bounded windows of at most 2000000 samples')
    xs=bounds[0]+np.arange(nx)*step;ys=bounds[1]+np.arange(ny)*step
    xx,yy=np.meshgrid(xs,ys)
    zz=LinearNDInterpolator(tri,z,fill_value=np.nan)(xx,yy)
    return xs,ys,xx,yy,zz


def prepare_terrain(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str)->dict:
    if parameters.get('operation')=='native.tin':
        from .native_tin import prepare_native_tin
        return prepare_native_tin(bridge,file,expected_sha256,parameters,template,request_id)
    if parameters.get('operation')=='native.idw':
        from .native_terrain import prepare_native_idw
        return prepare_native_idw(bridge,file,expected_sha256,parameters,template,request_id)
    operation=parameters.get('operation')
    allowed={'tin':{'operation','height_field'},'grid':{'operation','height_field','bounds','step'},
             'contours':{'operation','height_field','levels','grid'}}
    if operation not in allowed or set(parameters)!=allowed[operation]:raise BridgeError('UNEXPECTED_PARAMETERS')
    extension={'tin':'.wp','grid':'.wt','contours':'.wl'}[operation]
    if Path(template.get('name','')).suffix.lower()!=extension:raise BridgeError('INVALID_TEMPLATE_NAME')
    path,xy,z,tri=_surface(file,expected_sha256,parameters['height_field'])
    # A different declared output CRS must be a separate, explicit reprojection.
    _,source_reader,_=read_native(path)
    require_native_crs(source_reader,template.get('crs',{}),'TERRAIN_CRS_MISMATCH')
    metrics=_metrics(xy,z,tri)['counts'].copy()
    rows=[]
    def add(geometry,height):
        rows.append({'type':'Feature','id':f'{operation}-{len(rows)+1}',
                     'geometry':mapping(geometry),'properties':{'elevation':float(height)}})
    if operation=='tin':
        for ids in tri.simplices:add(Polygon(xy[ids]),np.mean(z[ids]))
    else:
        settings=parameters['grid'] if operation=='contours' else {k:parameters[k] for k in ('bounds','step')}
        xs,ys,xx,yy,zz=_grid(xy,z,tri,settings)
        metrics.update(grid_shape=[len(ys),len(xs)],masked_points=int(np.count_nonzero(~np.isfinite(zz))))
        if operation=='grid':
            for x,y,height in zip(xx.ravel(),yy.ravel(),zz.ravel()):
                if math.isfinite(height):add(Point(float(x),float(y)),height)
            metrics['point_count']=len(rows)
        else:
            levels=parameters['levels']
            if (not isinstance(levels,list) or not levels or len(set(levels))!=len(levels)
                    or any(type(v) not in (float,int) or not math.isfinite(v) for v in levels)):
                raise BridgeError('TERRAIN_LEVELS: explicit unique finite contour levels required')
            generator=contourpy.contour_generator(x=xs,y=ys,z=np.ma.masked_invalid(zz),line_type='Separate')
            empty=[]
            for level in sorted(levels):
                lines=generator.lines(level)
                valid=[line for line in lines if len(line)>1 and LineString(line).length>0]
                for line in valid:add(LineString(line),level)
                if not valid:empty.append(level)
            metrics.update(requested_levels=sorted(levels),empty_levels=empty)
    if not rows:raise BridgeError('TERRAIN_EMPTY_RESULT: no sample or isoline within the requested hull/levels')
    if sha(path.read_bytes())!=expected_sha256:raise BridgeError('STALE_DURING_TERRAIN')
    provenance=dict(template.get('provenance',{}))|{'backend':'SciPy/NumPy/ContourPy','operation':operation,
        'input_file':str(path),'input_sha256':expected_sha256,'parameters':parameters,'metrics':metrics}
    prepared=prepare_creation(bridge,{'type':'FeatureCollection','features':rows},template|{'provenance':provenance},request_id)
    extra={'backend':'SciPy/NumPy','input_file':str(path),'input_sha256':expected_sha256,'metrics':metrics}
    with bridge.connect() as db:
        old=db.execute('SELECT receipt FROM commands WHERE id=?',(request_id,)).fetchone()
        receipt=json.loads(old['receipt'])|extra
        db.execute('UPDATE commands SET receipt=? WHERE id=?',(json_text(receipt),request_id))
    return prepared|extra
