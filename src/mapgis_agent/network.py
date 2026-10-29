"""Explicit planar network models over native WL, using NetworkX and GEOS."""
from __future__ import annotations

import json
import math
from pathlib import Path

import networkx as nx
import numpy as np
from scipy.spatial import cKDTree
from shapely import STRtree
from shapely.geometry import LineString,Point,mapping
from shapely.ops import substring,unary_union

from .core import Bridge,BridgeError,json_text,native_ids,read_native,sha
from .creation import prepare_creation


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _build(path,expected_sha256,parameters):
    before,reader,_=read_native(path)
    if sha(before)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if reader.shapeType!='LINE':raise BridgeError('NETWORK_WL_REQUIRED')
    directed=parameters.get('directed',False)
    tolerance=parameters.get('snap_tolerance',0)
    noding=parameters.get('node_intersections',False)
    if type(directed) is not bool or type(noding) is not bool or not _number(tolerance) or tolerance<0:
        raise BridgeError('NETWORK_MODEL: explicit direction/noding and nonnegative tolerance required')
    field=parameters.get('cost_field')
    if field is not None and field not in reader.data:raise BridgeError('NETWORK_COST_FIELD')
    if field is None and reader.crs and reader.crs.is_geographic:
        raise BridgeError('NETWORK_COST: project geographic coordinates or provide an explicit edge cost field')
    originals=[]
    for row,(rid,g) in enumerate(zip(native_ids(reader),reader.geom)):
        if g.is_empty or not g.is_valid or g.length<=0:raise BridgeError('NETWORK_GEOMETRY')
        cost=float(reader.data.iloc[row][field]) if field is not None else g.length
        if not math.isfinite(cost) or cost<0:raise BridgeError('NETWORK_COST: Dijkstra requires finite nonnegative costs')
        originals.append({'id':rid,'geometry':g,'cost':cost,'row':row})
    if not originals:raise BridgeError('NETWORK_EMPTY')
    if noding:
        geometries=[x['geometry'] for x in originals]
        tree=STRtree(geometries)
        merged=unary_union(geometries)
        segments=list(merged.geoms) if merged.geom_type=='MultiLineString' else [merged]
        edges=[]
        for segment in segments:
            owners=tree.query(segment,predicate='covered_by')
            if not len(owners):raise BridgeError('NETWORK_PROVENANCE: noded segment has no source owner')
            for owner in owners:
                original=originals[int(owner)]
                points=list(segment.coords)
                if original['geometry'].project(Point(points[0]))>original['geometry'].project(Point(points[-1])):
                    points.reverse()
                edges.append(original|{'geometry':LineString(points),
                    'cost':original['cost']*segment.length/original['geometry'].length})
    else:edges=originals
    nodes=sorted({tuple(edge['geometry'].coords[i]) for edge in edges for i in (0,-1)})
    representatives={node:node for node in nodes}
    if tolerance:
        union=nx.utils.UnionFind(range(len(nodes)))
        for a,b in cKDTree(nodes).query_pairs(tolerance):union.union(a,b)
        for group in union.to_sets():
            representative=min(nodes[i] for i in group)
            for i in group:representatives[nodes[i]]=representative
    graph=nx.MultiDiGraph() if directed else nx.MultiGraph()
    for index,edge in enumerate(edges):
        points=list(edge['geometry'].coords)
        a,b=representatives[tuple(points[0])],representatives[tuple(points[-1])]
        points[0],points[-1]=a,b
        geometry=LineString(points)
        if geometry.length==0:raise BridgeError('NETWORK_SNAP_COLLAPSED_EDGE')
        cost=geometry.length if field is None else edge['cost']
        graph.add_edge(a,b,key=index,weight=cost,geometry=geometry,source_id=edge['id'],row=edge['row'],start=a,end=b)
    return before,reader,graph


def _nearest(graph,coordinate,tolerance):
    if not isinstance(coordinate,list) or len(coordinate)!=2 or not all(_number(v) for v in coordinate):
        raise BridgeError('NETWORK_NODE: explicit finite x/y required')
    coordinate=tuple(coordinate)
    if coordinate in graph:return coordinate
    distances=sorted((math.dist(coordinate,node),node) for node in graph.nodes)
    if not distances or distances[0][0]>tolerance:raise BridgeError('NETWORK_NODE_NOT_FOUND: choose a node or explicit snap tolerance')
    if len(distances)>1 and math.isclose(distances[0][0],distances[1][0],abs_tol=1e-12,rel_tol=0):
        raise BridgeError('NETWORK_AMBIGUOUS_NODE')
    return distances[0][1]


def analyze_network(file:str,expected_sha256:str,parameters:dict)->dict:
    operation=parameters.get('operation')
    common={'operation','directed','cost_field','snap_tolerance','node_intersections'}
    required={'route':{'source','target'},'service_area':{'source','cutoff'},'components':set()}
    if operation not in required or set(parameters)-(common|required[operation]) or not required[operation]<=set(parameters):
        raise BridgeError('NETWORK_PARAMETERS')
    path=Path(file).resolve(strict=True)
    before,reader,graph=_build(path,expected_sha256,parameters)
    tolerance=parameters.get('snap_tolerance',0)
    output=[]
    mapping_fields={'长度':'_source_length','面积':'_source_area','周长':'_source_perim'}
    reserved={'_source_id','_route_cost','_route_order','_component',*mapping_fields.values()}
    if reserved & set(reader.data):raise BridgeError('NETWORK_RESERVED_FIELD')
    def append(edge,geometry,cost,component=None):
        properties={mapping_fields.get(key,key):(value.item() if isinstance(value,np.generic) else value)
                    for key,value in reader.data.iloc[edge['row']].to_dict().items()}
        properties.update(_source_id=int(edge['source_id']),_route_cost=float(cost),_route_order=len(output)+1)
        if component is not None:properties['_component']=component
        output.append({'type':'Feature','id':f'network-{len(output)+1}',
            'geometry':mapping(geometry),'properties':properties})
    summary={'graph_nodes':graph.number_of_nodes(),'graph_edges':graph.number_of_edges(),
             'cost_model':'attribute:'+parameters['cost_field'] if parameters.get('cost_field') else 'source planar coordinate length',
             'source_record_ids':[]}
    if operation=='route':
        start=_nearest(graph,parameters['source'],tolerance)
        end=_nearest(graph,parameters['target'],tolerance)
        try:cost,nodes=nx.single_source_dijkstra(graph,start,end,weight='weight')
        except nx.NetworkXNoPath as exc:raise BridgeError('NETWORK_NO_PATH') from exc
        selected=[]
        for a,b in zip(nodes,nodes[1:]):
            edge=min(graph[a][b].values(),key=lambda e:(e['weight'],e['source_id']))
            geometry=edge['geometry']
            if edge['start']!=a:geometry=LineString(list(geometry.coords)[::-1])
            append(edge,geometry,edge['weight']);selected.append(edge['source_id'])
        summary.update(cost=float(cost),nodes=[list(node) for node in nodes],source_record_ids=selected)
    elif operation=='service_area':
        cutoff=parameters['cutoff']
        if not _number(cutoff) or cutoff<0:raise BridgeError('NETWORK_CUTOFF')
        start=_nearest(graph,parameters['source'],tolerance)
        distances=nx.single_source_dijkstra_path_length(graph,start,cutoff=cutoff,weight='weight')
        selected=[]
        for a,b,key,edge in graph.edges(keys=True,data=True):
            a,b=edge['start'],edge['end'];weight=edge['weight'];intervals=[]
            if a in distances:
                intervals.append((0.,1. if weight==0 else min(1.,max(0.,(cutoff-distances[a])/weight))))
            if not graph.is_directed() and b in distances:
                intervals.append((0. if weight==0 else 1.-min(1.,max(0.,(cutoff-distances[b])/weight)),1.))
            merged=[]
            for lo,hi in sorted(intervals):
                if hi<=lo:continue
                if merged and lo<=merged[-1][1]:merged[-1]=(merged[-1][0],max(hi,merged[-1][1]))
                else:merged.append((lo,hi))
            for lo,hi in merged:
                geometry=substring(edge['geometry'],lo,hi,normalized=True)
                if geometry.geom_type=='LineString' and geometry.length>0:
                    append(edge,geometry,weight*(hi-lo));selected.append(edge['source_id'])
        summary.update(cutoff=cutoff,reachable_nodes=len(distances),source_record_ids=selected,
            result_kind='reachable network segments; not an areal isochrone')
    else:
        components=nx.weakly_connected_components(graph) if graph.is_directed() else nx.connected_components(graph)
        groups=list(components)
        for number,nodes in enumerate(groups,1):
            for a,b,key,edge in graph.subgraph(nodes).edges(keys=True,data=True):
                append(edge,edge['geometry'],edge['weight'],number)
        summary.update(component_count=len(groups),source_record_ids=[f['properties']['_source_id'] for f in output])
    if path.read_bytes()!=before:raise BridgeError('STALE_DURING_NETWORK')
    return {'backend':'NetworkX/GEOS','source':str(path),'source_sha256':expected_sha256,
            'summary':summary,'features':{'type':'FeatureCollection','features':output},'source_preserved':True}


def prepare_network(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str)->dict:
    if Path(template.get('name','')).suffix.lower()!='.wl':raise BridgeError('NETWORK_WL_OUTPUT_REQUIRED')
    from .core import require_native_crs
    _,reader,_=read_native(Path(file).resolve(strict=True))
    require_native_crs(reader,template.get('crs',{}),'NETWORK_CRS_MISMATCH')
    result=analyze_network(file,expected_sha256,parameters)
    if not result['features']['features']:raise BridgeError('NETWORK_EMPTY_RESULT')
    provenance=dict(template.get('provenance',{}))|{'backend':result['backend'],'source':result['source'],
        'input_sha256':expected_sha256,'parameters':parameters,'summary':result['summary']}
    prepared=prepare_creation(bridge,result['features'],template|{'provenance':provenance},request_id)
    extra={'backend':result['backend'],'input_file':result['source'],'input_sha256':expected_sha256,
           'network_summary':result['summary']}
    with bridge.connect() as db:
        row=db.execute('SELECT receipt FROM commands WHERE id=?',(request_id,)).fetchone()
        db.execute('UPDATE commands SET receipt=? WHERE id=?',(json_text(json.loads(row['receipt'])|extra),request_id))
    return prepared|extra
