"""Individual graphic values use physical identity and the existing patch ledger."""
from pathlib import Path
import pytest
from mapgis_agent.core import Bridge,BridgeError,read_native,sha
from mapgis_agent.native_backend import prepare_native_operation
from mapgis_agent.comparison import _extract_style
from mapgis_agent.creation import prepare_creation
from test_attribute_schema import sample,LOCAL
from test_native_wp import enable_native


@pytest.mark.parametrize('suffix,field',[('wt','color'),('wl','color'),('wp','fill_color')])
def test_per_record_graphic_values_preserve_data_unselected_style_native_save_and_undo(tmp_path,suffix,field):
    from shapely.geometry import Point,LineString,box,mapping
    bridge=Bridge(tmp_path);enable_native(bridge)
    geometries=[Point(i*20,0) if suffix=='wt' else LineString([(i*20,0),(i*20+5,5)]) if suffix=='wl' else box(i*20,0,i*20+10,10) for i in range(3)]
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':mapping(g),
        'properties':{'ID':7,'old':100+i,'code':'砂岩'},**({'text':'保留注记'} if suffix=='wt' and i==1 else {})} for i,g in enumerate(geometries,1)]},
        {'name':'source.'+suffix,'map_scale':1000,'crs':LOCAL},'source')
    source=Path(bridge.apply('source')['output']);before=source.read_bytes()
    prepared=bridge.prepare(str(source),sha(before),[1,3],'varied',style={'layer':2},
        style_updates={'1':{field:3},'3':{field:5}},attribute_updates={'1':{'old':101},'3':{'old':303}})
    first=Path(bridge.apply('varied')['output']);old,new=read_native(source)[1],read_native(first)[1]
    assert [_extract_style(new,i)[field] for i in range(3)]==[3,_extract_style(old,1)[field],5]
    assert new.data['ID'].tolist()==[7,7,7] and new.data['old'].tolist()==[101,102,303]
    assert _extract_style(new,1)==_extract_style(old,1)
    assert all(a.wkb==b.wkb for a,b in zip(old.geom,new.geom))
    assert prepared['geometry_unchanged'] and prepared['untargeted_bytes_unchanged'] and source.read_bytes()==before
    prepare_native_operation(bridge,'resave',str(first),sha(first.read_bytes()),{},'native')
    saved=Path(bridge.apply('native')['output']);after=read_native(saved)[1]
    assert after.data.equals(new.data) and [_extract_style(after,i) for i in range(3)]==[_extract_style(new,i) for i in range(3)]
    bridge.prepare(str(first),sha(first.read_bytes()),[2],'later',style_updates={'2':{field:7}})
    later=Path(bridge.apply('later')['output']);bridge.recover('varied',str(later),sha(later.read_bytes()),'undo')
    restored=read_native(Path(bridge.apply('undo')['output']))[1]
    assert restored.data.equals(old.data) and _extract_style(restored,1)[field]==7
    assert _extract_style(restored,0)==_extract_style(old,0) and _extract_style(restored,2)==_extract_style(old,2)


@pytest.mark.parametrize('updates,uniform',[
    ({'2':{'color':3}},None),({'01':{'color':3}},None),({1:{'color':3}},None),
    ({'1':{}},None),({'1':{'color':3}},{'color':2}),({'1':{'color':-1}},None),
])
def test_per_record_style_rejects_identity_and_override_ambiguity(tmp_path,updates,uniform):
    bridge=Bridge(tmp_path);source=sample(bridge);before=source.read_bytes()
    with pytest.raises(BridgeError):bridge.prepare(str(source),sha(before),[1],'bad',style=uniform,style_updates=updates)
    assert source.read_bytes()==before


def test_style_updates_are_part_of_idempotent_request_identity(tmp_path):
    bridge=Bridge(tmp_path);source=sample(bridge);args=(str(source),sha(source.read_bytes()),[1],'same')
    bridge.prepare(*args,style_updates={'1':{'color':2}})
    assert bridge.prepare(*args,style_updates={'1':{'color':2}})['state']=='PREPARED'
    with pytest.raises(BridgeError,match='REQUEST_ID_CONFLICT'):bridge.prepare(*args,style_updates={'1':{'color':3}})


def test_individual_circle_arc_graphics_use_the_merged_primitive_profile(tmp_path):
    from test_native_point_radius import sample as primitive_sample
    bridge,source=primitive_sample(tmp_path)
    bridge.prepare(str(source),sha(source.read_bytes()),[1,2],'graphics',style_updates={
        '1':{'filled':1,'pen_width':.2},'2':{'start_angle':60.,'end_angle':120.}})
    output=Path(bridge.apply('graphics')['output']);reader=read_native(output)[1]
    assert _extract_style(reader,0)['filled']==1 and _extract_style(reader,1)['start_angle']==60.
    with pytest.raises(BridgeError,match='POINT_PRIMITIVE'):bridge.prepare(str(source),sha(source.read_bytes()),[1],'radius',style_updates={'1':{'radius_mm':5.}})
