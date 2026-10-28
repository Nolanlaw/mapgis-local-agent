from shapely.geometry import LineString, Polygon, Point, mapping, shape
import pytest

from mapgis_agent.core import Bridge, BridgeError


CRS = {"definition":"EPSG:4549","axis_order":"xy"}


def features(items):
    return {"type":"FeatureCollection","features":[
        {"type":"Feature","id":key,"geometry":mapping(geom),"properties":props}
        for key,geom,props in items]}


def test_topology_build_does_not_delete_open_faults_or_thin_bodies(tmp_path):
    from mapgis_agent.vector import prepare_vector_operation
    from pymapgis import Reader
    frame=LineString([(0,0),(10,0),(10,10),(0,10),(0,0)])
    thin=LineString([(4,0),(4,10)])
    second=LineString([(4.01,0),(4.01,10)])
    fault=LineString([(1,1),(3,8)])
    source=features([('frame',frame,{'role':'boundary'}),('thin-a',thin,{'role':'boundary'}),
                     ('thin-b',second,{'role':'boundary'}),('fault',fault,{'role':'fault'})])
    bridge=Bridge(tmp_path)
    result=prepare_vector_operation(bridge,'topology.polygonize',source,
        {'crs':CRS,'name':'areas.wp','map_scale':1000}, {}, 'build')
    assert result['qa']['preserved_nonboundary_ids']==['fault']
    areas=Reader(bridge.apply('build')['output'],make_valid=False).geom
    assert len(areas)==3
    assert sorted(g.area for g in areas)==pytest.approx([.1,40,59.9])
    assert result['qa']['dangles']==0


def test_overlay_clip_retains_attributes_and_feature_lineage(tmp_path):
    from mapgis_agent.vector import prepare_vector_operation
    from pymapgis import Reader
    source=features([('a',Polygon([(0,0),(10,0),(10,10),(0,10)]),{'ID':9,'地层':'T2d'})])
    clip=mapping(Polygon([(5,-1),(11,-1),(11,11),(5,11)]))
    bridge=Bridge(tmp_path)
    result=prepare_vector_operation(bridge,'overlay.clip',source,
        {'crs':CRS,'name':'clip.wp','map_scale':1000},{'mask':clip},'clip')
    reader=Reader(bridge.apply('clip')['output'])
    assert reader.geom[0].area==pytest.approx(50)
    assert reader.data['地层'].tolist()==['T2d']
    assert result['feature_mapping'][0]['feature_id']=='a'
    assert result['qa']['source_feature_ids']==['a']


def test_buffer_refuses_degrees_and_checks_radius(tmp_path):
    from mapgis_agent.vector import prepare_vector_operation
    source=features([('a',Point(120,30),{})])
    with pytest.raises(BridgeError,match='PROJECTED_UNITS_REQUIRED'):
        prepare_vector_operation(Bridge(tmp_path),'geometry.buffer',source,
            {'crs':{'definition':'EPSG:4326','axis_order':'xy'},'name':'buffer.wp','map_scale':1000},
            {'distance':10},'bad')


def test_classification_refuses_unmapped_codes_and_reprojection_is_real(tmp_path):
    from mapgis_agent.vector import prepare_vector_operation
    from pymapgis import Reader
    bridge=Bridge(tmp_path)
    source=features([('p',Point(120,30),{'code':'T2d'})])
    template={'crs':{'definition':'EPSG:4490','axis_order':'xy'},'name':'point.wt','map_scale':1000}
    with pytest.raises(BridgeError,match='UNMAPPED_CLASSES'):
        prepare_vector_operation(bridge,'style.classify',source,template,{'field':'code','mapping':{}},'missing')
    result=prepare_vector_operation(bridge,'projection.transform',source,
        {**template,'crs':CRS}, {'source_crs':template['crs']},'project')
    point=Reader(bridge.apply('project')['output']).geom[0]
    assert point.x==pytest.approx(500000,abs=.001)
    assert point.y==pytest.approx(3320113.398,abs=.01)
    assert result['qa']['coordinates_transformed']
