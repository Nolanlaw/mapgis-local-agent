"""Piecewise linear sections from saved native TIN faces, never retriangulated."""
from __future__ import annotations
import csv,json,math
from pathlib import Path
from typing import Literal
import numpy as np
from pydantic import BaseModel,ConfigDict,Field,StrictFloat
from shapely import STRtree
from shapely.geometry import LineString,Point,Polygon,mapping

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,sha
from .creation import prepare_creation
from .grid_analysis import _linear_scale
from .native_tin import _read_native,_tin_frame
from .raster import file_sha


class ProfileParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    path:list[list[StrictFloat]]=Field(min_length=2,max_length=1000)
    spacing:StrictFloat=Field(gt=0)
    outside:Literal['error','gap']='error'
    vertical_exaggeration:StrictFloat=Field(default=1.,gt=0)


def _intersection_points(geometry):
    if geometry.is_empty:return
    if geometry.geom_type=='Point':yield geometry
    elif geometry.geom_type=='LineString':
        yield Point(geometry.coords[0]);yield Point(geometry.coords[-1])
    elif hasattr(geometry,'geoms'):
        for part in geometry.geoms:yield from _intersection_points(part)


def _section(xyz,faces,edges,parameters):
    p=parameters
    if any(len(v)!=2 for v in p.path):raise BridgeError('TIN_PROFILE_XY_PATH: pairs of map XY coordinates required')
    route=LineString(p.path);length=float(route.length)
    if not length or any(a==b for a,b in zip(p.path,p.path[1:])):raise BridgeError('TIN_PROFILE_PATH: nonzero segments required')
    if math.ceil(length/p.spacing)>20000:raise BridgeError('TIN_PROFILE_SAMPLE_LIMIT: increase spacing; at most 20000 stations including triangle crossings')
    lines=[LineString(xyz[list(e),:2]) for e in edges];edge_tree=STRtree(lines)
    stations=[0.,length];stations.extend(float(v) for v in np.arange(0,length,p.spacing))
    offset=0.
    for a,b in zip(p.path,p.path[1:]):
        segment=LineString([a,b]);stations.append(offset)
        for i in edge_tree.query(segment,predicate='intersects'):
            for point in _intersection_points(segment.intersection(lines[int(i)])):
                stations.append(offset+float(segment.project(point)))
        offset+=segment.length
    tolerance=max(1e-11,length*1e-12);unique=[]
    for v in sorted(stations):
        value=max(0.,min(length,v))
        if not unique or value-unique[-1]>tolerance:unique.append(value)
    if len(unique)>20000:raise BridgeError('TIN_PROFILE_SAMPLE_LIMIT: too many actual triangle crossings')
    triangles=xyz[faces];polygons=[Polygon(t[:,:2]) for t in triangles];face_tree=STRtree(polygons)
    def sample(distance):
        point=route.interpolate(distance);x,y=point.x,point.y
        owners=face_tree.query(point,predicate='intersects')
        identity=min(map(int,owners)) if len(owners) else None;z=None
        if identity is not None:
            t=triangles[identity];weights=np.linalg.solve((t[1:,:2]-t[0,:2]).T,np.array([x,y])-t[0,:2])
            z=float(t[0,2]+np.dot(weights,t[1:,2]-t[0,2]))
        return {'distance':float(distance),'x':float(x),'y':float(y),'z':z,'triangle_id':None if identity is None else identity+1}
    # Boundaries on both sides of a NoData hole are valid. Insert an explicit
    # missing midpoint rather than drawing a line across the missing domain.
    gaps=[(a+b)/2 for a,b in zip(unique,unique[1:]) if sample((a+b)/2)['z'] is None]
    rows=[sample(v) for v in sorted(unique+gaps)]
    if len(rows)>20000:raise BridgeError('TIN_PROFILE_SAMPLE_LIMIT')
    if p.outside=='error' and any(r['z'] is None for r in rows):raise BridgeError('TIN_PROFILE_OUTSIDE: path leaves the saved triangle domain; use outside=gap explicitly to retain gaps')
    return rows,length


def prepare_tin_profile(bridge,file,expected_sha256,parameters,template,request_id):
    p=parameters if isinstance(parameters,ProfileParameters) else ProfileParameters.model_validate(parameters)
    path=Path(file).resolve(strict=True)
    if file_sha(path)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if set(template)-{'name','map_scale','style'} or not {'name','map_scale'}<=set(template) or Path(template['name']).name!=template['name'] or Path(template['name']).suffix.lower()!='.wl':raise BridgeError('TIN_PROFILE_TEMPLATE: new WL basename, map_scale and optional line style required')
    frame,sources=_tin_frame(path,expected_sha256)
    if frame is None:raise BridgeError('TIN_UNIT_METADATA_REQUIRED: profile needs explicit XY and elevation units')
    crs,scale=_linear_scale(frame['crs'],frame['vertical_unit'])
    horizontal_unit=frame['crs']['unit'] if crs is None else crs.axis_info[0].unit_name
    xy_to_metres=({'metre':1.,'millimetre':.001}[horizontal_unit] if crs is None else crs.axis_info[0].unit_conversion_factor)
    payload={'operation':'terrain.tin_profile','file':str(path),'expected_sha256':expected_sha256,'parameters':p.model_dump(),'template':template}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        xyz,faces,edges,quality,host=_read_native(bridge.root,path,stage/'read')
        rows,length=_section(xyz,faces,edges,p)
        valid=[r for r in rows if r['z'] is not None]
        reference=sha(json_text(payload).encode('utf8'))
        section_frame={'kind':'local','name':'TIN section: X=distance in metres; Y=elevation in metres times exaggeration','axis_order':'xy',
            'unit':'metre','reference_sha256':reference}
        output=[];run=[]
        def finish_run():
            if len(run)>=2:output.append({'type':'Feature','id':str(len(output)+1),'geometry':mapping(LineString(run)),
                'properties':{'ID':len(output)+1},'style':template.get('style',{'color':1})})
            run.clear()
        for row in rows:
            if row['z'] is None:finish_run()
            else:run.append((row['distance']*xy_to_metres,row['z']/scale*xy_to_metres*p.vertical_exaggeration))
        finish_run()
        if not output:raise BridgeError('TIN_PROFILE_NO_LINE: no continuous valid profile interval')
        encoder=Bridge(stage/'encoder');vector_template={'name':template['name'],'map_scale':template['map_scale'],'crs':section_frame}
        prepare_creation(encoder,{'type':'FeatureCollection','features':output},vector_template,'line');encoded=encoder.apply('line')
        files=stage/'files';files.mkdir();target=files/template['name'];target.write_bytes(Path(encoded['output']).read_bytes())
        nodes=[{'type':'Feature','id':str(i),'geometry':mapping(Point(r['distance']*xy_to_metres,r['z']/scale*xy_to_metres*p.vertical_exaggeration)),
            'properties':{'ID':i,'distance':r['distance'],'source_x':r['x'],'source_y':r['y'],'Z':r['z'],'triangle':r['triangle_id']}} for i,r in enumerate(valid,1)]
        prepare_creation(encoder,{'type':'FeatureCollection','features':nodes},vector_template|{'name':'profile-points.wt'},'points')
        encoded_points=encoder.apply('points');(files/'profile-points.wt').write_bytes(Path(encoded_points['output']).read_bytes())
        metadata={'schema':'mapgis-agent/tin-profile/1','sources':sources,'crs':section_frame,'source_frame':frame['crs'],
            'source_vertical_unit':frame['vertical_unit'],'horizontal_unit':horizontal_unit,'vertical_to_xy_scale':1/scale,'xy_to_metres':xy_to_metres,
            'vertical_exaggeration':p.vertical_exaggeration,'parameters':p.model_dump(),'samples':rows,
            'sampling':'original saved triangle faces; regular stations plus every route vertex, triangle crossing and explicit gap',
            'geometry':'section coordinates, not source map XY; elevations retained in original declared unit as attributes'}
        (files/'profile.json').write_text(json_text(metadata),encoding='utf8')
        (files/(target.name+'.json')).write_text(json_text({'schema':'mapgis-agent/tin-profile-vector/1','native_sha256':file_sha(target),
            'crs':section_frame,'metadata':'profile.json'}),encoding='utf8')
        with (files/'profile.csv').open('x',newline='',encoding='utf-8-sig') as stream:
            writer=csv.DictWriter(stream,fieldnames=['distance','x','y','z','triangle_id']);writer.writeheader();writer.writerows(rows)
        metrics={'length':length,'length_metres':length*xy_to_metres,'horizontal_unit':horizontal_unit,'stations':len(rows),'valid_stations':len(valid),
            'outside_stations':len(rows)-len(valid),'continuous_lines':len(output),'height_range':[min(r['z'] for r in valid),max(r['z'] for r in valid)]}
        return finish_bundle(bridge,request_id,files,{'backend':'fresh MapGIS native TIN reader; GEOS intersections and NumPy barycentric evaluation',
            'metrics':metrics,'frame':section_frame,'native_reopen':host,'source_preserved':True,'retriangulated':False})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
