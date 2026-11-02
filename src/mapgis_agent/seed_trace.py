"""Seed/guide tracing over bounded scan ink, reusing GeoScan's thin core."""
from __future__ import annotations

import json
import math

import cv2
import networkx as nx
import numpy as np
from shapely.geometry import LineString,mapping
from shapely.affinity import affine_transform

from ._vendor.geoscan_trace import zhang_suen_thin
from .candidates import _rgb
from .core import BridgeError,json_text,sha
from .raster import RasterWorkspace,file_sha


def trace_scan(raster:RasterWorkspace,scan_id:str,window_id:str,config:dict,request_id:str):
    required={'mode','role','anchors','rgb','color_tolerance','snap_px','stroke_width_px','gap_closing_px','simplify_px'}
    if not required<=set(config)<=required|{'georeference_id'}:raise BridgeError('UNEXPECTED_PARAMETERS')
    if config['mode'] not in {'guided','until_junction'}:raise BridgeError('TRACE_MODE')
    if config['role'] not in {'boundary','fault','contour','protected','annotation','dyke'}:raise BridgeError('EXPLICIT_LINE_ROLE_REQUIRED')
    anchors=config['anchors'];widths=config['stroke_width_px'];closing=config['gap_closing_px']
    if (not isinstance(anchors,list) or not 2<=len(anchors)<=64 or any(not isinstance(p,list) or len(p)!=2
            or any(type(v) not in (float,int) or not math.isfinite(v) for v in p) for p in anchors)):
        raise BridgeError('TRACE_ANCHORS: 2..64 preview pixel positions required')
    if (not isinstance(widths,list) or len(widths)!=2 or any(type(v) not in (float,int) or not math.isfinite(v) for v in widths)
            or not 0<=widths[0]<widths[1]<=4096):raise BridgeError('TRACE_STROKE_WIDTH')
    if type(closing) is not int or not 0<=closing<=8:raise BridgeError('TRACE_GAP_CLOSING')
    for key,low,high in [('color_tolerance',0,442),('snap_px',0,32),('simplify_px',0,16)]:
        value=config[key]
        if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:raise BridgeError('TRACE_'+key.upper())
    bridge=raster.bridge;bridge.request_id(request_id)
    scan,window,manifest=raster._load_window(scan_id,window_id)
    georef=None
    if config.get('georeference_id'):
        gid=bridge.request_id(config['georeference_id'])
        gp=bridge.destination_path(raster.root/'work/georeference'/(gid+'.json'))
        georef=json.loads(gp.read_text(encoding='utf8')) if gp.exists() else None
        if not georef or not georef.get('accepted') or georef['source_sha256']!=scan['source_sha256']:
            raise BridgeError('GEOREFERENCE_NOT_ACCEPTED')
    payload={'trace_protocol':2,'scan_id':scan_id,'window_id':window_id,'config':config,'window_sha256':file_sha(manifest),'georeference':georef}
    fingerprint=sha(json_text(payload).encode())
    directory=bridge.destination_path(raster.root/'work/seed-traces'/request_id)
    output=directory/'features.geojson';receipt=directory/'receipt.json'
    if receipt.is_file():
        old=json.loads(receipt.read_text(encoding='utf8'))
        if old['payload_hash']!=fingerprint:raise BridgeError('REQUEST_ID_CONFLICT')
        if file_sha(output)!=old['features_sha256']:raise BridgeError('TRACE_OUTPUT_CHANGED')
        return old
    image_path=bridge.owned_path(window['output'])
    bgr=cv2.imdecode(np.frombuffer(image_path.read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    if bgr is None or max(bgr.shape[:2])>4096:raise BridgeError('BOUNDED_SCAN_WINDOW_REQUIRED')
    height,width=bgr.shape[:2]
    if any(not 0<=p[0]<width or not 0<=p[1]<height for p in anchors):raise BridgeError('TRACE_ANCHOR_OUTSIDE_WINDOW')
    rgb=cv2.cvtColor(bgr,cv2.COLOR_BGR2RGB)
    mask=np.linalg.norm(rgb.astype(np.float32)-_rgb(config['rgb']).astype(np.float32),axis=2)<=config['color_tolerance']
    if closing:
        mask=cv2.morphologyEx(mask.astype(np.uint8),cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*closing+1,2*closing+1))).astype(bool)
    estimated_width=2*cv2.distanceTransform(mask.astype(np.uint8),cv2.DIST_L2,5)
    mask&=(estimated_width>=widths[0])&(estimated_width<=widths[1])
    skeleton=zhang_suen_thin(mask,max_iterations=64).astype(bool)
    ys,xs=np.nonzero(skeleton)
    if not len(xs):raise BridgeError('TRACE_NO_INK')
    if len(xs)>100000:raise BridgeError('TRACE_WINDOW_TOO_COMPLEX: choose a smaller window or narrower color/width mask')
    _,components=cv2.connectedComponents(skeleton.astype(np.uint8),connectivity=8)
    nodes=set(zip(xs.tolist(),ys.tolist()));snapped=[]
    for anchor in anchors:
        distance=(xs+.5-anchor[0])**2+(ys+.5-anchor[1])**2
        nearest=float(distance.min())
        if nearest>config['snap_px']**2:raise BridgeError('TRACE_ANCHOR_NOT_ON_INK')
        tied=np.flatnonzero(np.isclose(distance,nearest,atol=1e-12,rtol=0))
        if len(set(int(components[ys[i],xs[i]]) for i in tied))>1:raise BridgeError('TRACE_AMBIGUOUS_SEED: equally near disconnected strokes')
        i=int(tied[0]);snapped.append((int(xs[i]),int(ys[i])))
    graph=nx.Graph();graph.add_nodes_from(nodes)
    for x,y in nodes:
        for dx,dy in ((1,0),(0,1),(1,1),(1,-1)):
            other=(x+dx,y+dy)
            if other not in nodes:continue
            # A stair-step corner already has an orthogonal ink connection.
            # Its redundant diagonal forms a triangle and a false degree-3
            # junction. Retain diagonal edges only across true diagonal gaps.
            if dx and dy and ((x+dx,y) in nodes or (x,y+dy) in nodes):continue
            graph.add_edge((x,y),other,length=math.hypot(dx,dy))
    routes=[];choices=[];status='GUIDED'
    if config['mode']=='guided':
        route=[snapped[0]]
        for start,finish in zip(snapped,snapped[1:]):
            if start==finish:continue
            try:
                alternatives=nx.all_shortest_paths(graph,start,finish,weight='length')
                first=next(alternatives)
                second=next(alternatives,None)
            except (nx.NetworkXNoPath,StopIteration) as exc:raise BridgeError('TRACE_DISCONNECTED: no ink path; gap closing is explicit') from exc
            if second is not None:
                routes=[route+first[1:],route+second[1:]];status='AMBIGUOUS';break
            route.extend(first[1:])
        if not routes:routes=[route]
    else:
        start=snapped[0];aim=np.array(snapped[1])-np.array(start)
        if not np.any(aim):raise BridgeError('TRACE_DIRECTION_REQUIRED')
        neighbors=list(graph.neighbors(start))
        if not neighbors:routes=[[start]];status='END'
        else:
            next_node=max(neighbors,key=lambda p:float(np.dot(np.array(p)-start,aim)/np.linalg.norm(np.array(p)-start)))
            route=[start,next_node];visited=set(route)
            while True:
                options=[p for p in graph.neighbors(route[-1]) if p!=route[-2]]
                if len(options)!=1:
                    status='BRANCH' if options else 'END';choices=options;break
                if options[0] in visited:status='LOOP';break
                route.append(options[0]);visited.add(options[0])
            routes=[route]
    features=[]
    def transform(geometry,coefficients):
        a,b,c,d,e,f=coefficients
        return affine_transform(geometry,[a,b,d,e,c,f])
    for i,route in enumerate(routes,1):
        if len(route)<2:continue
        g=LineString([(x+.5,y+.5) for x,y in route]).simplify(config['simplify_px'])
        g=transform(g,window['plan']['pixel_to_source'])
        if georef:g=transform(g,georef['coefficients'])
        features.append({'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{
            'role':config['role'],'review_status':'UNREVIEWED','trace_status':status}})
    qa={'backend':'OpenCV/GeoScan Zhang-Suen/NetworkX','review_status':'UNREVIEWED','candidate_count':len(features),
        'skeleton_pixels':len(nodes),'gap_closing_applied':bool(closing),'gap_closing_px':closing,
        'stroke_width_definition':'two times OpenCV pixel-centre distance transform',
        'pixel_graph':'8-connected ink; redundant corner diagonals omitted',
        'automatic_geological_classification':False,'coordinate_frame':'map' if georef else 'source_pixel',
        'guide_policy':'explicit anchors and shortest ink paths; equal shortest routes pause as AMBIGUOUS'}
    last=routes[0][-1]
    result={'request_id':request_id,'payload_hash':fingerprint,'features':str(output),'trace_status':status,
        'choices':[[x+.5,y+.5] for x,y in sorted(choices)],'endpoint_preview':[last[0]+.5,last[1]+.5],
        'snapped_preview':[[x+.5,y+.5] for x,y in snapped],'config':config,'crs':georef['crs'] if georef else None,
        'source_sha256':scan['source_sha256'],'qa':qa}
    raster._load_window(scan_id,window_id)
    directory.mkdir(parents=True)
    with output.open('x',encoding='utf8') as stream:stream.write(json_text({'type':'FeatureCollection','features':features}))
    result['features_sha256']=file_sha(output)
    with receipt.open('x',encoding='utf8') as stream:stream.write(json_text(result))
    return result
