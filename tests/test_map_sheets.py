"""GB/T13989-2012 examples, southwest ownership and explicit geographic frame."""
import math
import pytest
from shapely.geometry import box,Polygon,mapping
from mapgis_agent.core import BridgeError

FRAME={'definition':'EPSG:4326','axis_order':'xy'}
EXPECTED={'1m':'J50','500k':'J50B001001','250k':'J50C001001','100k':'J50D002002','50k':'J50E004003','25k':'J50F008005',
    '10k':'J50G015010','5k':'J50H030019','2k':'J50I090055','1k':'J50J01800109','500':'J50K03600217'}

@pytest.mark.parametrize('scale,expected',EXPECTED.items())
def test_all_eleven_scales_match_the_standards_published_dms_example(scale,expected):
    from mapgis_agent.map_sheets import map_sheets
    result=map_sheets('index',{'points':[[114+33/60+45/3600,39+22/60+30/3600]],'scale':scale},FRAME)
    row=result['sheets'][0];assert row['code']==expected and result['standard']=='GB/T13989-2012'
    assert row['geographic_bounds'][0]<=114.5625<row['geographic_bounds'][2]
    assert row['geographic_bounds'][1]<=39.375<row['geographic_bounds'][3]
    decoded=map_sheets('decode',{'codes':[expected]},FRAME)
    assert decoded['sheets'][0]==row

@pytest.mark.parametrize('scale',list(EXPECTED))
def test_southwest_corner_belongs_to_its_sheet_and_next_float_keeps_the_correct_neighbor(scale):
    from mapgis_agent.map_sheets import map_sheets
    first=map_sheets('index',{'points':[[119.23456,26.089]],'scale':scale},FRAME)['sheets'][0]
    west,south,east,north=first['geographic_bounds'];sw=map_sheets('index',{'points':[[west,south]],'scale':scale},FRAME)['sheets'][0]
    assert sw['code']==first['code']
    other=map_sheets('index',{'points':[[math.nextafter(west,-math.inf),south],[west,math.nextafter(south,-math.inf)],
        [math.nextafter(east,-math.inf),math.nextafter(north,-math.inf)],[east,north]],'scale':scale},FRAME)['sheets']
    assert other[0]['code']!=first['code'] and other[1]['code']!=first['code']
    assert other[2]['code']==first['code'] and other[3]['code']!=first['code']


@pytest.mark.parametrize('code',['Z50','J00','J61','O50','J42','J54','J50G000010','J50G097001','J50I577001','J50J11530001','J50K00002305','J50G01A010','J50G001001 trailing'])
def test_invalid_codes_are_rejected_instead_of_silently_clamped(code):
    from mapgis_agent.map_sheets import map_sheets
    with pytest.raises(BridgeError,match='SHEET_CODE'):map_sheets('decode',{'codes':[code]},FRAME)


def test_cover_uses_polygon_area_by_default_and_explicit_boundary_touch_when_requested():
    from mapgis_agent.map_sheets import map_sheets
    row=map_sheets('decode',{'codes':['G50G001001']},FRAME)['sheets'][0];bounds=row['geographic_bounds']
    result=map_sheets('cover',{'geometry':mapping(box(*bounds)),'scale':'10k'},FRAME)
    assert [v['code'] for v in result['sheets']]==['G50G001001']
    touched=map_sheets('cover',{'geometry':mapping(box(*bounds)),'scale':'10k','include_boundary_touch':True},FRAME)
    assert len(touched['sheets'])==9 and len({v['code'] for v in touched['sheets']})==9


def test_cover_keeps_holes_and_limits_candidate_work_before_enumerating_tiny_sheets():
    from mapgis_agent.map_sheets import map_sheets
    cell=map_sheets('decode',{'codes':['G50G040040']},FRAME)['sheets'][0]['geographic_bounds'];w,s,e,n=cell;dx=e-w;dy=n-s
    # Use canonical neighboring sheet edges, not repeated floating subtraction
    # that would add a real one-ulp strip across an extra sheet boundary.
    neighbors=map_sheets('decode',{'codes':['G50G039039','G50G041041']},FRAME)['sheets']
    nw,se=[v['geographic_bounds'] for v in neighbors]
    outer=box(nw[0],se[1],se[2],nw[3]);hole=box(w+dx*.001,s+dy*.001,e-dx*.001,n-dy*.001)
    geometry=Polygon(outer.exterior.coords,[hole.exterior.coords])
    result=map_sheets('cover',{'geometry':mapping(geometry),'scale':'10k'},FRAME)
    assert len(result['sheets'])==9 # The thin valid rim still occupies the center.
    exact_hole=Polygon(outer.exterior.coords,[box(w,s,e,n).exterior.coords])
    result=map_sheets('cover',{'geometry':mapping(exact_hole),'scale':'10k'},FRAME)
    assert len(result['sheets'])==8 and 'G50G040040' not in {v['code'] for v in result['sheets']}
    with pytest.raises(BridgeError,match='SHEET_CAPACITY'):
        map_sheets('cover',{'geometry':mapping(box(114,24,120,28)),'scale':'500'},FRAME)


@pytest.mark.parametrize('point',[[71.9,26],[138,26],[119,-1],[119,56],[119,float('nan')],[True,26],[119,26,3]])
def test_outside_declared_domestic_unmerged_domain_and_invalid_coordinates_are_explicit(point):
    from mapgis_agent.map_sheets import map_sheets
    with pytest.raises(BridgeError,match='SHEET_COORDINATES'):map_sheets('index',{'points':[point],'scale':'10k'},FRAME)


def test_frame_is_mandatory_geographic_degrees_and_sheet_code_never_selects_a_datum():
    from mapgis_agent.map_sheets import map_sheets
    with pytest.raises(BridgeError,match='SHEET_GEOGRAPHIC_CRS'):
        map_sheets('decode',{'codes':['G50']},{'definition':'EPSG:32650','axis_order':'xy'})
    a=map_sheets('index',{'points':[[119.2,26.1]],'scale':'10k'},FRAME)
    b=map_sheets('index',{'points':[[119.2,26.1]],'scale':'10k'},{'definition':'EPSG:4490','axis_order':'xy'})
    assert a['sheets']==b['sheets'] and a['geographic_crs']!=b['geographic_crs']
    assert a['coordinate_conversion_performed'] is False
