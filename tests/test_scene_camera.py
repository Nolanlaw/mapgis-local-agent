import pytest
from pydantic import ValidationError
from mapgis_agent.scene_camera import NativeCameraPose,camera_settings,camera_readback
from mapgis_agent.core import BridgeError
POSE={'profile':'newtin67.camera.v1','basis':[500005.,3000005.,2.00333977,2.67111969,1.],
      'eye_model':[190.317276,787.980286,206.479218],'move_degrees':0.,'view_degrees':244.624863,'fov_degrees':45.,'tilt_degrees':0.}

def test_exact_saved_pose_uses_native_frame_and_float32_roundtrip():
    pose=NativeCameraPose.model_validate(POSE);tokens=camera_settings(pose).split()
    assert len(tokens)==13 and tokens[0]=='1'
    assert [float(v) for v in tokens[1:6]]==POSE['basis']
    assert camera_readback([{'event':'flight_final_camera','pose':POSE,'world_eye':[500100.,3000300.,206.479218]}],'flight_final_camera')==POSE

@pytest.mark.parametrize('change',[
    {'profile':'unknown'}, {'basis':[0.,0.,1.,1.]},{'basis':[0.,0.,0.,1.,1.]},
    {'eye_model':[1.,2.,float('nan')]},{'view_degrees':361.},{'fov_degrees':29.},{'tilt_degrees':91.},
])
def test_saved_native_pose_rejects_unknown_abi_invalid_axes_and_angles(change):
    with pytest.raises(ValidationError):NativeCameraPose.model_validate(POSE|change)

def test_missing_or_duplicate_original_camera_event_is_not_inferred():
    with pytest.raises(BridgeError,match='CAMERA_READBACK'):camera_readback([],'flight_final_camera')
    event={'event':'flight_final_camera','pose':POSE}
    with pytest.raises(BridgeError,match='CAMERA_READBACK'):camera_readback([event,event],'flight_final_camera')
