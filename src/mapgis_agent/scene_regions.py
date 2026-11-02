"""Owned CDT meshes from unchanged WP, sampled on the original DEM renderer."""
from __future__ import annotations
import io,math,shutil
from pathlib import Path
from typing import Literal
import numpy as np
from shapely import intersects_xy,segmentize
from pydantic import BaseModel,ConfigDict,Field
from pymapgis.reader import _read_crs
from .core import BridgeError,read_native,native_ids,sha,json_text
from .raster import explicit_crs,file_sha

class RegionOverlayInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    file:str
    expected_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    coordinate_frame:Literal['native_metres']
    source_crs:dict|None=None
    opacity:float=Field(default=1.,ge=0,le=1)
    visible:bool=True
    height_offset_m:float=Field(default=0.,ge=-1e6,le=1e6)
    max_view_distance_m:float=Field(default=1.7976931348623157e308,gt=0)

def region_profile(file,frame,grid,options):
    file=Path(file)
    if file.suffix.lower()!='.wp':raise BridgeError('SCENE_REGION_WP_REQUIRED')
    blob,reader,sections=read_native(file)
    if reader.shapeType!='POLYGON':raise BridgeError('SCENE_REGION_WP_REQUIRED')
    _,factor=_read_crs(io.BytesIO(blob))
    if factor!=1.:raise BridgeError('SCENE_REGION_NATIVE_METRE_STORAGE: projected native storage requires map_scale1000')
    actual=reader.crs or None;declared=explicit_crs(frame)
    if options.source_crs is not None:
        actual=explicit_crs(options.source_crs)
        if actual is None and json_text(options.source_crs)!=json_text(frame):raise BridgeError('SCENE_REGION_CRS_MISMATCH')
    if ((actual is None)!=(declared is None) or actual is not None and not actual.equals(declared)):raise BridgeError('SCENE_REGION_CRS_MISMATCH')
    ids=native_ids(reader);geometries=list(reader.geom)
    if sections[8][1]%40:raise BridgeError('SCENE_REGION_NATIVE_RECORD_LAYOUT')
    upper=sections[8][1]//40
    if not 1<=len(ids)<=20000 or len(ids)!=len(geometries) or upper>200001:raise BridgeError('SCENE_REGION_RECORD_BOUND')
    xmin,ymin,xmax,ymax=grid['bounds'];dx=(xmax-xmin)/(grid['columns']-1);dy=(ymax-ymin)/(grid['rows']-1)
    step=min(dx,dy);total=0;records=[];chunks=['MGSR0001\n',str(len(ids))+'\n']
    for row,(rid,geometry) in enumerate(zip(ids,geometries)):
        if (geometry is None or geometry.is_empty or geometry.geom_type not in {'Polygon','MultiPolygon'} or not geometry.is_valid
                or geometry.has_z or getattr(geometry,'has_m',False) or geometry.area<=0 or not all(math.isfinite(v) for v in geometry.bounds)):
            raise BridgeError('SCENE_REGION_GEOMETRY')
        left,bottom,right,top=geometry.bounds
        if left<xmin or bottom<ymin or right>xmax or top>ymax:raise BridgeError('SCENE_REGION_OUTSIDE_TERRAIN')
        parts=list(geometry.geoms) if geometry.geom_type=='MultiPolygon' else [geometry]
        original_vertices=sum(len(ring.coords)-1 for part in parts for ring in [part.exterior,*part.interiors])
        if original_vertices>200000 or geometry.length/step>200000:raise BridgeError('SCENE_REGION_BOUNDARY_BOUND')
        sampled=segmentize(geometry,step);parts=list(sampled.geoms) if sampled.geom_type=='MultiPolygon' else [sampled]
        points=[];edges=[];index={};rings=0
        def add(xy):
            xy=(float(xy[0]),float(xy[1]))
            if xy not in index:
                if total+len(points)>=100000:raise BridgeError('SCENE_REGION_MESH_BOUND: crop an owned branch or explicitly resample the DEM before composing')
                index[xy]=len(points);points.append(xy)
            return index[xy]
        for part in parts:
            for ring in [part.exterior,*part.interiors]:
                rings+=1;vertices=[add(xy) for xy in list(ring.coords)[:-1]]
                edges.extend(zip(vertices,vertices[1:]+vertices[:1]))
        if len(edges)>100000:raise BridgeError('SCENE_REGION_MESH_BOUND')
        c0=max(0,math.ceil((left-xmin)/dx));c1=min(grid['columns']-1,math.floor((right-xmin)/dx))
        r0=max(0,math.ceil((bottom-ymin)/dy));r1=min(grid['rows']-1,math.floor((top-ymin)/dy))
        xs=xmin+np.arange(c0,c1+1,dtype='float64')*dx
        for start in range(r0,r1+1,128):
            ys=ymin+np.arange(start,min(start+128,r1+1),dtype='float64')*dy
            xx,yy=np.meshgrid(xs,ys);inside=intersects_xy(geometry,xx,yy)
            for x,y in zip(xx[inside],yy[inside]):add((x,y))
        total+=len(points)
        chunks.append(f'{rid} {len(points)} {len(edges)}\n')
        chunks.extend(f'{x:.17g} {y:.17g}\n' for x,y in points);chunks.extend(f'{a} {b}\n' for a,b in edges)
        records.append({'id':rid,'area':geometry.area,'polygons':len(parts),'rings':rings,'mesh_vertices':len(points),'constraints':len(edges),
            'fill_color':int(reader.polygon_params.iloc[row]['fill_color'])})
    mesh=''.join(chunks).encode('ascii')
    return {'native':'regions.wp','native_sha256':sha(blob),'mesh':'regions.mesh','mesh_sha256':sha(mesh),'mesh_vertices':total,'boundary_step_m':step,
        'coordinate_frame':'native_metres','frame':frame,'source_crs':options.source_crs,
        'frame_evidence':'Explicit source CRS assignment, coordinates unchanged' if options.source_crs is not None else 'Legacy native header',
        'feature_count':len(ids),'record_upper_bound':upper,'records':records,
        'opacity':options.opacity,'visible':options.visible,'height_offset_m':options.height_offset_m,'max_view_distance_m':options.max_view_distance_m,
        'backend':'Unmodified CDT2.0.1; original native DEM bilinear height and WP fill palette; owned standard OpenGL composition',
        'scope':'Solid region fills, holes and multipart footprints. Boundary sampling and covered DEM nodes form a derived mesh, not a proprietary MapGIS scene. No licensed TinBas triangulator call; 2D fill patterns and per-region texture images are not applied.'},mesh

def prepare_regions(stage,spec,frame,grid):
    source=Path(spec.file).resolve(strict=True)
    if file_sha(source)!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    profile,mesh=region_profile(source,frame,grid,spec)
    if profile['native_sha256']!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    shutil.copy2(source,stage/'native/regions.wp');(stage/'native/regions.mesh').write_bytes(mesh)
    if file_sha(stage/'native/regions.wp')!=spec.expected_sha256 or file_sha(source)!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    return profile

def region_info(folder,members,frame,grid,description):
    names={'regions.wp','regions.mesh'}
    if not names&members.keys():return None
    fields=('source_crs','opacity','visible','height_offset_m','max_view_distance_m')
    if not names<=members.keys() or not isinstance(description,dict) or not set(fields)<=description.keys():raise BridgeError('SCENE_REGION_BINDING')
    folder=Path(folder)
    if any(file_sha(folder/name)!=members[name] for name in names):raise BridgeError('SCENE_REGION_MEMBER_CHANGED')
    options=RegionOverlayInput(file=str(folder/'regions.wp'),expected_sha256=members['regions.wp'],coordinate_frame='native_metres',**{k:description[k] for k in fields})
    profile,mesh=region_profile(folder/'regions.wp',frame,grid,options)
    if mesh!=(folder/'regions.mesh').read_bytes():raise BridgeError('SCENE_REGION_MESH_BINDING')
    return profile

def region_settings(profile):
    return ' '.join(map(str,[1,profile['feature_count'],int(profile['visible']),profile['opacity'],profile['height_offset_m'],profile['max_view_distance_m']]))+'\n'

def validate_region_readback(events,profile,*,bound=False):
    headers=[v for v in events if v.get('event')=='scene_regions_readback']
    rows=[v for v in events if v.get('event')=='scene_region_record']
    if profile is None:
        if headers or rows:raise BridgeError('SCENE_REGION_EXECUTION')
        return None
    if (len(headers)!=1 or (headers[0].get('active'),headers[0].get('record_upper_bound'),headers[0].get('mesh_vertices'))
            !=(profile['feature_count'],profile['record_upper_bound'],profile['mesh_vertices']) or len(rows)!=profile['feature_count']):
        raise BridgeError('SCENE_REGION_READBACK')
    if rows!=[{'event':'scene_region_record','record_id':v['id'],'fill_color':v['fill_color']} for v in profile['records']]:
        raise BridgeError('SCENE_REGION_NATIVE_RECORD_READBACK')
    if not bound:return headers[0]
    loaded=[v for v in events if v.get('event')=='scene_regions_bound'];meshes=[v for v in events if v.get('event')=='scene_region_mesh']
    if (len(loaded)!=1 or len(meshes)!=profile['feature_count'] or loaded[0].get('active')!=profile['feature_count']
            or loaded[0].get('mesh_vertices')!=profile['mesh_vertices'] or loaded[0].get('licensed_triangulator_called') is not False
            or loaded[0].get('visible') is not profile['visible']
            or np.float32(loaded[0].get('opacity'))!=np.float32(profile['opacity'])):raise BridgeError('SCENE_REGION_RENDER_READBACK')
    for actual,expected in zip(meshes,profile['records']):
        if (actual.get('record_id'),actual.get('vertices'),actual.get('fill_color'))!=(expected['id'],expected['mesh_vertices'],expected['fill_color']):raise BridgeError('SCENE_REGION_MESH_READBACK')
        if not math.isclose(actual.get('area',float('nan')),expected['area'],rel_tol=1e-10,abs_tol=0):raise BridgeError('SCENE_REGION_MESH_FOOTPRINT')
        if not 1<=actual.get('triangles',0)<=2*expected['mesh_vertices']:raise BridgeError('SCENE_REGION_MESH_READBACK')
    if loaded[0].get('triangles')!=sum(v['triangles'] for v in meshes):raise BridgeError('SCENE_REGION_MESH_READBACK')
    return loaded[0]|{'meshes':meshes,'backend':profile['backend']}
