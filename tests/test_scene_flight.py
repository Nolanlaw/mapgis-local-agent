import pytest
from pydantic import ValidationError
from mapgis_agent.core import BridgeError
from mapgis_agent.scene_flight import FlightParameters,flight_settings,validate_flight
GRID={'bounds':[500005.,3000005.,500645.,3000485.]}
POINTS=[[500100.,3000100.],[500500.,3000200.],[500400.,3000400.]]

def test_flight_serializes_explicit_original_speed_and_frame_window():
    spec=FlightParameters(points=POINTS,speed=20.,frame_start=2,frame_count=12,fps=24)
    tokens=flight_settings(spec,GRID,321,241).split()
    assert tokens[:5]==['3','2','12','24','20.0']
    assert list(map(float,tokens[5:]))==[v for point in POINTS for v in point]
    assert flight_settings(FlightParameters(points=POINTS,speed=20.),GRID,321,241).split()[2]=='0'

@pytest.mark.parametrize('changes',[
    {'points':[[0.,0.]]},{'points':[[0.,0.,0.],[1.,1.,1.]]},{'speed':float('nan')},
    {'speed':0.},{'frame_start':-1},{'frame_count':True},{'frame_count':1001},{'fps':0},{'fps':61},
])
def test_invalid_flight_options_are_refused(changes):
    with pytest.raises(ValidationError):FlightParameters.model_validate({'points':POINTS,'speed':20.}|changes)

def test_flight_refuses_clamping_duplicate_knots_and_unapproved_float32_rounding():
    for points,message in [([[500000.,3000100.],[500300.,3000200.]],'OUTSIDE'),
        ([[500100.,3000100.],[500100.,3000100.]],'DUPLICATE'),
        ([[500100.01,3000100.],[500300.,3000200.]],'FLOAT32_ROUNDING')]:
        with pytest.raises(BridgeError,match=message):flight_settings(FlightParameters(points=points,speed=20.),GRID,320,240)
    spec=FlightParameters(points=[[500100.01,3000100.],[500300.,3000200.]],speed=20.,max_xy_rounding_error_m=.02)
    assert flight_settings(spec,GRID,320,240).split()[5]=='500100.0'

def test_flight_refuses_excessive_uncompressed_output_before_native_launch():
    with pytest.raises(BridgeError,match='OUTPUT_SIZE'):flight_settings(FlightParameters(points=POINTS,speed=20.,frame_count=1000),GRID,2048,2048)
    with pytest.raises(BridgeError,match='STEP_BOUND'):flight_settings(FlightParameters(points=POINTS,speed=20.,frame_start=999,frame_count=2),GRID,320,240)

def test_fresh_avi_readback_checks_each_frame_not_only_file_or_count():
    spec=FlightParameters(points=POINTS,speed=20.,frame_start=2,frame_count=2,fps=24)
    route={'event':'flight_written','generated_frames':100,'frame_start':2,'frames':2,'fps':24,'width':321,'height':241,'speed':20.,'speed_range':[10.,1282.]}
    frames=[{'event':'flight_frame','index':i,'route_index':2+i,'next_route_index':3+i,'rgb_sha256':str(i)*64,'eye':[500100.+i,3000100.,200.]} for i in range(2)]
    decoded=[{'event':'flight_frame_readback','index':i,'rgb_sha256':str(i)*64} for i in range(2)]
    info={'event':'flight_readback','frames':2,'fps':24,'width':321,'height':241}
    result=validate_flight([route,*frames],[info,*decoded],spec,321,241)
    assert result['frames']==2 and result['pixel_check'].startswith('EXACT')
    broken=[decoded[0],decoded[1]|{'rgb_sha256':'f'*64}]
    with pytest.raises(BridgeError,match='PIXEL_READBACK'):validate_flight([route,*frames],[info,*broken],spec,321,241)
    with pytest.raises(BridgeError,match='ADVANCE'):validate_flight([route,frames[0],frames[1]|{'next_route_index':5}],[info,*decoded],spec,321,241)
    fractional=spec.model_copy(update={'speed':20.1})
    assert validate_flight([route|{'speed':20.1000004},*frames],[info,*decoded],fractional,321,241)['speed']==20.1000004
