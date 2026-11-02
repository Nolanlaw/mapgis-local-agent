from shapely.geometry import LineString, Polygon, Point, mapping, shape
import pytest

from mapgis_agent.core import Bridge, BridgeError


CRS = {"definition":"+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs","axis_order":"xy"}


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


def test_trim_boundary_overshoots_keeps_open_faults_and_rebuilds_the_closed_area(tmp_path):
    from mapgis_agent.vector import prepare_vector_operation
    from pymapgis import Reader
    boundary=[LineString([(-2,0),(12,0)]),LineString([(-2,10),(12,10)]),
              LineString([(0,-2),(0,12)]),LineString([(10,-2),(10,12)])]
    fault=LineString([(2,2),(8,7)])
    source=features([(str(i),g,{'role':'boundary','CODE':i}) for i,g in enumerate(boundary)]+[('fault',fault,{'role':'fault','CODE':9})])
    bridge=Bridge(tmp_path)
    trimmed=prepare_vector_operation(bridge,'topology.trim_dangles',source,
        {'crs':CRS,'name':'trimmed.wl','map_scale':1000},{'max_length':2.01},'trim')
    reader=Reader(bridge.apply('trim')['output'])
    assert trimmed['qa']['trimmed_branches']==8
    assert trimmed['qa']['trimmed_length']==pytest.approx(16)
    assert reader.data['CODE'].tolist().count(9)==1
    assert any(g.equals(fault) for g in reader.geom)
    rebuilt={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':mapping(g),
        'properties':{'role':reader.data.iloc[i]['role']}} for i,g in enumerate(reader.geom)]}
    result=prepare_vector_operation(bridge,'topology.polygonize',rebuilt,
        {'crs':CRS,'name':'area.wp','map_scale':1000},{},'polygonize')
    areas=Reader(bridge.apply('polygonize')['output'])
    assert len(areas.geom)==1 and areas.geom[0].area==pytest.approx(100)
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


@pytest.mark.parametrize('suffix,items',[
    ('wp',[('inside',Polygon([(0,0),(10,0),(10,10),(0,10)]),{'ID':7}),
           ('touch',Polygon([(10,10),(12,10),(12,12),(10,12)]),{'ID':8})]),
    ('wl',[('inside',LineString([(-1,5),(11,5)]),{'ID':7}),
           ('touch',LineString([(-2,-2),(0,0)]),{'ID':8})]),
])
def test_clip_discards_only_lower_dimension_boundary_contacts(tmp_path,suffix,items):
    from mapgis_agent.vector import prepare_vector_operation
    from pymapgis import Reader
    result=prepare_vector_operation(Bridge(tmp_path),'overlay.clip',features(items),
        {'crs':CRS,'name':'clip.'+suffix,'map_scale':1000},
        {'mask':mapping(Polygon([(0,0),(10,0),(10,10),(0,10)]))},'contacts')
    actual=Reader(Bridge(tmp_path).apply('contacts')['output'])
    assert len(actual.geom)==1 and actual.data['ID'].tolist()==[7]
    assert result['qa']['lower_dimension_contacts']==['touch']


def test_file_operation_binds_actual_source_through_publication(tmp_path):
    from pathlib import Path
    from mapgis_agent.creation import prepare_creation
    from mapgis_agent.features import prepare_file_operation
    from mapgis_agent.core import sha
    bridge=Bridge(tmp_path)
    prepare_creation(bridge,features([('a',LineString([(-1,5),(11,5)]),{'ID':7})]),
        {'crs':CRS,'name':'source.wl','map_scale':1000},'source')
    source=Path(bridge.apply('source')['output'])
    result=prepare_file_operation(bridge,'overlay.clip',str(source),sha(source.read_bytes()),
        {'crs':CRS,'name':'clip.wl','map_scale':1000},
        {'mask':mapping(Polygon([(0,0),(10,0),(10,10),(0,10)]))},'bound')
    source.write_bytes(source.read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('bound')
    assert not Path(result['output']).exists()


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
    template={'crs':{'definition':'EPSG:4326','axis_order':'xy'},'name':'point.wt','map_scale':1000}
    with pytest.raises(BridgeError,match='UNMAPPED_CLASSES'):
        prepare_vector_operation(bridge,'style.classify',source,template,{'field':'code','mapping':{}},'missing')
    result=prepare_vector_operation(bridge,'projection.transform',source,
        {**template,'crs':CRS}, {'source_crs':template['crs']},'project')
    point=Reader(bridge.apply('project')['output']).geom[0]
    assert point.x==pytest.approx(500000,abs=.001)
    assert point.y==pytest.approx(3320113.398,abs=.01)
    assert result['qa']['coordinates_transformed']
