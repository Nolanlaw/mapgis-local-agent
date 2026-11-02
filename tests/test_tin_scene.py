import json
import struct
import numpy as np
import pytest
from pydantic import ValidationError
from mapgis_agent.core import BridgeError
from mapgis_agent.raster import file_sha
from mapgis_agent.tin_scene import (
    FORMAT, NativeTinCamera, TinSceneParameters, _camera_bytes, _camera_read,
    _scene_source, _frame, _world_camera,
)

FRAME={'definition':'EPSG:4549','axis_order':'xy'}

def pose():
    return NativeTinCamera(origin=[500000.125,3000000.375],scales=[1.,1.,2.],
        eye_model=[124.83927917480469,120.56428527832031,607.0714111328125],
        target_model=[52.625,50.,0.],up=[0.,0.,1.],fov_degrees=90.)

def test_camera_binary_keeps_local_float32_and_double_world_origin(tmp_path):
    file=tmp_path/'camera.bin';file.write_bytes(_camera_bytes(pose()))
    actual=_camera_read(file)
    assert actual==pose() and len(file.read_bytes())==76
    eye,target=_world_camera(actual)
    assert eye==[500124.9642791748,3000120.9392852783,303.53570556640625]
    assert target==[500052.75,3000050.375,0.]
    # Original GetEyePos returns scaled607.071411; persisted worldZ must divide2.
    assert eye[2]!=actual.eye_model[2]
    assert struct.unpack_from('<2d',file.read_bytes(),8)==tuple(actual.origin)

@pytest.mark.parametrize('change',[
    {'scales':[0.,1.,1.]},{'scales':[1e-60,1.,1.]},
    {'eye_model':[52.625,50.,0.]},{'up':[0.,0.,0.]},
    {'up':[72.21427917480469,70.56428527832031,607.0714111328125]},
    {'eye_model':[1e100,1.,1.]},{'fov_degrees':29.},
])
def test_saved_camera_rejects_degenerate_or_nonfinite_native_pose(change):
    with pytest.raises(ValidationError):NativeTinCamera.model_validate(pose().model_dump()|change)

def test_world_camera_options_require_explicit_pair_and_nonparallel_up():
    for changes in [{'eye':[0.,0.,1.]},{'eye':[0.,0.,1.],'target':[0.,0.,0.]},
                    {'width':True},{'scales':[1.,0.,1.]},{'native_camera':pose(),'scales':[1.,1.,1.]}]:
        with pytest.raises(ValidationError):TinSceneParameters(**changes)
    assert TinSceneParameters(eye=[500001.,3000001.,10.],target=[500005.,3000005.,0.]).width==640

def test_tin_scene_requires_consistent_metre_frame_and_elevations():
    assert _frame(FRAME,'metre',None)==FRAME
    with pytest.raises(BridgeError,match='METRE'):_frame({'definition':'EPSG:4326','axis_order':'xy'},'metre',None)
    with pytest.raises(BridgeError,match='FRAME_CHANGED'):_frame(FRAME,'metre',{'crs':{'kind':'local','unit':'metre'},'vertical_unit':'metre'})
    with pytest.raises(BridgeError,match='VERTICAL'):_frame(FRAME,None,None)
    with pytest.raises(BridgeError,match='VERTICAL'):_frame(FRAME,'metre',{'crs':FRAME,'vertical_unit':'foot'})

def test_scene_rejects_camera_rebinding_or_unexpected_dependency_before_native(tmp_path):
    model=tmp_path/'model.tin';model.write_bytes(b'WTIN fixture')
    camera=tmp_path/'camera.bin';camera.write_bytes(_camera_bytes(pose()))
    record={'format':FORMAT,'native_tin':'model.tin','frame':FRAME,'camera':pose().model_dump(),
            'members':{'model.tin':file_sha(model),'camera.bin':file_sha(camera)}}
    scene=tmp_path/'map.scene.json';scene.write_text(json.dumps(record))
    actual,sources=_scene_source(scene,file_sha(scene));assert len(sources)==3
    record['camera']['origin'][0]+=1
    scene.write_text(json.dumps(record))
    with pytest.raises(BridgeError,match='CAMERA_BINDING'):_scene_source(scene,file_sha(scene))
    record['camera']=pose().model_dump();record['members']['../outside']=file_sha(model)
    scene.write_text(json.dumps(record))
    with pytest.raises(BridgeError,match='MEMBERS'):_scene_source(scene,file_sha(scene))

def test_camera_binary_rejects_trailing_bytes_and_unknown_profile(tmp_path):
    for raw in [_camera_bytes(pose())+b'x',b'MGTK0001'+_camera_bytes(pose())[8:]]:
        file=tmp_path/'camera.bin';file.write_bytes(raw)
        with pytest.raises(BridgeError,match='CAMERA_PROFILE'):_camera_read(file)

def test_saved_sidecar_cannot_rebind_another_crs_even_with_updated_member_hash(tmp_path):
    model=tmp_path/'model.tin';model.write_bytes(b'WTIN fixture')
    camera=tmp_path/'camera.bin';camera.write_bytes(_camera_bytes(pose()))
    sidecar=tmp_path/'model.tin.json';sidecar.write_text(json.dumps({'schema':'mapgis-agent/tin/1',
        'tin_sha256':file_sha(model),'crs':{'definition':'EPSG:3857','axis_order':'xy'},'vertical_unit':'metre',
        'source_file':'Z:/provenance-is-not-a-dependency/absent.wt'}))
    record={'format':FORMAT,'native_tin':'model.tin','frame':FRAME,'camera':pose().model_dump(),
        'members':{v.name:file_sha(v) for v in [model,camera,sidecar]}}
    scene=tmp_path/'map.scene.json';scene.write_text(json.dumps(record))
    with pytest.raises(BridgeError,match='FRAME_CHANGED'):_scene_source(scene,file_sha(scene))
    record['frame']={'definition':'EPSG:3857','axis_order':'xy'};scene.write_text(json.dumps(record))
    assert _scene_source(scene,file_sha(scene))[0]['frame']==record['frame']
