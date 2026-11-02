"""Minimum-curvature drillhole positions and explicitly signed apparent dip."""
import math
import numpy as np
import pytest
from mapgis_agent.core import BridgeError

FRAME={'crs':{'kind':'local','name':'borehole fixture','unit':'metre','axis_order':'xy','reference_sha256':'a'*64},
    'vertical_unit':'metre','vertical_datum':'fixture elevation','azimuth_reference':'grid_north','inclination_reference':'vertical_down'}
COLLAR={'easting':1000.,'northing':2000.,'elevation':300.}

@pytest.mark.parametrize('inclination,azimuth',[(0,0),(30,0),(30,90),(60,180),(90,270),(120,45)])
def test_constant_bearing_matches_an_independent_straight_line_including_climbing_holes(inclination,azimuth):
    from mapgis_agent.borehole import borehole_trajectory
    survey=[{'md':m,'inclination':inclination,'azimuth':azimuth} for m in [0,30,100]];query=[100,0,15,30,15]
    result=borehole_trajectory(survey,COLLAR,FRAME,query_mds=query)
    inc=math.radians(inclination);azi=math.radians(azimuth)
    for row,md in zip(result['query_samples'],query):
        expected=[1000+md*math.sin(inc)*math.sin(azi),2000+md*math.sin(inc)*math.cos(azi),300-md*math.cos(inc)]
        np.testing.assert_allclose([row['easting'],row['northing'],row['elevation']],expected,atol=1e-10,rtol=0)
        assert row['md']==md
    assert all(abs(v['dogleg_degrees_per_course'])<1e-10 for v in result['survey_samples'])
    assert result['coordinate_conversion_performed'] is False


def test_quarter_circle_query_uses_an_arc_not_linear_station_interpolation():
    from mapgis_agent.borehole import borehole_trajectory
    survey=[{'md':0,'inclination':0,'azimuth':90},{'md':100,'inclination':90,'azimuth':90}]
    query=[0,25,50,75,100];result=borehole_trajectory(survey,COLLAR,FRAME,query_mds=query)
    radius=100/(math.pi/2)
    for row,md in zip(result['query_samples'],query):
        angle=(math.pi/2)*(md/100)
        assert row['easting']==pytest.approx(1000+radius*(1-math.cos(angle)),abs=1e-9)
        assert row['northing']==pytest.approx(2000,abs=1e-9)
        assert row['elevation']==pytest.approx(300-radius*math.sin(angle),abs=1e-9)
    assert result['survey_samples'][-1]['dogleg_degrees_per_course']==pytest.approx(27.)
    assert result['resampled_survey_station_error_max']<1e-9


def test_azimuth_wrap_takes_the_short_great_circle_and_queries_keep_original_order():
    from mapgis_agent.borehole import borehole_trajectory
    survey=[{'md':0,'inclination':90,'azimuth':350},{'md':100,'inclination':90,'azimuth':10}]
    result=borehole_trajectory(survey,COLLAR,FRAME,query_mds=[100,50,0])
    end,mid,start=result['query_samples']
    assert end['easting']==pytest.approx(1000,abs=1e-9) and end['northing']>2090
    assert mid['easting']<1000 and mid['northing']==pytest.approx((2000+end['northing'])/2,abs=1e-9)
    assert start['elevation']==300


@pytest.mark.parametrize('survey',[
    [{'md':10,'inclination':0,'azimuth':0},{'md':20,'inclination':0,'azimuth':0}],
    [{'md':0,'inclination':0,'azimuth':0},{'md':0,'inclination':0,'azimuth':0}],
    [{'md':0,'inclination':0,'azimuth':0},{'md':20,'inclination':180,'azimuth':0}],
    [{'md':0,'inclination':0,'azimuth':0},{'md':20,'inclination':0,'azimuth':360}],
    [{'md':0,'inclination':90,'azimuth':0},{'md':20,'inclination':90,'azimuth':180}],
    [{'md':0,'inclination':0,'azimuth':0},{'md':float('inf'),'inclination':0,'azimuth':0}],
])
def test_invalid_or_antipodal_surveys_are_rejected_without_inventing_a_path(survey):
    from mapgis_agent.borehole import borehole_trajectory
    with pytest.raises(BridgeError,match='BOREHOLE_'):borehole_trajectory(survey,COLLAR,FRAME)


def test_unknown_units_magnetic_bearings_and_query_extrapolation_are_explicit():
    from mapgis_agent.borehole import borehole_trajectory
    survey=[{'md':0,'inclination':0,'azimuth':0},{'md':100,'inclination':0,'azimuth':0}]
    for bad in [{**FRAME,'vertical_unit':'foot'},{**FRAME,'azimuth_reference':'magnetic_north'},
            {**FRAME,'crs':{'definition':'EPSG:4326','axis_order':'xy'}}]:
        with pytest.raises(BridgeError,match='BOREHOLE_FRAME'):borehole_trajectory(survey,COLLAR,bad)
    with pytest.raises(BridgeError,match='BOREHOLE_QUERY'):borehole_trajectory(survey,COLLAR,FRAME,query_mds=[101])


@pytest.mark.parametrize('direction,dip,section,expected',[(90,30,90,30),(90,30,270,-30),(90,30,0,0),
    (90,30,45,math.degrees(math.atan(math.tan(math.pi/6)/math.sqrt(2)))),(90,0,13,0),(90,90,90,90),(90,90,270,-90)])
def test_signed_apparent_dip_matches_independent_projection(direction,dip,section,expected):
    from mapgis_agent.borehole import apparent_dip
    result=apparent_dip(direction,dip,section)
    assert result['apparent_dip_degrees']==pytest.approx(expected,abs=1e-10)
    assert result['positive_sign']=='down along increasing section distance'


def test_vertical_bed_parallel_section_has_no_unique_apparent_dip():
    from mapgis_agent.borehole import apparent_dip
    with pytest.raises(BridgeError,match='DIP_UNDEFINED'):apparent_dip(90,90,0)
