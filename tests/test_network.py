from pathlib import Path
import geopandas as gpd
from pymapgis import Writer
import pytest
from shapely.geometry import LineString
from mapgis_agent.core import Bridge,BridgeError,read_native,sha

LOCAL={'kind':'local','name':'analytic network','axis_order':'xy','unit':'unknown','reference_sha256':'0'*64}


def network_case(tmp_path,geometries=None,costs=None):
    source=tmp_path/'network.wl'
    geometries=geometries or [LineString([(0,0),(2,2)]),LineString([(0,0),(1,0)]),
        LineString([(1,0),(2,2)]),LineString([(2,2),(3,2)]),LineString([(1,0),(3,2)])]
    from mapgis_agent.creation import prepare_creation
    from shapely.geometry import mapping
    inputs=Bridge(tmp_path/'inputs')
    identity='network-'+sha((''.join(g.wkt for g in geometries)+repr(costs)).encode())[:12]
    prepare_creation(inputs,{'type':'FeatureCollection','features':[
        {'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{'ID':7,'cost':cost}}
        for i,(g,cost) in enumerate(zip(geometries,costs or [4.,2.,1.,5.,8.]),1)]},
        {'name':'network.wl','crs':LOCAL,'map_scale':1000},identity)
    source.write_bytes(Path(inputs.apply(identity)['output']).read_bytes())
    return source,sha(source.read_bytes())


def test_directed_route_uses_physical_ids_costs_and_preserves_input(tmp_path):
    from mapgis_agent.network import analyze_network,prepare_network
    source,digest=network_case(tmp_path)
    parameters={'operation':'route','source':[0,0],'target':[3,2],'directed':True,'cost_field':'cost'}
    result=analyze_network(str(source),digest,parameters)
    assert result['backend']=='NetworkX/GEOS'
    assert result['summary']['cost']==8
    assert result['summary']['source_record_ids']==[2,3,4]
    assert result['summary']['nodes']==[[0,0],[1,0],[2,2],[3,2]]
    bridge=Bridge(tmp_path/'project')
    prepared=prepare_network(bridge,str(source),digest,parameters,{'name':'route.wl','map_scale':1000,'crs':LOCAL},'route')
    assert prepared['state']=='PREPARED'
    _,reader,_=read_native(Path(bridge.apply('route')['output']))
    assert len(reader.geom)==3 and reader.data['_source_id'].tolist()==[2,3,4]
    assert sha(source.read_bytes())==digest
    with pytest.raises(BridgeError,match='NETWORK_NO_PATH'):
        analyze_network(str(source),digest,parameters|{'source':[3,2],'target':[0,0]})


def test_service_range_includes_partial_edge_without_overspending(tmp_path):
    from mapgis_agent.network import analyze_network
    source,digest=network_case(tmp_path,[LineString([(0,0),(10,0)])],[10.])
    result=analyze_network(str(source),digest,{'operation':'service_area','source':[0,0],
        'cutoff':3,'directed':True,'cost_field':'cost'})
    assert result['features']['features'][0]['geometry']['coordinates']==((0.,0.),(3.,0.))
    assert result['summary']['source_record_ids']==[1]


def test_crossings_only_connect_when_noding_is_explicit(tmp_path):
    from mapgis_agent.network import analyze_network
    source,digest=network_case(tmp_path,[LineString([(0,5),(10,5)]),LineString([(5,0),(5,10)])],[10.,10.])
    args={'operation':'route','source':[0,5],'target':[5,10],'directed':False,'cost_field':'cost'}
    with pytest.raises(BridgeError,match='NETWORK_NO_PATH'):
        analyze_network(str(source),digest,args)
    result=analyze_network(str(source),digest,args|{'node_intersections':True})
    assert result['summary']['cost']==pytest.approx(10)
    assert result['summary']['source_record_ids']==[1,2]


def test_parallel_edges_negative_cost_and_stale_source(tmp_path):
    from mapgis_agent.network import analyze_network
    lines=[LineString([(0,0),(10,0)]),LineString([(0,0),(10,0)])]
    source,digest=network_case(tmp_path,lines,[5.,2.])
    args={'operation':'route','source':[0,0],'target':[10,0],'directed':True,'cost_field':'cost'}
    result=analyze_network(str(source),digest,args)
    assert result['summary']['cost']==2 and result['summary']['source_record_ids']==[2]
    with pytest.raises(BridgeError,match='STALE_SOURCE'):
        analyze_network(str(source),'0'*64,args)
    source,digest=network_case(tmp_path,lines,[-5.,2.])
    with pytest.raises(BridgeError,match='NETWORK_COST'):
        analyze_network(str(source),digest,args)
