"""CDT triangulation with MapGIS native TIN storage and independent readback."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace
import uuid

import numpy as np
import psutil
from shapely import STRtree
from shapely.geometry import LineString,MultiPoint,Point,Polygon,mapping

from .artifacts import begin_bundle,finish_bundle,fail_bundle,reconcile_bundle
from .core import Bridge,BridgeError,json_text,native_ids,read_native,sha
from .grid_analysis import _linear_scale
from .native_backend import backend_configuration,_run_child,completed_native_child
from .native_paths import NativePathAlias,cleanup_aliases
from .native_terrain import _idw_samples
from .raster import file_sha,explicit_crs
from .terrain import _metrics

POINT=np.dtype([('x','<f8'),('y','<f8'),('z','<f8'),('reserved','<u2'),('state','i1'),('pad','u1')])
TRIANGLE=np.dtype([('vertices','<i4',(3,)),('neighbors','<i4',(3,)),('state','i1'),('pad','u1')])


def _tin_frame(path,digest):
    sources=[{'file':str(path),'sha256':digest}];sidecar=path.with_name(path.name+'.json')
    if not sidecar.is_file():return None,sources
    frame=json.loads(sidecar.read_text(encoding='utf8'))
    if frame.get('schema')!='mapgis-agent/tin/1' or frame.get('tin_sha256')!=digest:raise BridgeError('TIN_METADATA_MISMATCH')
    _linear_scale(frame['crs'],frame['vertical_unit'])
    return frame,sources+[{'file':str(sidecar),'sha256':file_sha(sidecar)}]


def _model(path):
    raw=path.read_bytes()
    if len(raw)<16:raise BridgeError('TIN_READBACK_HEADER')
    magic,n,m=struct.unpack_from('<8s2I',raw)
    if magic!=b'MGNT0001' or not 3<=n<=100000 or not 1<=m<=200000 or len(raw)!=16+(n+1)*28+(m+1)*26:
        raise BridgeError('TIN_READBACK_PROFILE')
    points=np.frombuffer(raw,dtype=POINT,count=n+1,offset=16)[1:].copy()
    triangles=np.frombuffer(raw,dtype=TRIANGLE,count=m+1,offset=16+(n+1)*28)[1:].copy()
    xyz=np.column_stack([points[k] for k in ('x','y','z')]);vertices=triangles['vertices']-1
    if (not np.all(np.isfinite(xyz)) or np.any(np.abs(xyz[:,2])>=3.4e18) or np.any(points['state']!=1)
            or np.any(triangles['state']!=1) or vertices.min()<0 or vertices.max()>=n
            or triangles['neighbors'].min()<0 or triangles['neighbors'].max()>m):raise BridgeError('TIN_RECORD_PROFILE')
    if len(set(map(tuple,xyz[:,:2])))!=n:raise BridgeError('TIN_DUPLICATE_VERTICES')
    faces=xyz[vertices,:2];a=faces[:,1]-faces[:,0];b=faces[:,2]-faces[:,0]
    signed=a[:,0]*b[:,1]-a[:,1]*b[:,0]
    if np.any(signed==0) or len(set(tuple(sorted(v)) for v in vertices))!=m:raise BridgeError('TIN_DEGENERATE_FACE')
    edges={};adjacent=[set() for _ in range(m)]
    for i,row in enumerate(vertices):
        for u,v in zip(row,np.roll(row,-1)):
            edge=tuple(sorted((int(u),int(v))));owners=edges.setdefault(edge,[]);owners.append(i)
            if len(owners)>2:raise BridgeError('TIN_NONMANIFOLD_EDGE')
    for owners in edges.values():
        if len(owners)==2:
            a,b=owners;adjacent[a].add(b+1);adjacent[b].add(a+1)
    if any({int(j) for j in row if j}!=adjacent[i] for i,row in enumerate(triangles['neighbors'])):
        raise BridgeError('TIN_NEIGHBOR_GEOMETRY_MISMATCH')
    for i,row in enumerate(vertices):
        for j,(u,v) in enumerate(zip(row,np.roll(row,-1))):
            owners=edges[tuple(sorted((int(u),int(v))))]
            other=next((k+1 for k in owners if k!=i),0)
            if int(triangles['neighbors'][i,j])!=other:raise BridgeError('TIN_NEIGHBOR_EDGE_ORDER')
    hull=MultiPoint(xyz[:,:2]).convex_hull;area=float(np.abs(signed).sum()/2)
    from shapely.ops import unary_union
    coverage=unary_union([Polygon(row) for row in faces]);coverage_area=float(coverage.area)
    if not math.isclose(area,coverage_area,rel_tol=1e-10,abs_tol=1e-10):raise BridgeError('TIN_OVERLAPPING_FACES')
    angles=[]
    for i in range(3):
        u=faces[:,(i+1)%3]-faces[:,i];v=faces[:,(i+2)%3]-faces[:,i]
        cosine=np.sum(u*v,axis=1)/(np.linalg.norm(u,axis=1)*np.linalg.norm(v,axis=1))
        angles.append(np.degrees(np.arccos(np.clip(cosine,-1,1))))
    metrics={'point_count':n,'triangle_count':m,'edge_count':len(edges),'boundary_edges':sum(len(v)==1 for v in edges.values()),
        'bounds':[float(xyz[:,0].min()),float(xyz[:,1].min()),float(xyz[:,0].max()),float(xyz[:,1].max())],
        'height_range':[float(xyz[:,2].min()),float(xyz[:,2].max())],'planar_area':area,'convex_hull_area':float(hull.area),
        'fills_convex_hull':math.isclose(area,hull.area,abs_tol=1e-8,rel_tol=1e-10),
        'minimum_angle_degrees':float(np.min(angles)),'neighbors_match_shared_edges':True,
        'coverage_area':coverage_area,'overlapping_faces':False,'unused_nodes':int(n-len(np.unique(vertices)))}
    return xyz,vertices,edges,metrics


def _read_native(root,path,folder):
    executable,bas=backend_configuration(root);folder.mkdir()
    with path.open('rb') as stream:signature=stream.read(4)
    if path.stat().st_size>64_000_000 or signature!=b'WTIN':raise BridgeError('TIN_FILE_PROFILE: bounded MapGIS WTIN required')
    snapshot=folder/'model.tin';shutil.copyfile(path,snapshot);before=file_sha(path)
    if file_sha(snapshot)!=before:raise BridgeError('STALE_SOURCE')
    with NativePathAlias(folder) as alias:
        host,events=_run_child([str(executable),'terrain','tin.inspect',str(bas.parent/'W60_Tin.dll'),
            str(alias.path/'model.tin'),str(alias.path/'model.bin')],folder,'native-tin-read.json')
    xyz,vertices,edges,metrics=_model(folder/'model.bin')
    event=next((e for e in events if e['event']=='tin'),None)
    if not event or event['points']!=len(xyz) or event['triangles']!=len(vertices):raise BridgeError('TIN_READBACK_COUNTS')
    if file_sha(path)!=before or file_sha(snapshot)!=before:raise BridgeError('STALE_DURING_TIN_READ')
    return xyz,vertices,edges,metrics,host


def _measure(xyz,vertices,frame,base_height):
    if frame is None:
        if base_height is not None:raise BridgeError('TIN_UNIT_METADATA_REQUIRED: bind elevation/XY units before area/volume analysis')
        return {}
    _,scale=_linear_scale(frame['crs'],frame['vertical_unit'])
    if base_height is not None and (type(base_height) not in (int,float) or not math.isfinite(base_height)):raise BridgeError('INVALID_BASE_HEIGHT')
    result=_metrics(xyz[:,:2],xyz[:,2]/scale,SimpleNamespace(simplices=vertices),None if base_height is None else base_height/scale)
    output={'surface_area':result['triangulated_surface_area'],'units':'area in declared XY units squared; volume in XY units cubed',
        'vertical_to_xy_scale':1/scale}
    if base_height is not None:output.update(base_height=base_height,base_height_unit=frame['vertical_unit'],cut_volume=result['cut_volume'],fill_volume=result['fill_volume'])
    return output


def native_tin_info(root,file,expected_sha256,base_height=None):
    bridge=Bridge(root);path=Path(file).resolve(strict=True)
    if file_sha(path)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    frame,sources=_tin_frame(path,expected_sha256)
    folder=bridge.destination_path(bridge.root/'work/native-tin-info'/uuid.uuid4().hex);folder.parent.mkdir(parents=True,exist_ok=True)
    xyz,vertices,edges,metrics,host=_read_native(bridge.root,path,folder)
    metrics.update(_measure(xyz,vertices,frame,base_height))
    if any(file_sha(Path(s['file']))!=s['sha256'] for s in sources):raise BridgeError('STALE_DURING_TIN_READ')
    return {'file':str(path),'sha256':expected_sha256,'backend':'MapGIS W60_Tin fresh native file reader',
        'frame':None if frame is None else {k:frame[k] for k in ('crs','vertical_unit')},'metrics':metrics,
        'native_reopen':host,'source_preserved':True,'scope':'dense active MapGIS WTIN records; absent CRS/height units stay unknown'}


def _samples(payload):
    p=payload['parameters'];template=payload['template'];file=payload['file'];digest=payload['expected_sha256']
    if (set(p)-{'operation','height_field','vertical_unit','constraints'} or p.get('operation')!='native.tin'
            or not {'height_field','vertical_unit'}<=set(p)):raise BridgeError('TIN_PARAMETERS')
    if set(template)!={'name','crs'} or Path(template['name']).name!=template['name'] or Path(template['name']).suffix.lower()!='.tin':raise BridgeError('TIN_TEMPLATE')
    if p['vertical_unit'] not in {'metre','foot','us_survey_foot'}:raise BridgeError('TIN_VERTICAL_UNIT: metre, foot or us_survey_foot required')
    _linear_scale(template['crs'],p['vertical_unit'])
    path,rows,count=_idw_samples(file,digest,p['height_field'],template['crs']);xyz=np.array(rows)
    if len(rows)>100000:raise BridgeError('TIN_POINT_LIMIT: at most 100000 unique points')
    if np.linalg.matrix_rank(xyz[:,:2]-xyz[0,:2])<2:raise BridgeError('COLLINEAR_INPUT')
    requested=p.get('constraints',[])
    if not isinstance(requested,list) or len(requested)>300000:raise BridgeError('TIN_CONSTRAINTS')
    _,reader,_=read_native(path);by_xy={tuple(row[:2]):i for i,row in enumerate(rows)}
    by_id={int(rid):by_xy[(float(g.x),float(g.y))] for rid,g in zip(native_ids(reader),reader.geom)}
    edges=[];seen=set()
    for pair in requested:
        if (not isinstance(pair,list) or len(pair)!=2 or any(type(v) is not int or v not in by_id for v in pair)):
            raise BridgeError('TIN_CONSTRAINT_RECORDS: pairs of existing physical WT record IDs required')
        edge=tuple(sorted(by_id[v] for v in pair))
        if edge[0]==edge[1]:raise BridgeError('TIN_ZERO_LENGTH_CONSTRAINT')
        if edge in seen:raise BridgeError('TIN_DUPLICATE_CONSTRAINT')
        seen.add(edge);edges.append(edge)
    lines=[LineString(xyz[list(e),:2]) for e in edges]
    if lines:
        tree=STRtree(lines)
        for i,line in enumerate(lines):
            for j in tree.query(line,predicate='intersects'):
                if j<=i:continue
                intersection=line.intersection(lines[j])
                if intersection.geom_type!='Point':raise BridgeError('TIN_CONSTRAINT_OVERLAP: use distinct noded edges')
                if tuple(intersection.coords[0]) not in by_xy:raise BridgeError('TIN_CONSTRAINT_INTERSECTION: split crossings at an existing elevation node')
    return path,xyz,edges,count


def _finish(bridge,request_id,stage,payload,execution,events,recovered=False):
    path,expected,constraints,count=_samples(payload);candidate=stage/'native/model.tin'
    saved=next((e for e in events if e['event']=='tin_saved'),None)
    if not saved or saved['sha256']!=file_sha(candidate):raise BridgeError('NATIVE_TIN_SAVE_DIFF')
    xyz,vertices,edges,metrics,fresh=_read_native(bridge.root,candidate,stage/('reopen-'+uuid.uuid4().hex[:12]))
    if execution['pid']==fresh['pid'] or sorted(map(tuple,xyz))!=sorted(map(tuple,expected)) or not metrics['fills_convex_hull']:
        raise BridgeError('NATIVE_TIN_SOURCE_OR_COVERAGE_DIFF')
    native_lines=[LineString(xyz[list(e),:2]) for e in edges]
    from shapely.ops import unary_union
    union=unary_union(native_lines) if constraints else None
    for a,b in constraints:
        if LineString(expected[[a,b],:2]).difference(union).length>1e-8:raise BridgeError('NATIVE_TIN_CONSTRAINT_MISSING')
    metadata={'schema':'mapgis-agent/tin/1','tin_sha256':saved['sha256'],'crs':payload['template']['crs'],
        'vertical_unit':payload['parameters']['vertical_unit'],'algorithm':'CDT 2.0.1 constrained Delaunay; MapGIS W60_Tin native file writer/reader',
        'algorithm_source_commit':'53eff288a4e77ad1c0afe1a10f60bd2b57f877b8',
        'parameters':payload['parameters'],'source_file':str(path),'source_sha256':payload['expected_sha256'],
        'source_points':count,'unique_points':len(expected),'constraint_count':len(constraints),
        'constraint_segments':[expected[list(edge)].tolist() for edge in constraints],
        'coordinate_metadata':'declared CRS/vertical units are hash-bound in this sidecar; native factory coordinate parameters are retained'}
    metrics.update(_measure(xyz,vertices,metadata,None));files=stage/'files';files.mkdir(exist_ok=recovered)
    name=payload['template']['name'];target=files/name
    if not target.exists():shutil.copyfile(candidate,target)
    if file_sha(target)!=saved['sha256']:raise BridgeError('NATIVE_TIN_SAVE_DIFF')
    (files/(name+'.json')).write_text(json_text(metadata),encoding='utf8')
    return finish_bundle(bridge,request_id,files,{'backend':metadata['algorithm'],'metrics':metrics,
        'constraint_count':len(constraints),'constraints_verified_on_native_edges':True,'native_execution':execution,'native_reopen':fresh,
        'source_preserved':True,'replayed':False,'reconciled_after_interruption':recovered})


def prepare_native_tin(bridge,file,expected_sha256,parameters,template,request_id):
    payload={'operation':'terrain.native_tin','file':str(Path(file).resolve(strict=True)),'expected_sha256':expected_sha256,'parameters':parameters,'template':template,'native_tin_protocol':2,'algorithm':'CDT 2.0.1'}
    path,xyz,edges,count=_samples(payload);executable,bas=backend_configuration(bridge.root)
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],template['name'])
    if not new:return receipt
    try:
        producer=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':producer.create_time(),'executable':producer.exe()}),encoding='utf8')
        native=stage/'native';native.mkdir()
        (native/'points.txt').write_text(f'{len(xyz)} {len(edges)}\n'+''.join(' '.join(map(repr,map(float,row)))+'\n' for row in xyz)+
            ''.join(f'{a} {b}\n' for a,b in edges),encoding='ascii')
        with NativePathAlias(native) as alias:
            execution,events=_run_child([str(executable),'terrain','tin',str(bas.parent/'W60_Tin.dll'),
                str(alias.path/'points.txt'),str(alias.path/'model.tin')],native,'native-tin.json')
        return _finish(bridge,request_id,stage,payload,execution,events)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def reconcile_native_tin(bridge,request_id):
    bridge.request_id(request_id)
    with bridge.connect() as db:row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row)
    if receipt.get('artifact_type') not in {'terrain.native_tin','terrain.tin_edit'}:raise BridgeError('NATIVE_TIN_REQUEST_REQUIRED')
    if row['state'] not in {'RUNNING','FAILED'}:return bridge.status(request_id)
    stage=bridge.destination_path(receipt['stage'])
    if (stage/'complete.json').exists():return reconcile_bundle(bridge,request_id)
    producer=json.loads((stage/'producer.json').read_text(encoding='utf8'))
    try:
        process=psutil.Process(producer['pid'])
        if abs(process.create_time()-producer['start'])<1e-5 and process.exe()==producer['executable'] and process.is_running():return receipt|{'producer':'RUNNING','replayed':False}
    except psutil.NoSuchProcess:pass
    except psutil.AccessDenied as exc:raise BridgeError('NATIVE_PRODUCER_IDENTITY_UNAVAILABLE') from exc
    result=completed_native_child(stage/'native','native-tin.json')
    if result is None:return receipt|{'native_child':'RUNNING','replayed':False}
    cleanup_aliases(stage/'native');execution,events=result
    try:
        if receipt['artifact_type']=='terrain.tin_edit':
            from .tin_edit import finish_tin_edit
            return finish_tin_edit(bridge,request_id,stage,receipt['payload'],execution,events,True)
        return _finish(bridge,request_id,stage,receipt['payload'],execution,events,True)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def prepare_tin_vectors(bridge,file,expected_sha256,part,template,request_id):
    suffix={'nodes':'.wt','faces':'.wp','edges':'.wl'}.get(part)
    name=template.get('name','')
    if not suffix or Path(name).name!=name or Path(name).suffix.lower()!=suffix:raise BridgeError('TIN_VECTOR_TEMPLATE: nodes WT, faces WP or edges WL required')
    path=Path(file).resolve(strict=True)
    if file_sha(path)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    frame,sources=_tin_frame(path,expected_sha256);target_crs=explicit_crs(template.get('crs',{}))
    if frame is not None:
        original=explicit_crs(frame['crs'])
        if (original is None)!=(target_crs is None) or original is not None and not original.equals(target_crs) or original is None and frame['crs']!=template['crs']:
            raise BridgeError('TIN_VECTOR_CRS_MISMATCH')
    payload={'operation':'terrain.tin_vectors','file':str(path),'expected_sha256':expected_sha256,'part':part,'template':template}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,name)
    if not new:return receipt
    try:
        xyz,vertices,edges,metrics,host=_read_native(bridge.root,path,stage/'read')
        features=[]
        def append(geometry,properties):
            features.append({'type':'Feature','id':str(len(features)+1),'geometry':mapping(geometry),'properties':properties})
        if part=='nodes':
            for i,row in enumerate(xyz,1):append(Point(row[:2]),{'ID':i,'Z':float(row[2])})
        elif part=='faces':
            for i,row in enumerate(vertices,1):
                coordinates=xyz[row];props={'ID':i,'N1':int(row[0]+1),'N2':int(row[1]+1),'N3':int(row[2]+1),
                    'Z1':float(coordinates[0,2]),'Z2':float(coordinates[1,2]),'Z3':float(coordinates[2,2]),'Z_mean':float(coordinates[:,2].mean())}
                append(Polygon(coordinates[:,:2]),props)
        else:
            constraints=[LineString(np.array(segment)[:,:2]) for segment in (frame or {}).get('constraint_segments',[])]
            tree=STRtree(constraints) if constraints else None
            for i,(edge,owners) in enumerate(sorted(edges.items()),1):
                coordinates=xyz[list(edge)];line=LineString(coordinates[:,:2])
                constrained=bool(tree is not None and any(constraints[int(j)].covers(line) for j in tree.query(line,predicate='intersects')))
                append(line,{'ID':i,'N1':edge[0]+1,'N2':edge[1]+1,'Z1':float(coordinates[0,2]),'Z2':float(coordinates[1,2]),
                    'boundary':int(len(owners)==1),'constraint':int(constrained)})
        from .creation import prepare_creation
        encoder=Bridge(stage/'encoder');prepare_creation(encoder,{'type':'FeatureCollection','features':features},template,'encode')
        encoded=encoder.apply('encode');files=stage/'files';files.mkdir();shutil.copyfile(encoded['output'],files/name)
        qa={'features':len(features),'part':part,'model':metrics,'geometry_dimension':'2D native vector; original elevations retained as attributes'}
        (files/(name+'.json')).write_text(json_text({'schema':'mapgis-agent/tin-vectors/1','native_sha256':file_sha(files/name),
            'sources':sources,'frame':None if frame is None else {k:frame[k] for k in ('crs','vertical_unit')},'metrics':qa}),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'fresh MapGIS native TIN readback plus existing native vector encoder',
            'metrics':qa,'native_reopen':host,'source_preserved':True})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
