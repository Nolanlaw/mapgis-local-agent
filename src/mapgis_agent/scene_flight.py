"""Bounded original terrain flights, recorded through Windows AVI APIs."""
from __future__ import annotations
import math,re
from typing import Annotated
import numpy as np
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from .core import BridgeError

class FlightParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    points:list[Annotated[list[float],Field(min_length=2,max_length=2)]]=Field(min_length=2,max_length=5000)
    speed:float=Field(gt=0,description='Original native spatial step, within returned fly_speed_range; not metres per second.')
    frame_start:StrictInt=Field(default=0,ge=0,le=999)
    frame_count:StrictInt|None=Field(default=None,ge=1,le=1000)
    fps:StrictInt=Field(default=24,ge=1,le=60)
    max_xy_rounding_error_m:float=Field(default=0.,ge=0)

def flight_settings(spec,grid,width,height):
    points=np.asarray(spec.points,dtype='float64');converted=points.astype('float32')
    if not np.isfinite(converted).all():raise BridgeError('SCENE_FLIGHT_FINITE_COORDINATES')
    if float(np.max(np.abs(points-converted.astype('float64'))))>spec.max_xy_rounding_error_m:
        raise BridgeError('SCENE_FLIGHT_FLOAT32_ROUNDING')
    if np.any(np.all(converted[1:]==converted[:-1],axis=1)):raise BridgeError('SCENE_FLIGHT_DUPLICATE_KNOT')
    xmin,ymin,xmax,ymax=grid['bounds']
    if np.any(converted[:,0]<xmin) or np.any(converted[:,0]>xmax) or np.any(converted[:,1]<ymin) or np.any(converted[:,1]>ymax):
        raise BridgeError('SCENE_FLIGHT_OUTSIDE_TERRAIN')
    if spec.frame_count and spec.frame_start+spec.frame_count>1000:raise BridgeError('SCENE_FLIGHT_STEP_BOUND')
    if spec.frame_count and ((width*3+3)//4*4)*height*spec.frame_count>1024**3-65536:
        raise BridgeError('SCENE_FLIGHT_OUTPUT_SIZE: uncompressed AVI must remain below1GiB')
    values=[len(points),spec.frame_start,spec.frame_count or 0,spec.fps,spec.speed,*converted.astype('float64').ravel().tolist()]
    return ' '.join(map(str,values))+'\n'

def validate_flight(events,readback,spec,width,height):
    written=[v for v in events if v.get('event')=='flight_written']
    inspected=[v for v in readback if v.get('event')=='flight_readback']
    if len(written)!=1 or len(inspected)!=1:raise BridgeError('SCENE_FLIGHT_INCOMPLETE')
    info=written[0];count=info.get('generated_frames',0);frames=info.get('frames',0)
    wanted=spec.frame_count or count-spec.frame_start
    if (not 2<=count<=100000 or not 1<=frames<=1000 or frames!=wanted or info.get('frame_start')!=spec.frame_start
            or spec.frame_start+frames>count or any(info.get(k)!=v or inspected[0].get(k)!=v for k,v in
                {'frames':frames,'fps':spec.fps,'width':width,'height':height}.items())):
        raise BridgeError('SCENE_FLIGHT_PROFILE_READBACK')
    limits=info.get('speed_range',[])
    if (len(limits)!=2 or not all(math.isfinite(v) for v in limits)
            or not np.float32(limits[0])<=np.float32(info.get('speed',-1))<=np.float32(limits[1])
            or np.float32(info['speed'])!=np.float32(spec.speed)):raise BridgeError('SCENE_FLIGHT_SPEED_READBACK')
    original=[v for v in events if v.get('event')=='flight_frame']
    decoded=[v for v in readback if v.get('event')=='flight_frame_readback']
    if len(original)!=frames or len(decoded)!=frames:raise BridgeError('SCENE_FLIGHT_FRAME_COUNT')
    for index,(frame,actual) in enumerate(zip(original,decoded)):
        expected=spec.frame_start+index
        if frame.get('index')!=index or frame.get('route_index')!=expected or frame.get('next_route_index')!=(expected+1)%count:
            raise BridgeError('SCENE_FLIGHT_ADVANCE_READBACK')
        eye=frame.get('eye',[])
        if len(eye)!=3 or not all(math.isfinite(v) for v in eye):raise BridgeError('SCENE_FLIGHT_EYE_READBACK')
        if (actual.get('index')!=index or not re.fullmatch('[0-9a-f]{64}',frame.get('rgb_sha256',''))
                or actual.get('rgb_sha256')!=frame['rgb_sha256']):raise BridgeError('SCENE_FLIGHT_PIXEL_READBACK')
    return {k:v for k,v in info.items() if k!='event'}|{'file':'flight.avi','codec':'Windows VFW uncompressed24bit DIB',
        'input':spec.model_dump(),'frame_details':[{k:v for k,v in frame.items() if k!='event'} for frame in original],
        'pixel_check':'EXACT every original RGB frame vs fresh Windows AVI stream readback',
        'scope':'Original native route interpolation and per-frame camera/height behavior; no vendor modal recording dialog, no inferred geodetic accuracy.'}
