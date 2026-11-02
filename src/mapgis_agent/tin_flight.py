"""Original TIN PAT routes with explicit coordinate precision and exact AVI checks."""
from __future__ import annotations
import re
from typing import Annotated
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from .core import BridgeError


class TinFlightParameters(BaseModel):
    model_config=ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    points:list[Annotated[list[float], Field(min_length=3, max_length=3)]]=Field(min_length=2, max_length=100)
    speed:float=Field(gt=0, le=1000, description='Original PAT spatial speed parameter, not metres per second.')
    frame_start:StrictInt=Field(default=0, ge=0, le=999)
    frame_count:StrictInt|None=Field(default=None, ge=1, le=1000)
    fps:StrictInt=Field(default=24, ge=1, le=60)
    max_coordinate_error_m:float=Field(default=0., ge=0)


def flight_settings(spec, pose, width, height):
    points=np.asarray(spec.points, dtype='float64')
    origin=np.asarray([*pose.origin, 0.], dtype='float64')
    scales=np.asarray(pose.scales, dtype='float64')
    local=(points-origin)*scales
    if not np.isfinite(local).all(): raise BridgeError('TIN_FLIGHT_FINITE_COORDINATES')
    rows=[]; native=[]
    speed=float(f'{spec.speed:.5f}')
    if speed<=0 or np.float32(speed)!=np.float32(spec.speed): raise BridgeError('TIN_FLIGHT_SPEED_PRECISION')
    for point in local:
        row=','.join(f'{value:.5f}' for value in [*point, speed])+'\r\n'
        if len(row.encode('ascii'))>=50: raise BridgeError('TIN_FLIGHT_PAT_ROW_BOUND')
        rows.append(row)
        native.append([np.float32(float(value)) for value in row.strip().split(',')[:3]])
    native=np.asarray(native, dtype='float64')
    if not np.isfinite(native).all(): raise BridgeError('TIN_FLIGHT_FINITE_COORDINATES')
    if float(np.max(np.abs(native/scales+origin-points)))>spec.max_coordinate_error_m:
        raise BridgeError('TIN_FLIGHT_COORDINATE_ROUNDING')
    if np.any(np.all(native[1:,:2]==native[:-1,:2], axis=1)):
        raise BridgeError('TIN_FLIGHT_DUPLICATE_NATIVE_KNOT')
    if spec.frame_count and spec.frame_start+spec.frame_count>1000: raise BridgeError('TIN_FLIGHT_STEP_BOUND')
    if spec.frame_count and ((width*3+3)//4*4)*height*spec.frame_count>1024**3-65536:
        raise BridgeError('TIN_FLIGHT_OUTPUT_SIZE')
    return ' '.join(map(str, [len(points), spec.frame_start, spec.frame_count or 0,
        spec.fps, speed, *native.ravel().tolist()]))+'\n'


def validate_flight(events, readback, spec, width, height):
    written=[v for v in events if v.get('event')=='tin_flight_written']
    inspected=[v for v in readback if v.get('event')=='flight_readback']
    if len(written)!=1 or len(inspected)!=1: raise BridgeError('TIN_FLIGHT_INCOMPLETE')
    info=written[0]; count=info.get('generated_frames', 0); frames=info.get('frames', 0)
    wanted=spec.frame_count or count-spec.frame_start
    if (not 2<=count<=info.get('capacity', 0)<=1000 or not 1<=frames<=1000 or frames!=wanted
        or info.get('frame_start')!=spec.frame_start or spec.frame_start+frames>count
        or info.get('original_save_called') is not False or np.float32(info.get('speed', -1))!=np.float32(spec.speed)
        or any(info.get(k)!=v or inspected[0].get(k)!=v for k,v in
            dict(frames=frames, fps=spec.fps, width=width, height=height).items())):
        raise BridgeError('TIN_FLIGHT_PROFILE_READBACK')
    original=[v for v in events if v.get('event')=='tin_flight_frame']
    decoded=[v for v in readback if v.get('event')=='flight_frame_readback']
    if len(original)!=frames or len(decoded)!=frames: raise BridgeError('TIN_FLIGHT_FRAME_COUNT')
    for index,(frame,actual) in enumerate(zip(original,decoded)):
        route_index=spec.frame_start+index
        if frame.get('index')!=index or frame.get('route_index')!=route_index or frame.get('next_native_index')!=route_index+1:
            raise BridgeError('TIN_FLIGHT_ADVANCE_READBACK')
        for key in ['eye_model', 'target_model']:
            vector=frame.get(key, [])
            if len(vector)!=3 or not np.isfinite(vector).all(): raise BridgeError('TIN_FLIGHT_CAMERA_READBACK')
        if (actual.get('index')!=index or not re.fullmatch('[0-9a-f]{64}', frame.get('rgb_sha256', ''))
            or actual.get('rgb_sha256')!=frame['rgb_sha256']): raise BridgeError('TIN_FLIGHT_PIXEL_READBACK')
    return {k:v for k,v in info.items() if k!='event'} | dict(file='flight.avi', input=spec.model_dump(),
        frame_details=[{k:v for k,v in frame.items() if k!='event'} for frame in original],
        pixel_check='EXACT every original TIN RGB frame vs fresh Windows AVI stream readback')
