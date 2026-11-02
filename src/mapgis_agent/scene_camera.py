"""A saved camera in the pinned original model basis, without world-float loss."""
from __future__ import annotations
from typing import Literal
import numpy as np
from pydantic import BaseModel,ConfigDict,Field,model_validator,ValidationError
from .core import BridgeError

class NativeCameraPose(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    profile:Literal['newtin67.camera.v1']='newtin67.camera.v1'
    basis:list[float]=Field(min_length=5,max_length=5)
    eye_model:list[float]=Field(min_length=3,max_length=3)
    move_degrees:float=Field(ge=0,le=360)
    view_degrees:float=Field(ge=0,le=360)
    fov_degrees:float=Field(ge=30,le=120)
    tilt_degrees:float=Field(ge=0,le=90)

    @model_validator(mode='after')
    def native_floats(self):
        values=np.asarray(self.basis+self.eye_model,dtype='float64').astype('float32')
        if not np.isfinite(values).all() or np.any(values[2:5]<=0):raise ValueError('finite native floats and positive model scales required')
        return self

def camera_settings(pose):
    return ' '.join(map(str,[1,*pose.basis,*pose.eye_model,pose.move_degrees,pose.view_degrees,pose.fov_degrees,pose.tilt_degrees]))+'\n'

def camera_readback(events,name):
    records=[v for v in events if v.get('event')==name]
    if len(records)!=1:raise BridgeError('SCENE_CAMERA_READBACK')
    try:return NativeCameraPose.model_validate(records[0]['pose']).model_dump()
    except (KeyError,ValidationError) as error:raise BridgeError('SCENE_CAMERA_READBACK') from error
