from pathlib import Path
import pytest
from shapely.geometry import LineString,Point,box,mapping
from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.vector import prepare_vector_operation

LOCAL={'kind':'local','name':'explicit analytic grid','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}


def collection(geometries):
    return {'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':mapping(g),
        'properties':{'ID':7,'code':str(i)}} for i,g in enumerate(geometries,1)]}


def test_crossing_lines_are_noded_with_source_attributes_and_geometry_conserved(tmp_path):
    bridge=Bridge(tmp_path);source=collection([LineString([(0,0),(10,10)]),LineString([(0,10),(10,0)])])
    prepared=prepare_vector_operation(bridge,'topology.node',source,{'name':'noded.wl','crs':LOCAL,'map_scale':1000},{},'node')
    _,reader,_=read_native(Path(bridge.apply('node')['output']))
    assert len(reader.geom)==4 and set(reader.data['code'])=={'1','2'}
    assert sum(g.length for g in reader.geom)==pytest.approx(2*200**.5)
    assert all((5,5) in (tuple(g.coords[0]),tuple(g.coords[-1])) for g in reader.geom)
    assert prepared['qa']['source_count']==2


def test_vector_affine_changes_geometry_and_refuses_singular_transform(tmp_path):
    bridge=Bridge(tmp_path);source=collection([Point(2,3)])
    template={'name':'corrected.wt','crs':LOCAL,'map_scale':1000}
    prepare_vector_operation(bridge,'correction.affine',source,template,{'matrix':[2,0,0,3,10,-5]},'affine')
    _,reader,_=read_native(Path(bridge.apply('affine')['output']))
    assert reader.geom[0].equals(Point(14,4)) and reader.data['ID'].tolist()==[7]
    with pytest.raises(BridgeError,match='AFFINE_MATRIX'):
        prepare_vector_operation(bridge,'correction.affine',source,template,{'matrix':[1,2,2,4,0,0]},'singular')


def test_explicit_precision_grid_produces_shared_native_boundary_without_invalid_areas(tmp_path):
    bridge=Bridge(tmp_path);source=collection([box(0,0,10.004,10),box(10.006,0,20,10)])
    prepare_vector_operation(bridge,'topology.snap_grid',source,{'name':'snapped.wp','crs':LOCAL,'map_scale':1000},{'grid_size':.1},'snap')
    output=Path(bridge.apply('snap')['output']);_,reader,_=read_native(output)
    assert len(reader.geom)==2 and reader.geom[0].touches(reader.geom[1])
    assert sum(g.area for g in reader.geom)==pytest.approx(200)
    from mapgis_agent.topology import validate_wp_topology
    assert validate_wp_topology(output)['shared_arcs']==1
