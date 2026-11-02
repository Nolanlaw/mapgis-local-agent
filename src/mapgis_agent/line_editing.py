"""Signed source-unit endpoint distances for the existing native line editor."""
from __future__ import annotations
import math
import numpy as np
from shapely import get_coordinates
from shapely.geometry import LineString, Point, shape
from shapely.ops import substring, nearest_points
from shapely.errors import GEOSException
from .core import BridgeError, native_ids


def prepare_resize(parameters,reader):
    if reader.shapeType!='LINE':raise BridgeError('RESIZE_TYPE: saved WL required')
    if reader.crs and reader.crs.is_geographic:raise BridgeError('RESIZE_FRAME: projected or declared local source coordinates required')
    updates=parameters.get('updates') if isinstance(parameters,dict) else None
    if not isinstance(parameters,dict) or set(parameters)!={'updates'} or not isinstance(updates,list) or not 1<=len(updates)<=1000:
        raise BridgeError('RESIZE_UPDATES: explicit record_id/start_delta/end_delta required')
    records=dict(zip(native_ids(reader),reader.geom));seen=set();result=[];details=[]
    for update in updates:
        if not isinstance(update,dict) or set(update)!={'record_id','start_delta','end_delta'}:raise BridgeError('RESIZE_UPDATES')
        rid=update['record_id']
        if type(rid) is not int or rid not in records or rid in seen:raise BridgeError('RESIZE_IDS: unique active physical IDs required')
        if any(type(update[k]) not in (int,float) or not math.isfinite(update[k]) for k in ('start_delta','end_delta')):raise BridgeError('RESIZE_DISTANCES: finite signed source units required')
        seen.add(rid);original=np.asarray(records[rid].coords,dtype=float)
        if original.ndim!=2 or original.shape[1]!=2:raise BridgeError('RESIZE_COORDINATES: 2D vertices required')
        origin=original[0].copy();relative=original-origin
        points=relative[np.r_[True,np.any(relative[1:]!=relative[:-1],axis=1)]]
        if len(points)<2:raise BridgeError('RESIZE_COLLAPSED: nonzero source line required')
        line=LineString(points);length=line.length
        start,end=-update['start_delta'],length+update['end_delta']
        if not math.isfinite(length) or not math.isfinite(end) or end-start<=32*max(math.ulp(start),math.ulp(end),math.ulp(length)):
            raise BridgeError('RESIZE_COLLAPSED: the end must lie after the start along the extended line')
        forward=points[1]-points[0];forward/=math.hypot(*forward)
        backward=points[-1]-points[-2];backward/=math.hypot(*backward)
        def position(distance):
            if distance<0:return points[0]+distance*forward
            if distance>length:return points[-1]+(distance-length)*backward
            return np.asarray(line.interpolate(distance).coords[0])
        coordinates=[position(start).tolist()]
        left,right=max(0.,start),min(length,end)
        if left<=right:coordinates.extend(list(p) for p in substring(line,left,right).coords)
        coordinates.append(position(end).tolist())
        unique=[coordinates[0]]
        for point in coordinates[1:]:
            if point!=unique[-1]:unique.append(point)
        actual=(np.asarray(unique)+origin).tolist()
        if update['start_delta']==0 and update['end_delta']==0:actual=original.tolist()
        result.append({'record_id':rid,'coordinates':actual})
        details.append({'record_id':rid,'before_length':length,'after_length':end-start})
    return {'updates':sorted(result,key=lambda u:u['record_id']), 'resize':{'algorithm':'geos-substring-tangent-v1',
        'coordinate_frame':'source','distances':sorted(updates,key=lambda u:u['record_id']),
        'lengths':sorted(details,key=lambda u:u['record_id'])}}


def prepare_extend_to(parameters,reader):
    if reader.shapeType!='LINE':raise BridgeError('EXTEND_TYPE: saved WL required')
    if reader.crs and reader.crs.is_geographic:raise BridgeError('EXTEND_FRAME: projected or declared local coordinates required')
    updates=parameters.get('updates') if isinstance(parameters,dict) else None
    if not isinstance(parameters,dict) or set(parameters)!={'updates'} or not isinstance(updates,list) or not 1<=len(updates)<=1000:
        raise BridgeError('EXTEND_UPDATES: record_id/end/max_distance/boundary required')
    records=dict(zip(native_ids(reader),reader.geom));seen=set();distances=[];hits=[]
    for update in updates:
        if not isinstance(update,dict) or set(update)!={'record_id','end','max_distance','boundary'}:raise BridgeError('EXTEND_UPDATES')
        rid=update['record_id'];end=update['end'];maximum=update['max_distance']
        if type(rid) is not int or rid not in records or rid in seen:raise BridgeError('EXTEND_IDS: unique active physical IDs required')
        if end not in ('start','end') or type(maximum) not in (int,float) or not math.isfinite(maximum) or maximum<=0:raise BridgeError('EXTEND_PARAMETERS')
        seen.add(rid)
        try:
            boundary=shape(update['boundary'])
            if (boundary.geom_type not in ('LineString','MultiLineString','Polygon','MultiPolygon','Point','MultiPoint')
                    or boundary.is_empty or boundary.has_z or not np.isfinite(get_coordinates(boundary)).all() or not boundary.is_valid):
                raise ValueError('finite valid 2D target required')
            if boundary.geom_type in ('Polygon','MultiPolygon'):boundary=boundary.boundary
        except (ValueError,TypeError,AttributeError,KeyError,GEOSException) as exc:raise BridgeError('EXTEND_BOUNDARY: '+str(exc)) from exc
        points=np.asarray(records[rid].coords,dtype=float)
        if points.ndim!=2 or points.shape[1]!=2:raise BridgeError('EXTEND_COORDINATES: 2D source required')
        points=points[np.r_[True,np.any(points[1:]!=points[:-1],axis=1)]]
        if len(points)<2:raise BridgeError('EXTEND_COLLAPSED')
        start=points[0] if end=='start' else points[-1]
        direction=points[0]-points[1] if end=='start' else points[-1]-points[-2]
        direction/=math.hypot(*direction);finish=start+maximum*direction
        if not np.isfinite(finish).all() or np.array_equal(finish,start):raise BridgeError('EXTEND_PRECISION')
        intersection=LineString([start,finish]).intersection(boundary)
        if intersection.is_empty:raise BridgeError('EXTEND_NO_TARGET_WITHIN_DISTANCE')
        hit=nearest_points(Point(start),intersection)[1];distance=Point(start).distance(hit)
        distances.append({'record_id':rid,'start_delta':distance if end=='start' else 0.,'end_delta':distance if end=='end' else 0.})
        hits.append({'record_id':rid,'end':end,'distance':distance,'target_coordinates':list(hit.coords[0])})
    result=prepare_resize({'updates':distances},reader)
    result['extend_to']={'algorithm':'geos-first-ray-intersection-v1','coordinate_frame':'source',
        'targets':sorted(updates,key=lambda u:u['record_id']),'hits':sorted(hits,key=lambda u:u['record_id'])}
    return result
