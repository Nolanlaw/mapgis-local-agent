from pathlib import Path
import math
import pytest
from shapely.geometry import Point,box,mapping

from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.spatial import spatial_query

LOCAL={'kind':'local','name':'spatial grid','unit':'metre','axis_order':'xy','reference_sha256':'0'*64}


def sources(tmp_path):
    bridge=Bridge(tmp_path);result=[]
    for key,geometries,suffix in [('points',[Point(1,1),Point(2,1),Point(5,5)],'wt'),
                                  ('regions',[box(0,0,2,2),box(2,0,4,2)],'wp')]:
        prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),
            'geometry':mapping(g),'properties':{'ID':7,'label':key+str(i)}} for i,g in enumerate(geometries,1)]},
            {'name':key+'.'+suffix,'crs':LOCAL,'map_scale':1000},key)
        output=bridge.apply(key);result.append({'file':output['output'],'expected_sha256':output['candidate_sha256']})
    return result


def pairs(result):return [(p['left_record_id'],p['right_record_id']) for p in result['pairs']]


def test_spatial_query_distinguishes_boundary_interior_and_duplicate_attribute_ids(tmp_path):
    left,right=sources(tmp_path)
    intersect=spatial_query(left,right,{'predicate':'intersects','crs':LOCAL,'include_attributes':True})
    assert pairs(intersect)==[(1,1),(2,1),(2,2)]
    assert all(p['left_attributes']['ID']==p['right_attributes']['ID']==7 for p in intersect['pairs'])
    assert pairs(spatial_query(left,right,{'predicate':'within','crs':LOCAL}))==[(1,1)]
    assert pairs(spatial_query(right,left,{'predicate':'covers','crs':LOCAL}))==[(1,1),(1,2),(2,2)]
    assert pairs(spatial_query(left,right,{'predicate':'touches','crs':LOCAL}))==[(2,1),(2,2)]
    assert pairs(spatial_query(left,right,{'predicate':'disjoint','crs':LOCAL}))==[(1,2),(3,1),(3,2)]
    assert intersect['source_preserved']


def test_spatial_query_nearest_retains_all_ties_with_explicit_planar_distance(tmp_path):
    left,right=sources(tmp_path)
    result=spatial_query(left,right,{'predicate':'nearest','crs':LOCAL})
    assert pairs(result)==[(1,1),(2,1),(2,2),(3,2)]
    assert result['pairs'][-1]['distance']==pytest.approx(math.sqrt(10))
    assert pairs(spatial_query(left,right,{'predicate':'nearest','crs':LOCAL,'max_distance':3}))==[(1,1),(2,1),(2,2)]
    assert pairs(spatial_query(left,right,{'predicate':'dwithin','crs':LOCAL,'distance':0}))==[(1,1),(2,1),(2,2)]
    with pytest.raises(BridgeError,match='SPATIAL_DISTANCE_UNITS'):
        spatial_query(left,right,{'predicate':'nearest','crs':LOCAL|{'unit':'unknown'}})


def test_spatial_query_is_paged_in_physical_id_order_and_never_labels_unknown_totals_as_zero(tmp_path):
    left,right=sources(tmp_path)
    first=spatial_query(left,right,{'predicate':'intersects','crs':LOCAL},limit=1)
    second=spatial_query(left,right,{'predicate':'intersects','crs':LOCAL},offset=first['next_offset'],limit=1)
    third=spatial_query(left,right,{'predicate':'intersects','crs':LOCAL},offset=second['next_offset'],limit=1)
    assert pairs(first)+pairs(second)+pairs(third)==[(1,1),(2,1),(2,2)]
    assert first['total_pairs'] is None and first['truncated']
    assert third['next_offset'] is None and third['total_pairs']==3
    with pytest.raises(BridgeError,match='STALE_SOURCE'):
        spatial_query(left|{'expected_sha256':'0'*64},right,{'predicate':'intersects','crs':LOCAL})
