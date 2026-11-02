"""Survey grid equations and byte-preserving sediment point identifiers."""
import math
from pathlib import Path

import pytest
from pymapgis import Reader
from pymapgis.reader import _ATTR_HEADER_INDEX
from shapely.affinity import rotate
from shapely.geometry import Polygon,mapping

from mapgis_agent.core import Bridge,BridgeError,read_native,native_ids,annotation_text
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha


FRAME={'kind':'local','name':'frozen survey plane','axis_order':'xy','unit':'metre','reference_sha256':'a'*64}


def source(folder,features,name):
    bridge=Bridge(folder/'source');prepare_creation(bridge,{'type':'FeatureCollection','features':features},
        {'name':name,'crs':FRAME,'map_scale':1000},'source')
    return Path(bridge.apply('source')['output'])


def boundary(folder,bearing=0.):
    polygon=Polygon([(-100,-100),(100,-100),(100,100),(-100,100)],
        [[(-20,-20),(20,-20),(20,20),(-20,20)]])
    polygon=rotate(polygon,-bearing,origin=(0,0))
    return source(folder,[{'type':'Feature','id':'range','geometry':mapping(polygon),'properties':{'Name':'测量范围'}}],'range.wp')


def soil_options(**extra):
    return {'origin':[0.,0.],'grid_north_azimuth_deg':0.,'point_spacing_m':50.,'line_spacing_m':50.,
        'base_line_number':0,'base_point_number':100,'line_rule':'left_odd_right_even','prefix':'S',
        'line_digits':3,'point_digits':3,'boundary_tolerance_m':1e-7,**extra}


def soil(folder,options=None,bearing=0.):
    from mapgis_agent.sample_numbering import prepare_soil_grid
    file=boundary(folder,bearing);digest=file_sha(file);bridge=Bridge(folder/'run')
    receipt=prepare_soil_grid(bridge,file,digest,options or soil_options(),
        {'name':'soil.wt','crs':FRAME,'map_scale':1000},'soil')
    out=bridge.apply('soil')['output'];assert file_sha(file)==digest
    return receipt,Reader(out,make_valid=False),Path(out)


def test_soil_lattice_hole_parity_labels_and_clipped_lines(tmp_path):
    receipt,reader,out=soil(tmp_path)
    assert len(reader.geom)==24 and (0.,0.) not in {(g.x,g.y) for g in reader.geom}
    expected={-100:3,-50:1,0:0,50:2,100:4}
    for i,g in enumerate(reader.geom):
        assert reader.data['LINE_NO'].iloc[i]==expected[round(g.x)]
        assert reader.data['POINT_NO'].iloc[i]==100+round(g.y/50)
    labels=Reader(out.with_name('soil-labels.wt'));assert len(labels.geom)==24
    lines=Reader(out.with_name('soil-lines.wl'));assert len(lines.geom)==6
    assert len(set(reader.data['SAMPLE_ID']))==24


def test_soil_bearing_is_clockwise_from_grid_north_not_math_x_axis(tmp_path):
    _,reader,_=soil(tmp_path,soil_options(grid_north_azimuth_deg=30.),bearing=30.)
    assert len(reader.geom)==24
    a=math.radians(30)
    for point in reader.geom:
        u=point.x*math.cos(a)-point.y*math.sin(a);v=point.x*math.sin(a)+point.y*math.cos(a)
        assert u/50==pytest.approx(round(u/50),abs=1e-12)
        assert v/50==pytest.approx(round(v/50),abs=1e-12)


def test_sequential_line_numbers_and_negative_permission_are_explicit(tmp_path):
    with pytest.raises(BridgeError,match='SOIL_NEGATIVE_NUMBER'):
        soil(tmp_path,soil_options(line_rule='sequential'))


def test_signed_sequential_numbers_are_usable_when_explicitly_selected(tmp_path):
    _,reader,_=soil(tmp_path,soil_options(line_rule='sequential',base_point_number=0,allow_negative_numbers=True))
    assert min(reader.data['LINE_NO'])==-2 and min(reader.data['POINT_NO'])==-2
    assert len(set(reader.data['SAMPLE_ID']))==24


def test_soil_budget_is_checked_before_publishing_partial_grid(tmp_path):
    with pytest.raises(BridgeError,match='SOIL_GRID_BUDGET'):
        soil(tmp_path,soil_options(max_candidate_points=10))


def sediment_options(**extra):
    return {'origin':[500.,1500.],'origin_reference':'upper_left_cell_centre','large_columns':2,'large_rows':2,
        'large_width_m':1000.,'large_height_m':1000.,'first_large_number':101,'last_large_number':104,
        'boundary_rule':'reject','boundary_tolerance_m':0.,'prefix':'MP','large_digits':3,**extra}


def sediment(folder,points=None,options=None,old=None):
    from mapgis_agent.sample_numbering import prepare_sediment_numbering
    points=points or [(100,1900),(200,1800),(600,1900),(100,1400),(600,1400),(1100,1900),(100,900),(1100,900)]
    features=[{'type':'Feature','id':str(i+1),'geometry':{'type':'Point','coordinates':point},
        'properties':{'ORIGINAL':f'P{i+1}','OLD':old[i] if old else ''},
        'style':{'point_type':2,'radius_mm':1.,'filled':1,'pen_width':.2,'color':3,'border_color':3}} for i,point in enumerate(points)]
    file=source(folder,features,'samples.wt');digest=file_sha(file);bridge=Bridge(folder/'run')
    receipt=prepare_sediment_numbering(bridge,file,digest,options or sediment_options(),
        {'name':'numbered.wt','crs':FRAME,'map_scale':1000},'number')
    out=bridge.apply('number')['output'];assert file_sha(file)==digest
    return receipt,Reader(out,make_valid=False),Path(out),file


def test_sediment_abcd_row_major_sequences_and_every_nonattribute_byte(tmp_path):
    _,reader,out,file=sediment(tmp_path)
    assert list(reader.data['SAMPLE_ID'])==['MP101A1','MP101A2','MP101B1','MP101C1','MP101D1','MP102A1','MP103A1','MP104A1']
    before,old,sections=read_native(file);after,new,new_sections=read_native(out)
    assert native_ids(old)==native_ids(new)
    for i,((a,n),(b,m)) in enumerate(zip(sections,new_sections)):
        if i!=_ATTR_HEADER_INDEX['POINT']:assert before[a:a+n]==after[b:b+m]
    assert list(reader.data['ORIGINAL'])==[f'P{i}' for i in range(1,9)]
    labels=out.with_name('numbered-labels.wt');blob,r,parts=read_native(labels)
    assert [annotation_text(blob,parts,i) for i in native_ids(r)]==list(reader.data['SAMPLE_ID'])


def test_reserved_ids_and_existing_human_values_are_preserved(tmp_path):
    _,reader,_,_=sediment(tmp_path,options=sediment_options(existing_id_field='OLD',reserved_sample_ids=['MP101A2']),
        old=['MP101A1','','','','','','',''])
    assert list(reader.data['SAMPLE_ID'])[:2]==['MP101A1','MP101A3']
    assert reader.data['OLD'].iloc[0]=='MP101A1'


@pytest.mark.parametrize('rule,expected',[('north_west','MP101A1'),('south_east','MP101D1')])
def test_exact_internal_edges_have_a_declared_owner(tmp_path,rule,expected):
    _,reader,_,_=sediment(tmp_path,points=[(500,1500)],options=sediment_options(boundary_rule=rule))
    assert reader.data['SAMPLE_ID'].iloc[0]==expected


def test_boundary_ambiguity_is_rejected_without_an_owner_rule(tmp_path):
    with pytest.raises(BridgeError,match='SAMPLE_GRID_BOUNDARY'):
        sediment(tmp_path,points=[(500,1500)])


@pytest.mark.parametrize('point',[(2001,1000),(-1,1000),(500,2001),(500,-1)])
def test_outside_points_are_not_silently_dropped_or_clamped(tmp_path,point):
    with pytest.raises(BridgeError,match='SAMPLE_GRID_OUTSIDE'):
        sediment(tmp_path,points=[point])


def test_duplicate_locations_need_an_explicit_stable_tie_rule(tmp_path):
    with pytest.raises(BridgeError,match='SAMPLE_COINCIDENT'):
        sediment(tmp_path,points=[(100,1900),(100,1900)])


def test_coincident_samples_can_use_explicit_physical_record_order(tmp_path):
    _,reader,_,_=sediment(tmp_path,points=[(100,1900),(100,1900)],
        options=sediment_options(coincident_policy='physical_record_order'))
    assert list(reader.data['SAMPLE_ID'])==['MP101A1','MP101A2']


def test_preserved_external_ids_have_unknown_sequence_as_empty_not_zero(tmp_path):
    _,reader,_,_=sediment(tmp_path,points=[(100,1900)],old=['人工-甲'],
        options=sediment_options(existing_id_field='OLD'))
    assert reader.data['SAMPLE_ID'].iloc[0]=='人工-甲' and reader.data['CELL_SEQ'].iloc[0]==''


def test_native_source_context_cannot_be_treated_as_degrees_or_unknown_units(tmp_path):
    from mapgis_agent.sample_numbering import prepare_soil_grid
    file=boundary(tmp_path)
    with pytest.raises(BridgeError,match='NUMBERING_LINEAR_FRAME'):
        prepare_soil_grid(Bridge(tmp_path/'run'),file,file_sha(file),soil_options(),
            {'name':'soil.wt','crs':FRAME|{'unit':'unknown'},'map_scale':1000},'soil')
