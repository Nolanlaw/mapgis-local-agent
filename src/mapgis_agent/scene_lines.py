"""Unchanged native WL overlays for the original terrain renderer."""
from __future__ import annotations
import io,math,shutil
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from pymapgis.reader import _read_crs
from .core import BridgeError,read_native,native_ids,sha,json_text
from .raster import explicit_crs,file_sha

class LineOverlayInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    file:str
    expected_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    coordinate_frame:Literal['native_metres']
    source_crs:dict|None=None

def line_profile(file,frame,grid,source_crs=None):
    file=Path(file)
    if file.suffix.lower()!='.wl':raise BridgeError('SCENE_LINE_WL_REQUIRED')
    blob,reader,sections=read_native(file)
    if reader.shapeType!='LINE':raise BridgeError('SCENE_LINE_WL_REQUIRED')
    declared=explicit_crs(frame);_,factor=_read_crs(io.BytesIO(blob))
    if factor!=1.:raise BridgeError('SCENE_LINE_NATIVE_METRE_STORAGE: align native XY to metres before composing; normal projected storage uses map_scale=1000')
    actual=reader.crs or None
    if source_crs is not None:
        actual=explicit_crs(source_crs)
        if actual is None and json_text(source_crs)!=json_text(frame):raise BridgeError('SCENE_LINE_CRS_MISMATCH')
    if ((actual is None)!=(declared is None) or actual is not None and not actual.equals(declared)):
        raise BridgeError('SCENE_LINE_CRS_MISMATCH')
    ids=native_ids(reader);geometries=list(reader.geom)
    if not 1<=len(ids)<=20000 or len(ids)!=len(geometries):raise BridgeError('SCENE_LINE_RECORD_BOUND')
    vertices=0;bounds=[math.inf,math.inf,-math.inf,-math.inf]
    for geometry in geometries:
        if (geometry is None or geometry.is_empty or geometry.geom_type!='LineString' or not geometry.is_valid
                or geometry.has_z or getattr(geometry,'has_m',False)):raise BridgeError('SCENE_LINE_GEOMETRY')
        points=list(geometry.coords);vertices+=len(points)
        if vertices>200000 or any(not math.isfinite(value) for point in points for value in point):raise BridgeError('SCENE_LINE_VERTEX_BOUND')
        b=geometry.bounds;bounds=[min(bounds[0],b[0]),min(bounds[1],b[1]),max(bounds[2],b[2]),max(bounds[3],b[3])]
    extent=grid['bounds']
    if any(bounds[i]<extent[i] for i in (0,1)) or any(bounds[i]>extent[i] for i in (2,3)):
        raise BridgeError('SCENE_LINE_OUTSIDE_TERRAIN: clip or transform an owned branch before draping')
    if sections[0][1]%57:raise BridgeError('SCENE_LINE_NATIVE_RECORD_LAYOUT')
    return {'native':'overlay.wl','native_sha256':sha(blob),'coordinate_frame':'native_metres','frame':frame,
        'feature_count':len(ids),'physical_record_ids':ids,'record_upper_bound':sections[0][1]//57,
        'vertices':vertices,'native_bounds':bounds,'source_map_scale':1000,'source_crs':source_crs,
        'frame_evidence':'Explicit source CRS assignment, coordinates unchanged' if source_crs is not None else 'Legacy native header',
        'drape':'Original terrain height at existing input vertices; no implicit densification'}

def prepare_lines(stage,spec,frame,grid):
    source=Path(spec.file).resolve(strict=True)
    if file_sha(source)!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    profile=line_profile(source,frame,grid,spec.source_crs)
    if profile['native_sha256']!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    target=stage/'native/overlay.wl';shutil.copy2(source,target)
    if file_sha(target)!=spec.expected_sha256 or file_sha(source)!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    return profile

def line_overlay_info(folder,members,frame,grid,source_crs=None):
    if 'overlay.wl' not in members:return None
    file=Path(folder)/'overlay.wl'
    if file_sha(file)!=members['overlay.wl']:raise BridgeError('SCENE_LINE_MEMBER_CHANGED')
    return line_profile(file,frame,grid,source_crs)
