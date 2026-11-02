from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point,LineString,Polygon,MultiLineString

from mapgis_agent.core import Bridge,BridgeError,read_native,sha
from mapgis_agent.exchange import vector_source_info,prepare_import
from mapgis_agent.native_backend import prepare_native_operation
from test_native_raster import native_settings

CRS={'definition':'+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}


@pytest.mark.parametrize('geometry,suffix',[
    (Point(400000,2800000),'wt'),
    (LineString([(400000,2800000),(400010,2800002)]),'wl'),
    (Polygon([(400000,2800000),(400020,2800000),(400020,2800020),(400000,2800020)],
             [[(400005,2800005),(400005,2800010),(400010,2800010),(400010,2800005)]]),'wp')])
def test_shapefile_multifile_import_preserves_chinese_attributes_holes_crs_and_native_reopen(tmp_path,geometry,suffix):
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    source=tmp_path/'source.shp'
    gpd.GeoDataFrame({'ID':[7],'岩性':['砂岩'],'value':[1.25]},geometry=[geometry],crs=CRS['definition']).to_file(source,encoding='UTF-8')
    original={p:p.read_bytes() for p in tmp_path.glob('source.*')}
    info=vector_source_info(str(source))
    assert {'.shp','.shx','.dbf','.prj','.cpg'}<={Path(name).suffix for name in info['members']}
    assert info['layers'][0]['geometry_type']==geometry.geom_type
    prepared=prepare_import(bridge,str(source),sha(source.read_bytes()),{'name':'import.'+suffix,'crs':CRS,'map_scale':1000},
        'import',expected_members=info['members'])
    imported=bridge.apply('import');_,reader,_=read_native(Path(imported['output']))
    assert reader.data['ID'].tolist()==[7] and reader.data['岩性'].tolist()==['砂岩']
    assert reader.geom[0].equals_exact(geometry,1e-6) or reader.geom[0].equals(geometry)
    if suffix=='wp':
        from shapely import get_parts
        assert sum(len(p.interiors) for p in get_parts(reader.geom[0]))==1
    saved=prepare_native_operation(bridge,'resave',imported['output'],imported['candidate_sha256'],{},'native')
    assert saved['comparison']['records_equal']
    assert prepared['provenance']['input_members']==info['members']
    assert all(path.read_bytes()==raw for path,raw in original.items())


def test_shapefile_member_hashes_detect_attribute_changes_and_missing_projection_identity(tmp_path):
    bridge=Bridge(tmp_path);source=tmp_path/'points.shp'
    gpd.GeoDataFrame({'ID':[7],'label':['砂岩']},geometry=[Point(400000,2800000)],crs='EPSG:32650').to_file(source,encoding='UTF-8')
    info=vector_source_info(str(source));template={'name':'points.wt','crs':CRS,'map_scale':1000}
    with pytest.raises(BridgeError,match='IMPORT_MEMBERS_REQUIRED'):
        prepare_import(bridge,str(source),sha(source.read_bytes()),template,'missing')
    cpg=source.with_suffix('.cpg');cpg.write_text('GBK',encoding='ascii')
    with pytest.raises(BridgeError,match='STALE_SOURCE_MEMBERS'):
        prepare_import(bridge,str(source),sha(source.read_bytes()),template,'stale',expected_members=info['members'])


def test_multiline_parts_require_explicit_explode_and_keep_duplicate_attribute_ids(tmp_path):
    bridge=Bridge(tmp_path);source=tmp_path/'parts.shp'
    geometry=MultiLineString([[(0,0),(2,2)],[(4,4),(6,4)]])
    gpd.GeoDataFrame({'ID':[7],'label':['边界']},geometry=[geometry]).to_file(source,encoding='UTF-8')
    info=vector_source_info(str(source))
    template={'name':'parts.wl','crs':{'kind':'local','name':'parts','axis_order':'xy','unit':'metre','reference_sha256':'0'*64},'map_scale':1000}
    with pytest.raises(BridgeError,match='MULTIPART_IMPORT'):
        prepare_import(bridge,str(source),sha(source.read_bytes()),template,'reject',expected_members=info['members'])
    result=prepare_import(bridge,str(source),sha(source.read_bytes()),template,'explode',expected_members=info['members'],multipart='explode')
    actual=bridge.apply('explode');_,reader,_=read_native(Path(actual['output']))
    assert len(reader.geom)==2 and reader.data['ID'].tolist()==[7,7]
    assert all(a.equals(b) for a,b in zip(reader.geom,geometry.geoms))
    assert [item['part'] for item in result['provenance']['feature_parts']]==[1,2]


def test_utm_shapefile_can_explicitly_reproject_to_representable_native_crs(tmp_path):
    from pyproj import Transformer
    bridge=Bridge(tmp_path);native_settings(tmp_path);source=tmp_path/'utm.shp'
    point=Point(400000,2800000)
    gpd.GeoDataFrame({'ID':[7]},geometry=[point],crs='EPSG:32650').to_file(source,encoding='UTF-8')
    info=vector_source_info(str(source));template={'name':'projected.wt','crs':CRS,'map_scale':1000}
    with pytest.raises(BridgeError,match='IMPORT_CRS_MISMATCH'):
        prepare_import(bridge,str(source),sha(source.read_bytes()),template,'same',expected_members=info['members'])
    result=prepare_import(bridge,str(source),sha(source.read_bytes()),template,'transform',expected_members=info['members'],reproject=True)
    saved=bridge.apply('transform');_,reader,_=read_native(Path(saved['output']))
    expected=Point(Transformer.from_crs('EPSG:32650',CRS['definition'],always_xy=True).transform(point.x,point.y))
    assert reader.geom[0].distance(expected)<1e-7
    assert result['provenance']['reprojected'] and 'UTM' in result['provenance']['source_crs_wkt']
    native=prepare_native_operation(bridge,'resave',saved['output'],saved['candidate_sha256'],{},'resave')
    assert native['comparison']['records_equal']
