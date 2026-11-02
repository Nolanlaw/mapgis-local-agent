"""Unchanged WT text anchors for the original terrain annotation renderer."""
from __future__ import annotations
import io,math,shutil
from pathlib import Path
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictInt,field_validator
from pymapgis.reader import _read_crs
from .core import BridgeError,read_native,native_ids,annotation_text,sha,json_text
from .raster import explicit_crs,file_sha

class AnnotationFont(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    face:str='SimSun'
    points:StrictInt=Field(default=14,ge=1,le=30)
    bold:bool=False
    italic:bool=False

    @field_validator('face')
    @classmethod
    def native_face(cls,value):
        try:encoded=value.encode('gbk',errors='strict')
        except UnicodeEncodeError as error:raise ValueError('font face must fit CP936') from error
        if not value.strip() or len(encoded)>31 or any(ord(c)<32 for c in value):
            raise ValueError('font face must fit Windows LOGFONT and contain no controls')
        return value

class AnnotationOverlayInput(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    file:str
    expected_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    coordinate_frame:Literal['native_metres']
    source_crs:dict|None=None
    font:AnnotationFont=Field(default_factory=AnnotationFont)
    height_offset_m:float=Field(default=5.,ge=-1e6,le=1e6)
    max_view_distance_m:float=Field(default=1.7976931348623157e308,gt=0)
    visible:bool=True

def annotation_profile(file,frame,grid,options):
    file=Path(file)
    if file.suffix.lower()!='.wt':raise BridgeError('SCENE_ANNOTATION_WT_REQUIRED')
    blob,reader,sections=read_native(file)
    if reader.shapeType!='POINT':raise BridgeError('SCENE_ANNOTATION_WT_REQUIRED')
    _,factor=_read_crs(io.BytesIO(blob))
    if factor!=1.:raise BridgeError('SCENE_ANNOTATION_NATIVE_METRE_STORAGE: projected native storage requires map_scale1000')
    source_crs=options.source_crs;actual=reader.crs or None;declared=explicit_crs(frame)
    if source_crs is not None:
        actual=explicit_crs(source_crs)
        if actual is None and json_text(source_crs)!=json_text(frame):raise BridgeError('SCENE_ANNOTATION_CRS_MISMATCH')
    if ((actual is None)!=(declared is None) or actual is not None and not actual.equals(declared)):
        raise BridgeError('SCENE_ANNOTATION_CRS_MISMATCH')
    ids=native_ids(reader);geometries=list(reader.geom)
    if sections[0][1]%93:raise BridgeError('SCENE_ANNOTATION_NATIVE_RECORD_LAYOUT')
    upper=sections[0][1]//93
    if not 1<=len(ids)<=20000 or len(ids)!=len(geometries) or upper>200001:raise BridgeError('SCENE_ANNOTATION_RECORD_BOUND')
    records=[];total=0;extent=grid['bounds']
    for rid,geometry in zip(ids,geometries):
        text=annotation_text(blob,sections,rid)
        if text is None:raise BridgeError('SCENE_ANNOTATION_ORDINARY_TEXT_REQUIRED')
        try:encoded=text.encode('gb2312',errors='strict')
        except UnicodeEncodeError as error:raise BridgeError('SCENE_ANNOTATION_GB2312_REQUIRED: original glyph parser cannot render all GBK extensions') from error
        total+=len(encoded)
        if (not text.strip() or len(encoded)>32767 or total>1048576
                or any(ord(c)<32 or ord(c)==127 for c in text)):raise BridgeError('SCENE_ANNOTATION_PLAIN_TEXT_BOUND')
        if (geometry is None or geometry.is_empty or geometry.geom_type!='Point' or geometry.has_z or getattr(geometry,'has_m',False)
                or not all(math.isfinite(v) for v in geometry.coords[0])):raise BridgeError('SCENE_ANNOTATION_GEOMETRY')
        x,y=geometry.coords[0]
        if not extent[0]<=x<=extent[2] or not extent[1]<=y<=extent[3]:raise BridgeError('SCENE_ANNOTATION_OUTSIDE_TERRAIN')
        records.append({'id':rid,'text':text,'anchor':[x,y]})
    return {'native':'notes.wt','native_sha256':sha(blob),'coordinate_frame':'native_metres','frame':frame,
        'feature_count':len(ids),'record_upper_bound':upper,'records':records,'source_crs':source_crs,
        'frame_evidence':'Explicit source CRS assignment, coordinates unchanged' if source_crs is not None else 'Legacy native header',
        'font':options.font.model_dump(),'height_offset_m':options.height_offset_m,'max_view_distance_m':options.max_view_distance_m,'visible':options.visible,
        'text_scope':'Plain GB2312 text; original GDI glyph metrics and coverage checked before rendering. Native per-record text size/angle are not 3D font settings.'}

def prepare_annotations(stage,spec,frame,grid):
    source=Path(spec.file).resolve(strict=True)
    if file_sha(source)!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    profile=annotation_profile(source,frame,grid,spec)
    if profile['native_sha256']!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    target=stage/'native/notes.wt';shutil.copy2(source,target)
    if file_sha(target)!=spec.expected_sha256 or file_sha(source)!=spec.expected_sha256:raise BridgeError('STALE_SOURCE')
    return profile

def annotation_info(folder,members,frame,grid,description):
    if 'notes.wt' not in members:return None
    fields=('source_crs','font','height_offset_m','max_view_distance_m','visible')
    if not isinstance(description,dict) or not set(fields)<=description.keys():raise BridgeError('SCENE_ANNOTATION_BINDING')
    file=Path(folder)/'notes.wt'
    if file_sha(file)!=members['notes.wt']:raise BridgeError('SCENE_ANNOTATION_MEMBER_CHANGED')
    options=AnnotationOverlayInput(file=str(file),expected_sha256=members['notes.wt'],coordinate_frame='native_metres',
        **{k:description[k] for k in fields})
    return annotation_profile(file,frame,grid,options)

def annotation_settings(profile):
    font=profile['font'];flags=int(font['bold'])+2*int(font['italic'])
    values=[1,profile['feature_count'],flags,font['points'],profile['height_offset_m'],profile['max_view_distance_m'],int(profile['visible']),font['face'].encode('gbk').hex()]
    return ' '.join(map(str,values))+'\n'+''.join(str(v['id'])+'\n' for v in profile['records'])

def validate_annotation_readback(events,profile,*,bound=False):
    headers=[v for v in events if v.get('event')=='scene_annotations_readback']
    records=[v for v in events if v.get('event')=='scene_annotation_record']
    if profile is None:
        if headers or records:raise BridgeError('SCENE_ANNOTATION_EXECUTION')
        return None
    if (len(headers)!=1 or (headers[0].get('active'),headers[0].get('record_upper_bound'))!=(profile['feature_count'],profile['record_upper_bound'])
            or len(records)!=profile['feature_count']):raise BridgeError('SCENE_ANNOTATION_READBACK')
    for actual,expected in zip(records,profile['records']):
        if actual.get('record_id')!=expected['id'] or actual.get('xy')!=expected['anchor'] or actual.get('text_hex')!=expected['text'].encode('gb2312').hex():
            raise BridgeError('SCENE_ANNOTATION_NATIVE_TEXT_READBACK')
    if not bound:return headers[0]
    loaded=[v for v in events if v.get('event')=='scene_annotations_bound']
    if (len(loaded)!=1 or loaded[0].get('active')!=profile['feature_count'] or loaded[0].get('font_points')!=profile['font']['points']
            or loaded[0].get('font_flags')!=int(profile['font']['bold'])+2*int(profile['font']['italic'])
            or loaded[0].get('visible') is not profile['visible'] or not loaded[0].get('glyph_coverage_checked')
            or not loaded[0].get('atlas_fit_checked')):raise BridgeError('SCENE_ANNOTATION_RENDER_READBACK')
    return loaded[0]
