"""Read-only domestic geographic sheet index, composed with existing map frames."""
from __future__ import annotations
import math
from shapely.geometry import shape,box
from shapely import get_num_coordinates
from .core import BridgeError
from .raster import explicit_crs,crs_metadata
from ._vendor import ovobj_grids as grid

CAPACITY=5000
SCALES={'1m':1000000,'500k':500000,'250k':250000,'100k':100000,'50k':50000,'25k':25000,'10k':10000,'5k':5000,'2k':2000,'1k':1000,'500':500}


def _sheet(code):
    try:data=grid.sheet_code_to_bbox(code)
    except (TypeError,ValueError) as error:raise BridgeError('SHEET_CODE: domestic canonical letter/zone, scale and row/column required') from error
    return {'code':data['code'],'scale':data['scale'],'map_scale':SCALES[data['scale']],
        'geographic_bounds':[data['lon_min'],data['lat_min'],data['lon_max'],data['lat_max']]}


def _point(point):
    if not isinstance(point,(list,tuple)) or len(point)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in point) or not 72<=point[0]<138 or not 0<=point[1]<56:
        raise BridgeError('SHEET_COORDINATES: finite longitude/latitude pairs within72<=lon<138 and0<=lat<56 required; high-latitude/global formats not implemented')


def map_sheets(action,parameters,geographic_crs):
    original=explicit_crs(geographic_crs)
    if original is None or not original.is_geographic or len(original.axis_info)!=2 or any(not math.isclose(a.unit_conversion_factor,math.pi/180,rel_tol=1e-12) for a in original.axis_info):
        raise BridgeError('SHEET_GEOGRAPHIC_CRS: explicit2D geographic CRS with degrees and xy longitude/latitude axes required')
    allowed={'index':{'points','scale'},'decode':{'codes'},'cover':{'geometry','scale','include_boundary_touch'}}
    if action not in allowed or not isinstance(parameters,dict) or set(parameters)-allowed[action]:raise BridgeError('SHEET_PARAMETERS')
    rows=[];candidates=None
    if action=='decode':
        codes=parameters.get('codes')
        if not isinstance(codes,list) or not 1<=len(codes)<=CAPACITY:raise BridgeError('SHEET_CAPACITY:1..5000 codes required')
        rows=[_sheet(c) for c in codes]
    else:
        try:scale=grid.normalize_scale(parameters.get('scale'))
        except ValueError as error:raise BridgeError('SHEET_SCALE: use1m,500k,250k,100k,50k,25k,10k,5k,2k,1k,500 or explicit scale denominator') from error
        if action=='index':
            points=parameters.get('points')
            if not isinstance(points,list) or not 1<=len(points)<=CAPACITY:raise BridgeError('SHEET_CAPACITY:1..5000 points required')
            for point in points:_point(point);rows.append(_sheet(grid.lonlat_to_sheet_code(*point,scale)))
        else:
            try:geom=shape(parameters['geometry'])
            except Exception as error:raise BridgeError('SHEET_GEOMETRY') from error
            touch=parameters.get('include_boundary_touch',False)
            if type(touch) is not bool:raise BridgeError('SHEET_PARAMETERS: include_boundary_touch is boolean')
            if geom.geom_type not in {'Polygon','MultiPolygon'} or geom.is_empty or not geom.is_valid or geom.has_z or get_num_coordinates(geom)>100000:
                raise BridgeError('SHEET_GEOMETRY: valid nonempty2D Polygon/MultiPolygon, at most100000 vertices; no automatic repair')
            w,s,e,n=geom.bounds
            if not all(math.isfinite(v) for v in geom.bounds) or not 72<=w<e<=138 or not 0<=s<n<=56:raise BridgeError('SHEET_COORDINATES: AOI outside domestic unmerged envelope')
            per=int(grid.SCALES_CONFIG[scale]['rows']);nx=11*per;ny=14*per
            x0=grid._axis_index(w,72,66,nx);x1=min(nx-1,grid._axis_index(e,72,66,nx))
            y0=grid._axis_index(s,0,56,ny);y1=min(ny-1,grid._axis_index(n,0,56,ny))
            if touch:
                if w==grid._edge(72,66,x0,nx):x0-=1
                if s==grid._edge(0,56,y0,ny):y0-=1
            x0=max(0,x0);y0=max(0,y0);candidates=(x1-x0+1)*(y1-y0+1)
            if candidates>CAPACITY:raise BridgeError('SHEET_CAPACITY: at most5000 bounding candidates; split the AOI or choose a smaller scale')
            for iy in range(y1,y0-1,-1):
                low=grid._edge(0,56,iy,ny);high=grid._edge(0,56,iy+1,ny)
                for ix in range(x0,x1+1):
                    left=grid._edge(72,66,ix,nx);right=grid._edge(72,66,ix+1,nx);cell=box(left,low,right,high)
                    if geom.intersects(cell) and (touch or geom.intersection(cell).area>0):
                        rows.append(_sheet(grid.lonlat_to_sheet_code((left+right)/2,(low+high)/2,scale)))
    return {'standard':'GB/T13989-2012','scope':'Domestic unmerged envelope72..138E,0..56N; geographic row/column scheme only',
        'action':action,'sheets':rows,'count':len(rows),'bounding_candidates':candidates,
        'geographic_crs':crs_metadata(geographic_crs,original),'coordinate_conversion_performed':False,
        'point_boundary_rule':'south/west included, north/east excluded; southwest corner identifies its sheet',
        'cover_boundary_rule':'closed-set intersection' if parameters.get('include_boundary_touch') else 'positive polygon intersection area',
        'datum_policy':'Sheet number does not specify a datum; caller supplies geographic CRS. No coordinate transformation or survey accuracy inferred.',
        'frame_next_step':'Pass a returned geographic_bounds, explicit geographic_crs and projected template CRS/map_scale to mapgis_prepare_map_frame border/grid/labels.'}
