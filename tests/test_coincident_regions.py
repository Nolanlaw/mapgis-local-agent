"""Distinct physical regions retain identical/same-side boundaries and values."""
from pathlib import Path

import pytest
from shapely.geometry import box,Polygon,mapping

from mapgis_agent.core import Bridge,read_native,native_ids
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import prepare_native_operation,native_file_info
from mapgis_agent.raster import file_sha
from mapgis_agent.topology import validate_wp_topology
from test_native_raster import native_settings


def geometries(case):
    square=box(0,0,10,10)
    if case=='three-identical':return [square,square,Polygon(list(square.exterior.coords)[::-1])]
    if case=='same-edge-overlap':return [square,box(0,0,5,10)]
    if case=='duplicates-adjacent':return [square,square,square,box(10,0,20,10)]
    donut=Polygon(square.exterior.coords,[box(2,2,4,4).exterior.coords])
    return [donut,donut,box(2,2,4,4)]


@pytest.mark.parametrize('case',['three-identical','same-edge-overlap','duplicates-adjacent','duplicate-holes-island'])
@pytest.mark.parametrize('projected',[False,True])
def test_coincident_region_identity_geometry_attributes_and_native_save(tmp_path,case,projected):
    bridge=Bridge(tmp_path);native_settings(tmp_path);source_geometries=geometries(case)
    frame=({'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'} if projected else
        {'kind':'local','name':'coincident region fixture','unit':'metre','axis_order':'xy','reference_sha256':'0'*64})
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':f'region-{i}','geometry':mapping(g),
        'properties':{'ID':7,'value':i*11,'类别':f'区域{i}'},'style':{'fill_color':i+2}} for i,g in enumerate(source_geometries,1)]}
    receipt=prepare_creation(bridge,features,{'name':'regions.wp','map_scale':50000 if projected else 1000,'crs':frame},'create')
    source=Path(bridge.apply('create')['output']);before=source.read_bytes()
    _,parsed,_=read_native(source)
    assert len(parsed.geom)==len(source_geometries) and parsed.data['value'].tolist()==[i*11 for i in range(1,len(source_geometries)+1)]
    assert native_ids(parsed)==list(range(1,len(source_geometries)+1)) and len(receipt['feature_mapping'])==len(source_geometries)
    for actual,expected in zip(parsed.geom,source_geometries):assert actual.equals(expected)
    validate_wp_topology(source)
    prepare_native_operation(bridge,'resave',str(source),file_sha(source),{},'native');saved=Path(bridge.apply('native')['output'])
    info=native_file_info(tmp_path,str(saved),file_sha(saved));assert info['active']==len(source_geometries)
    _,after,_=read_native(saved)
    assert after.data.equals(parsed.data)
    for actual,expected in zip(after.geom,source_geometries):assert actual.equals(expected)
    assert after.polygon_params['fill_color'].tolist()==[i+2 for i in range(1,len(source_geometries)+1)]
    assert source.read_bytes()==before


def test_coincident_regions_still_have_separate_native_delete_and_subset_identity(tmp_path):
    from mapgis_agent.native_com import prepare_native_subset
    from mapgis_agent.features import read_features
    bridge=Bridge(tmp_path);native_settings(tmp_path);square=box(0,0,10,10)
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':f'region-{i}','geometry':mapping(square),
        'properties':{'ID':7,'value':i*11}} for i in range(1,4)]}
    frame={'kind':'local','name':'coincident identity fixture','unit':'metre','axis_order':'xy','reference_sha256':'0'*64}
    prepare_creation(bridge,features,{'name':'three.wp','map_scale':1000,'crs':frame},'create')
    source=Path(bridge.apply('create')['output']);before=source.read_bytes()
    prepare_native_subset(bridge,str(source),file_sha(source),[1,3],{'name':'selected.wp'},'subset')
    subset=bridge.apply('subset');rows=read_features(subset['output'],subset['candidate_sha256'])['collection']['features']
    assert [f['properties']['value'] for f in rows]==[11,33] and [f['id'] for f in rows]==['1','2']
    prepare_native_operation(bridge,'feature.delete',str(source),file_sha(source),{'record_ids':[2]},'delete-middle')
    result=bridge.apply('delete-middle');rows=read_features(result['output'],result['candidate_sha256'])['collection']['features']
    assert [f['properties']['value'] for f in rows]==[11,33] and [f['id'] for f in rows]==['1','3']
    assert source.read_bytes()==before
