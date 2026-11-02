from pathlib import Path
import pytest
from shapely.geometry import Polygon,box,mapping,shape
from shapely import unary_union

from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.creation import prepare_creation
from mapgis_agent.spatial import polygon_overlay
from test_spatial_query import LOCAL


def overlay_sources(tmp_path,touch=False):
    bridge=Bridge(tmp_path);result=[]
    left=Polygon([(0,0),(4,0),(4,4),(0,4)],holes=[[(1,1),(1,2),(2,2),(2,1)]])
    right=box(4 if touch else 2,0,8 if touch else 6,4)
    for key,geometry,rock in [('left',left,'砂岩'),('right',right,'花岗岩')]:
        prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1',
            'geometry':mapping(geometry),'properties':{'ID':7,'rock':rock},'style':{'fill_color':3 if key=='left' else 4}}]},
            {'name':key+'.wp','crs':LOCAL,'map_scale':1000},key)
        output=bridge.apply(key);result.append({'file':output['output'],'expected_sha256':output['candidate_sha256']})
    return bridge,*result,left,right


@pytest.mark.parametrize('operation,area',[('intersection',8),('union',23),('symmetric_difference',15),('difference',7),('identity',15)])
def test_polygon_overlay_keeps_both_source_identities_explicit_fields_and_area(operation,area,tmp_path):
    bridge,left,right,a,b=overlay_sources(tmp_path)
    fields=[{'side':'left','field':'rock','target':'左岩性','missing':''},
            {'side':'right','field':'rock','target':'右岩性','missing':''}]
    result=polygon_overlay(left,right,{'operation':operation,'crs':LOCAL,'fields':fields})
    assert result['qa']['output_union_area']==pytest.approx(area)
    assert result['source_preserved'] and not result['empty']
    features=result['collection']['features'];assert all(f['geometry']['type'] in ('Polygon','MultiPolygon') for f in features)
    assert any(f['properties']['src_left']==1 for f in features)
    for feature in features:
        props=feature['properties']
        assert props['左岩性']==('砂岩' if props['src_left'] else '')
        assert props['右岩性']==('花岗岩' if props['src_right'] else '')
    prepare_creation(bridge,result['collection'],{'name':'result.wp','crs':LOCAL,'map_scale':1000},'overlay')
    output=bridge.apply('overlay');geometries=read_native(Path(output['output']))[1].geom
    assert unary_union(geometries).area==pytest.approx(area)
    if operation=='union':
        from test_native_raster import native_settings
        from mapgis_agent.native_backend import prepare_native_operation
        native_settings(tmp_path)
        native=prepare_native_operation(bridge,'resave',output['output'],output['candidate_sha256'],{},'native-overlay')
        assert native['comparison']['records_equal']


def test_polygon_overlay_boundary_only_contact_is_a_real_empty_area_result(tmp_path):
    _,left,right,_,_=overlay_sources(tmp_path,touch=True)
    result=polygon_overlay(left,right,{'operation':'intersection','crs':LOCAL})
    assert result['empty'] and result['collection']['features']==[] and result['qa']['output_union_area']==0
    assert result['qa']['geometry_policy']=='polygon areas; line/point-only contacts excluded'


def test_polygon_overlay_requires_explicit_missing_value_for_unmatched_mapped_attributes(tmp_path):
    _,left,right,_,_=overlay_sources(tmp_path)
    with pytest.raises(BridgeError,match='OVERLAY_MISSING_VALUE'):
        polygon_overlay(left,right,{'operation':'union','crs':LOCAL,'fields':[{'side':'left','field':'rock','target':'rock'}]})
