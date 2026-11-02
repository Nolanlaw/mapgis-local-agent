"""Branch-controlled scan tracing over the existing MIT GeoScan skeleton graph.

Every call is a deterministic read. Rewind by limiting the accepted segment
prefix; resume by extending the explicit branch decision list on the same graph.
No geological meaning, gap connection or native file edit is inferred.
"""
from __future__ import annotations
import json,math
from pathlib import Path
import cv2,numpy as np
from pydantic import BaseModel,ConfigDict,Field,StrictFloat,StrictInt
from rasterio.features import rasterize
from shapely.geometry import LineString,shape
from ._vendor.geoscan_trace import zhang_suen_thin,trace_skeleton_paths
from .core import BridgeError,json_text,sha
from .raster import affine_point,file_sha


class TraceBridge(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    start:list[StrictFloat]=Field(min_length=2,max_length=2)
    end:list[StrictFloat]=Field(min_length=2,max_length=2)
    reason:str=Field(min_length=1,max_length=240)


class SeedTraceParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    seed:list[StrictFloat]=Field(min_length=2,max_length=2)
    rgb_min:list[StrictInt]|None=None
    rgb_max:list[StrictInt]|None=None
    gray_threshold:StrictInt|None=None
    min_halfwidth_px:StrictFloat=Field(default=0.,ge=0,le=64)
    max_halfwidth_px:StrictFloat=Field(default=8.,gt=0,le=64)
    snap_radius_px:StrictFloat=Field(default=3.,ge=0,le=32)
    max_gap_px:StrictFloat=Field(default=8.,gt=0,le=32)
    bridges:list[TraceBridge]=Field(default_factory=list,max_length=1000)
    exclude_regions:list[dict]=Field(default_factory=list,max_length=1000)
    role:str='boundary'
    georeference_id:str|None=None


def _pixel(p):return [p[0]+.5,p[1]+.5]


def _locate(point,pixels,radius):
    x,y=point;nearest=[];best=radius*radius+1e-12
    for pixel in pixels:
        distance=(pixel[0]+.5-x)**2+(pixel[1]+.5-y)**2
        if distance<best-1e-12:best=distance;nearest=[pixel]
        elif abs(distance-best)<=1e-12:nearest.append(pixel)
    if not nearest:raise BridgeError('TRACE_SEED_NOT_ON_LINE: inspect color/width/window or move the seed within snap_radius_px')
    if len(nearest)>1:raise BridgeError('TRACE_SEED_AMBIGUOUS: use one exact pixel center from '+json_text([_pixel(p) for p in sorted(nearest)[:8]]))
    return nearest[0],math.sqrt(best)


def _graph(rgb,p):
    if p.rgb_min is not None or p.rgb_max is not None:
        if p.gray_threshold is not None or any(v is None or len(v)!=3 or any(not 0<=x<=255 for x in v) for v in (p.rgb_min,p.rgb_max)) or any(a>b for a,b in zip(p.rgb_min,p.rgb_max)):raise BridgeError('TRACE_COLOR_RANGE: explicit RGB low/high triples or gray_threshold, without mixing')
        mask=cv2.inRange(rgb,np.array(p.rgb_min,np.uint8),np.array(p.rgb_max,np.uint8))>0
    else:
        if p.gray_threshold is None or not 0<=p.gray_threshold<=255:raise BridgeError('TRACE_COLOR_REQUIRED: choose RGB bounds or gray_threshold explicitly')
        mask=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)<=p.gray_threshold
    if p.min_halfwidth_px>p.max_halfwidth_px:raise BridgeError('TRACE_WIDTH_RANGE')
    if p.role not in {'boundary','fault','contour','annotation','protected','symbol'}:raise BridgeError('EXPLICIT_LINE_ROLE_REQUIRED')
    excluded=[]
    for item in p.exclude_regions:
        if set(item)!={'geometry','reason'} or not isinstance(item['reason'],str) or not item['reason']:raise BridgeError('TRACE_EXCLUSION_REASON')
        geometry=shape(item['geometry'])
        if geometry.geom_type not in {'Polygon','MultiPolygon'} or not geometry.is_valid or geometry.is_empty or not all(math.isfinite(x) for x in geometry.bounds):raise BridgeError('TRACE_EXCLUSION_POLYGON')
        excluded.append(geometry)
    if excluded:mask&=~rasterize(((g,1) for g in excluded),out_shape=mask.shape,fill=0,dtype='uint8').astype(bool)
    # Pad explicitly so a stroke touching the image edge has known background
    # outside the bounded observation, rather than an infinite distance.
    distance=cv2.distanceTransform(np.pad(mask.astype('uint8'),1),cv2.DIST_L2,cv2.DIST_MASK_PRECISE)[1:-1,1:-1]
    skeleton=zhang_suen_thin(mask,max_iterations=64)
    if not np.array_equal(skeleton,zhang_suen_thin(skeleton,max_iterations=1)):raise BridgeError('TRACE_THINNING_INCOMPLETE: use a more selective color mask or smaller observation window')
    skeleton&=(distance>=p.min_halfwidth_px)&(distance<=p.max_halfwidth_px)
    if int(skeleton.sum())>1000000:raise BridgeError('TRACE_GRAPH_LIMIT: use a smaller or more selective bounded window')
    paths=trace_skeleton_paths(skeleton)
    paths=sorted(tuple(min(tuple(path),tuple(reversed(path)))) for path in paths if len(path)>=2)
    if not paths:raise BridgeError('TRACE_SEED_NOT_ON_LINE: no eligible centerline after explicit color/width constraints')
    paths=[list(path) for path in paths];bridge_reason={}
    endpoints={path[0] for path in paths}|{path[-1] for path in paths}
    degrees={point:sum((path[0]==point)+(path[-1]==point) for path in paths) for point in endpoints}
    for bridge in p.bridges:
        a,_=_locate(bridge.start,endpoints,.01);b,_=_locate(bridge.end,endpoints,.01)
        if a==b or degrees[a]!=1 or degrees[b]!=1 or math.dist(a,b)>p.max_gap_px:raise BridgeError('TRACE_GAP_ENDPOINTS: bridge unique degree-one endpoints within max_gap_px explicitly')
        pair=tuple(sorted([a,b]))
        if any(tuple(sorted([path[0],path[-1]]))==pair for path in paths):raise BridgeError('TRACE_DUPLICATE_BRIDGE')
        bridge_reason[len(paths)]=bridge.reason;paths.append(list(pair))
    return paths,distance,bridge_reason,{'mask_pixels':int(mask.sum()),'skeleton_pixels':int(skeleton.sum()),'paths':len(paths)}


def seed_trace(raster,scan_id,window_id,expected_window_sha256,parameters,decisions=None,expected_graph_sha256=None,stop_after_segments=None):
    p=parameters if isinstance(parameters,SeedTraceParameters) else SeedTraceParameters.model_validate(parameters)
    decisions=decisions or []
    if not isinstance(decisions,list) or len(decisions)>2000 or any(not isinstance(v,str) for v in decisions):raise BridgeError('TRACE_DECISIONS')
    if decisions and expected_graph_sha256 is None:raise BridgeError('TRACE_GRAPH_HASH_REQUIRED')
    if stop_after_segments is not None and (type(stop_after_segments) is not int or not 0<=stop_after_segments<=2000):raise BridgeError('TRACE_STEP_LIMIT')
    scan,window,manifest=raster._load_window(scan_id,window_id)
    if window['output_sha256']!=expected_window_sha256:raise BridgeError('STALE_WINDOW')
    source=raster.bridge.owned_path(window['output']);import pyvips
    image=pyvips.Image.new_from_file(str(source),access='sequential');w,h=window['plan']['output_size']
    if (image.width,image.height)!=(w,h) or w*h>4194304 or image.bands<3:raise BridgeError('TRACE_BOUNDED_RGB_WINDOW: at most4194304 pixels')
    rgb=np.frombuffer(image.extract_band(0,n=3).cast('uchar').write_to_memory(),np.uint8).reshape(h,w,3)
    paths,width,bridges,qa=_graph(rgb,p)
    identity=sha(json_text({'window':expected_window_sha256,'mask_parameters':p.model_dump(exclude={'seed','georeference_id'}),'paths':paths}).encode())
    if expected_graph_sha256 is not None and identity!=expected_graph_sha256:raise BridgeError('TRACE_GRAPH_CHANGED: continue only the same image and graph parameters')
    pixels={point for path in paths for point in path};seed,snap_distance=_locate(p.seed,pixels,p.snap_radius_px)
    locations={point:[] for point in pixels}
    for index,path in enumerate(paths):
        for position,point in enumerate(path):locations[point].append((index,position))
    current=seed;used=set();history=[];coordinates=[];chosen=0;used_bridges=0;state='ENDPOINT';options=[]
    while True:
        options=[]
        for index,position in locations[current]:
            if index in used:continue
            closed=paths[index][0]==paths[index][-1]
            if closed and position==len(paths[index])-1:continue
            for direction in [-1,1]:
                if not closed and not 0<=position+direction<len(paths[index]):continue
                if closed:
                    ring=paths[index][:-1];points=[ring[(position+direction*k)%len(ring)] for k in range(len(ring)+1)]
                else:points=paths[index][position::direction] if direction>0 else paths[index][position::-1]
                key=f'p{index+1:06d}:{position}:{direction}'
                options.append({'id':key,'path':index,'start_position':position,'direction':direction,'points':points,
                    'start_pixel':_pixel(points[0]),'next_pixel':_pixel(points[1]),'end_pixel':_pixel(points[-1]),'length_px':float(LineString(points).length),
                    'explicit_gap_reason':bridges.get(index)})
        if not options:break
        if stop_after_segments is not None and len(history)>=stop_after_segments:state='PAUSED_STEP_LIMIT';break
        if len(history)>=2000 or len(coordinates)>20000:state='PAUSED_SIZE_LIMIT';break
        if len(options)>1:
            if chosen==len(decisions):state='PAUSED_BRANCH';break
            option=next((o for o in options if o['id']==decisions[chosen]),None)
            if option is None:raise BridgeError('TRACE_BRANCH_CHOICE: select one current choice id')
            chosen+=1
        else:option=options[0]
        points=option['points']
        if len(coordinates)+len(points)-(1 if coordinates else 0)>20000:raise BridgeError('TRACE_ROUTE_LIMIT: one route exceeds20000 vertices; use smaller windows or stop_after_segments before the long segment')
        used.add(option['path']);current=points[-1]
        start=len(coordinates)-1 if coordinates else 0
        coordinates.extend(points[1:] if coordinates else points)
        used_bridges+=int(option['path'] in bridges)
        history.append({k:v for k,v in option.items() if k not in {'points','path'}}|{'vertex_start':start,'vertex_end':len(coordinates)-1})
    if chosen!=len(decisions) and state!='PAUSED_STEP_LIMIT':raise BridgeError('TRACE_UNUSED_DECISIONS: remove decisions beyond the current route/endpoint')
    # Source frame uses pixel centers, including preview scaling/offset. Map
    # coordinates are optional and require the existing accepted affine record.
    georef=None;georef_hash=None
    if p.georeference_id:
        raster.bridge.request_id(p.georeference_id);record=raster.bridge.owned_path(raster.root/'work/georeference'/(p.georeference_id+'.json'))
        georef=json.loads(record.read_text(encoding='utf8'));georef_hash=file_sha(record)
        if not georef.get('accepted') or georef.get('source_sha256')!=scan['source_sha256']:raise BridgeError('GEOREFERENCE_NOT_ACCEPTED')
    def transform(point):
        result=affine_point(window['plan']['pixel_to_source'],_pixel(point))
        return list(affine_point(georef['coefficients'],result) if georef else result)
    geometry={'type':'LineString','coordinates':[transform(point) for point in coordinates]} if len(coordinates)>=2 else None
    features={'type':'FeatureCollection','features':[] if geometry is None else [{'type':'Feature','id':'seed-trace','geometry':geometry,
        'properties':{'role':p.role,'review_status':'UNREVIEWED','explicit_gap_bridges_used':used_bridges}}]}
    # Reading again checks both the original scan and its bound window receipt.
    raster._load_window(scan_id,window_id)
    if georef_hash and file_sha(record)!=georef_hash:raise BridgeError('STALE_GEOREFERENCE')
    return {'state':state,'scan_id':scan_id,'window_id':window_id,'source_sha256':scan['source_sha256'],'window_sha256':expected_window_sha256,
        'graph_sha256':identity,'seed_pixel':_pixel(seed),'snap_distance_px':snap_distance,'last_pixel':_pixel(current),
        'geometry':geometry,'features':features,'coordinate_frame':'map' if georef else 'source_pixel','crs':georef['crs'] if georef else None,
        'decisions_consumed':chosen,'history':history,'choices':[{k:v for k,v in o.items() if k not in {'points','path'}} for o in options],
        'explicit_gap_bridges_used':used_bridges,'automatic_gap_closing':False,'automatic_geological_interpretation':False,
        'width_metric':'Euclidean center-to-background pixel distance on the binary mask; not a physical stroke width',
        'qa':qa,'source_preserved':True,'continuation':'Repeat the seed/graph with branch choice ids and expected_graph_sha256. Reduce stop_after_segments to rewind; keep only the decisions used before a changed branch. Output stays UNREVIEWED until explicitly accepted into native creation.'}
