import copy
import json
import pytest
from pydantic import ValidationError
from mapgis_agent.core import BridgeError
from mapgis_agent.tin_scene import NativeTinCamera, FORMAT, _camera_bytes, _scene_source
from mapgis_agent.raster import file_sha
from mapgis_agent.tin_flight import TinFlightParameters, flight_settings, validate_flight


def camera():
    return NativeTinCamera(origin=[500000.125, 3000000.375], scales=[1., 1., 2.],
        eye_model=[50., 50., 200.], target_model=[100., 50., 0.],
        up=[0., 0., 1.], fov_degrees=90.)


def flight(**changes):
    return TinFlightParameters.model_validate(dict(points=[
        [500010.125, 3000010.375, 100.], [500090.125, 3000010.375, 100.]],
        speed=50., frame_count=2) | changes)


def test_world_route_uses_double_origin_and_native_vertical_scale():
    settings=flight_settings(flight(), camera(), 321, 241).split()
    assert settings[:5]==['2', '0', '2', '24', '50.0']
    assert list(map(float, settings[5:]))==[10., 10., 200., 90., 10., 200.]


def test_route_precision_is_explicit_and_duplicate_native_knots_are_rejected():
    points=[[500010.2484567, 3000010.375, 100.], [500090.125, 3000010.375, 100.]]
    with pytest.raises(BridgeError, match='ROUNDING'):
        flight_settings(flight(points=points), camera(), 321, 241)
    assert flight_settings(flight(points=points, max_coordinate_error_m=0.00001), camera(), 321, 241)
    with pytest.raises(BridgeError, match='DUPLICATE'):
        flight_settings(flight(points=[[500010.125, 3000010.375, 100.]]*2), camera(), 321, 241)


@pytest.mark.parametrize('changes', [dict(fps=True), dict(speed=0.), dict(frame_count=0),
    dict(points=[[1., 2., 3.]]), dict(points=[[1., 2.], [3., 4.]]), dict(speed=float('inf'))])
def test_tin_flight_rejects_invalid_public_parameters(changes):
    with pytest.raises(ValidationError): flight(**changes)


def test_recording_window_and_uncompressed_size_are_bounded():
    with pytest.raises(BridgeError, match='STEP_BOUND'):
        flight_settings(flight(frame_start=999, frame_count=2), camera(), 321, 241)
    with pytest.raises(BridgeError, match='OUTPUT_SIZE'):
        flight_settings(flight(frame_count=1000), camera(), 2048, 2048)


def receipts():
    original=[dict(event='tin_flight_written', generated_frames=3, capacity=4,
        frame_start=0, frames=2, fps=24, width=321, height=241, speed=50.,
        original_save_called=False)]
    original += [dict(event='tin_flight_frame', index=i, route_index=i,
        next_native_index=i+1, eye_model=[float(i), 0., 100.],
        target_model=[5., 0., 100.], rgb_sha256=str(i+1)*64) for i in range(2)]
    decoded=[dict(event='flight_readback', frames=2, fps=24, width=321, height=241)]
    decoded += [dict(event='flight_frame_readback', index=i, rgb_sha256=str(i+1)*64) for i in range(2)]
    return original, decoded


def test_video_readback_requires_every_original_frame_and_one_native_advance():
    original, decoded=receipts()
    assert validate_flight(original, decoded, flight(), 321, 241)['frames']==2
    bad=copy.deepcopy(decoded); bad[2]['rgb_sha256']='f'*64
    with pytest.raises(BridgeError, match='PIXEL_READBACK'):
        validate_flight(original, bad, flight(), 321, 241)
    bad=copy.deepcopy(original); bad[1]['next_native_index']=2
    with pytest.raises(BridgeError, match='ADVANCE_READBACK'):
        validate_flight(bad, decoded, flight(), 321, 241)
    with pytest.raises(BridgeError, match='FRAME_COUNT'):
        validate_flight(original, decoded[:-1], flight(), 321, 241)


def test_saved_flight_binds_world_path_to_initial_camera_and_control_file(tmp_path):
    files={'model.tin':b'WTIN fixture', 'camera.bin':_camera_bytes(camera()),
        'initial-camera.bin':_camera_bytes(camera()), 'flight.avi':b'AVI fixture',
        'flight.pat':b'PAT fixture',
        'flight.txt':b'2 0 2 24 50.0 10.0 10.0 200.0 90.0 10.0 200.0\n'}
    for name,raw in files.items(): (tmp_path/name).write_bytes(raw)
    record=dict(format=FORMAT, native_tin='model.tin', camera=camera().model_dump(),
        frame={'definition':'EPSG:4549','axis_order':'xy'}, width=321, height=241,
        members={name:file_sha(tmp_path/name) for name in files},
        flight=dict(input=flight().model_dump(), initial_camera=camera().model_dump()))
    scene=tmp_path/'flight.scene.json';scene.write_text(json.dumps(record))
    assert _scene_source(scene,file_sha(scene))[0]['flight']['input']['points'][0][0]==500010.125
    record['flight']['input']['points'][0][0]+=1;scene.write_text(json.dumps(record))
    with pytest.raises(BridgeError, match='FLIGHT_BINDING'):
        _scene_source(scene,file_sha(scene))
