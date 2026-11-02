"""Explicit edits on a bound dense native TIN, saved to a new native branch."""
from __future__ import annotations

import json,math,os,shutil,struct,uuid
from pathlib import Path
import numpy as np
import psutil
from pydantic import BaseModel,ConfigDict,StrictFloat,StrictInt
from shapely.geometry import LineString,MultiPoint,Polygon
from shapely.ops import unary_union

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import BridgeError,json_text
from .native_backend import backend_configuration,_run_child
from .native_paths import NativePathAlias
from .native_tin import POINT,TRIANGLE,_tin_frame,_read_native,_model,_measure
from .raster import file_sha


class TinNodeUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    node_id:StrictInt
    x:StrictFloat|None=None
    y:StrictFloat|None=None
    z:StrictFloat|None=None


class TinEditParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    updates:list[TinNodeUpdate]|None=None
    points:list[list[StrictFloat]]|None=None
    node_ids:list[StrictInt]|None=None
    nodes:list[StrictInt]|None=None
    edges:list[list[StrictInt]]|None=None


def _ids(values,n,label):
    if not isinstance(values,list) or not values or any(type(v) is not int or not 1<=v<=n for v in values) or len(set(values))!=len(values):
        raise BridgeError(label+': unique existing one-based node IDs required')
    return [v-1 for v in values]


def _constraints(frame,xyz):
    by_xy={tuple(row[:2]):i for i,row in enumerate(xyz)};result=[]
    for segment in frame.get('constraint_segments',[]):
        if len(segment)!=2 or any(tuple(p[:2]) not in by_xy for p in segment):raise BridgeError('TIN_CONSTRAINT_METADATA')
        result.append(tuple(by_xy[tuple(p[:2])] for p in segment))
    return result


def _change(xyz,faces,edges,frame,operation,parameters):
    p=parameters;n=len(xyz);out=xyz.copy();triangles=faces.copy();constraints=_constraints(frame,xyz)
    mapping={str(i+1):i+1 for i in range(n)};rebuild=False
    if operation in {'node.height','node.move'}:
        fields={'node_id','z'} if operation=='node.height' else {'node_id','x','y'}
        if set(p)!={'updates'} or not isinstance(p['updates'],list) or not p['updates'] or any(not isinstance(v,dict) or set(v)!=fields for v in p['updates']):raise BridgeError('TIN_NODE_UPDATES')
        ids=_ids([u['node_id'] for u in p['updates']],n,'TIN_NODE_UPDATES')
        for index,update in zip(ids,p['updates']):
            for key in fields-{'node_id'}:
                v=update[key]
                if type(v) not in (int,float) or not math.isfinite(v):raise BridgeError('TIN_NODE_VALUE')
                out[index,{'x':0,'y':1,'z':2}[key]]=v
        rebuild=operation=='node.move'
    elif operation=='node.insert':
        if set(p)!={'points'} or not isinstance(p['points'],list) or not p['points'] or n+len(p['points'])>100000:raise BridgeError('TIN_INSERT_POINTS')
        if any(not isinstance(row,list) or len(row)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) for v in row) for row in p['points']):raise BridgeError('TIN_INSERT_XYZ')
        out=np.vstack([out,np.array(p['points'],dtype=float)]);rebuild=True
    elif operation=='node.delete':
        if set(p)!={'node_ids'}:raise BridgeError('TIN_DELETE_PARAMETERS')
        remove=set(_ids(p['node_ids'],n,'TIN_DELETE_NODES'))
        if n-len(remove)<3:raise BridgeError('TIN_AT_LEAST_THREE_POINTS')
        if any(remove.intersection(edge) for edge in constraints):raise BridgeError('TIN_CONSTRAINT_NODE_DELETE: replace constraints before deleting an endpoint')
        kept=[i for i in range(n) if i not in remove];new={old:i for i,old in enumerate(kept)}
        out=out[kept];constraints=[tuple(new[v] for v in edge) for edge in constraints]
        mapping={str(i+1):None if i in remove else new[i]+1 for i in range(n)};rebuild=True
    elif operation=='constraints.replace':
        if set(p)!={'edges'} or not isinstance(p['edges'],list) or len(p['edges'])>300000 or any(not isinstance(pair,list) or len(pair)!=2 for pair in p['edges']):raise BridgeError('TIN_EDIT_CONSTRAINTS')
        constraints=[tuple(_ids(pair,n,'TIN_EDIT_CONSTRAINTS')) for pair in p['edges']];rebuild=True
    elif operation=='edge.flip':
        if set(p)!={'nodes'}:raise BridgeError('TIN_EDGE_PARAMETERS')
        edge=tuple(sorted(_ids(p['nodes'],n,'TIN_EDGE_NODES')))
        if len(edge)!=2 or edge not in edges or len(edges[edge])!=2:raise BridgeError('TIN_INTERIOR_EDGE_REQUIRED')
        line=LineString(xyz[list(edge),:2])
        if any(LineString(xyz[list(c),:2]).covers(line) for c in constraints):raise BridgeError('TIN_CONSTRAINED_EDGE: remove its constraint explicitly before exchange')
        a,b=edges[edge];opposite=[next(int(v) for v in faces[i] if v not in edge) for i in [a,b]]
        if tuple(sorted(opposite)) in edges:raise BridgeError('TIN_EDGE_ALREADY_PRESENT')
        quad=MultiPoint(xyz[list(edge)+opposite,:2]).convex_hull
        old=unary_union([Polygon(xyz[faces[i],:2]) for i in [a,b]])
        if not math.isclose(quad.area,old.area,rel_tol=1e-12,abs_tol=1e-10) or len(quad.exterior.coords)!=5:raise BridgeError('TIN_NONCONVEX_EDGE: exchange would leave the original two-face domain')
        for index,row in zip([a,b],[[opposite[0],opposite[1],edge[0]],[opposite[1],opposite[0],edge[1]]]):
            coordinates=xyz[row,:2];u=coordinates[1]-coordinates[0];v=coordinates[2]-coordinates[0]
            if u[0]*v[1]-u[1]*v[0]<0:row=[row[0],row[2],row[1]]
            triangles[index]=row
    else:raise BridgeError('TIN_EDIT_OPERATION')
    if len(set(map(tuple,out[:,:2])))!=len(out):raise BridgeError('TIN_DUPLICATE_VERTICES')
    if np.linalg.matrix_rank(out[:,:2]-out[0,:2])<2:raise BridgeError('COLLINEAR_INPUT')
    if np.any(np.abs(out[:,2])>=3.4e18):raise BridgeError('TIN_NODE_VALUE')
    normalized=[tuple(sorted(e)) for e in constraints]
    if len(set(normalized))!=len(normalized):raise BridgeError('TIN_DUPLICATE_CONSTRAINT')
    lines=[LineString(out[list(e),:2]) for e in constraints];by_xy=set(map(tuple,out[:,:2]))
    from shapely import STRtree
    tree=STRtree(lines) if lines else None
    for i,line in enumerate(lines):
        for j in tree.query(line,predicate='intersects'):
            if j<=i:continue
            intersection=line.intersection(lines[int(j)])
            if intersection.geom_type!='Point':raise BridgeError('TIN_CONSTRAINT_OVERLAP')
            if tuple(intersection.coords[0]) not in by_xy:raise BridgeError('TIN_CONSTRAINT_INTERSECTION: split crossings at an existing elevation node')
    return out,triangles,constraints,mapping,rebuild


def _transport(path,xyz,faces):
    points=np.zeros(len(xyz)+1,dtype=POINT);triangles=np.zeros(len(faces)+1,dtype=TRIANGLE)
    for i,key in enumerate(['x','y','z']):points[key][1:]=xyz[:,i]
    points['state'][1:]=1;triangles['state'][1:]=1;triangles['vertices'][1:]=faces+1
    owners={}
    for i,row in enumerate(faces):
        for j,(u,v) in enumerate(zip(row,np.roll(row,-1))):owners.setdefault(tuple(sorted((int(u),int(v)))),[]).append((i,j))
    for values in owners.values():
        if len(values)>2:raise BridgeError('TIN_NONMANIFOLD_EDGE')
        if len(values)==2:
            (i,a),(j,b)=values;triangles['neighbors'][i+1,a]=j+1;triangles['neighbors'][j+1,b]=i+1
    path.write_bytes(struct.pack('<8s2I',b'MGNT0001',len(xyz),len(faces))+points.tobytes()+triangles.tobytes())
    _model(path)


def _expected(bridge,stage,payload):
    path=Path(payload['file']);frame,sources=_tin_frame(path,payload['expected_sha256'])
    if frame is None or frame.get('algorithm_source_commit')!='53eff288a4e77ad1c0afe1a10f60bd2b57f877b8':
        raise BridgeError('TIN_EDIT_BOUND_MODEL_REQUIRED: this version edits bound CDT native TIN models; import coordinate/unit metadata for other profiles first')
    xyz,faces,edges,metrics,host=_read_native(bridge.root,path,stage/('source-'+uuid.uuid4().hex[:8]))
    if not metrics['fills_convex_hull']:raise BridgeError('TIN_EDIT_FULL_HULL_REQUIRED')
    changed,triangles,constraints,mapping,rebuild=_change(xyz,faces,edges,frame,payload['edit_operation'],payload['parameters'])
    return changed,triangles,constraints,mapping,rebuild,frame,sources


def finish_tin_edit(bridge,request_id,stage,payload,execution,events,recovered=False,expected=None):
    xyz,faces,constraints,mapping,rebuild,frame,sources=expected or _expected(bridge,stage,payload)
    candidate=stage/'native/model.tin';saved=next((e for e in events if e['event']=='tin_saved'),None)
    if not saved or file_sha(candidate)!=saved['sha256']:raise BridgeError('NATIVE_TIN_SAVE_DIFF')
    actual,new_faces,edges,metrics,fresh=_read_native(bridge.root,candidate,stage/('reopen-'+uuid.uuid4().hex[:8]))
    if not np.array_equal(actual,xyz) or not metrics['fills_convex_hull']:raise BridgeError('TIN_EDIT_READBACK_DIFF')
    if not rebuild and not np.array_equal(new_faces,faces):raise BridgeError('TIN_EDIT_CONNECTIVITY_DIFF')
    lines=unary_union([LineString(actual[list(e),:2]) for e in edges]) if constraints else None
    if any(LineString(actual[list(e),:2]).difference(lines).length>1e-8 for e in constraints):raise BridgeError('NATIVE_TIN_CONSTRAINT_MISSING')
    for s in sources:
        if file_sha(Path(s['file']))!=s['sha256']:raise BridgeError('STALE_SOURCE')
    metadata={**frame,'tin_sha256':saved['sha256'],'source_file':payload['file'],'source_sha256':payload['expected_sha256'],
        'algorithm':('CDT 2.0.1 constrained Delaunay; MapGIS native storage' if rebuild else 'Saved native topology with explicit '+payload['edit_operation']+'; MapGIS native storage'),
        'unique_points':len(xyz),'constraint_count':len(constraints),'constraint_segments':[actual[list(e)].tolist() for e in constraints],
        'edit':{'operation':payload['edit_operation'],'parameters':payload['parameters'],'node_mapping':mapping,'connectivity_rebuilt':rebuild,
            'face_identity':'reassigned after retriangulation' if rebuild else 'original one-based face slots preserved'}}
    metrics.update(_measure(actual,new_faces,metadata,None));files=stage/'files';files.mkdir(exist_ok=recovered)
    target=files/payload['template']['name']
    if not target.exists():shutil.copyfile(candidate,target)
    if file_sha(target)!=saved['sha256']:raise BridgeError('NATIVE_TIN_SAVE_DIFF')
    (files/(target.name+'.json')).write_text(json_text(metadata),encoding='utf8')
    return finish_bundle(bridge,request_id,files,{'backend':'CDT retriangulation or explicit topology edit; MapGIS native TIN save/readback',
        'metrics':metrics,'edit':metadata['edit'],'native_execution':execution,'native_reopen':fresh,'source_preserved':True,
        'constraints_verified_on_native_edges':True,'replayed':False,'reconciled_after_interruption':recovered})


def prepare_tin_edit(bridge,file,expected_sha256,operation,parameters,template,request_id):
    model=parameters if isinstance(parameters,TinEditParameters) else TinEditParameters.model_validate(parameters)
    parameters=model.model_dump(exclude_none=True)
    path=Path(file).resolve(strict=True)
    if file_sha(path)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if set(template)!={'name'} or Path(template['name']).name!=template['name'] or Path(template['name']).suffix.lower()!='.tin':raise BridgeError('TIN_EDIT_TEMPLATE')
    frame,sources=_tin_frame(path,expected_sha256)
    payload={'operation':'terrain.tin_edit','file':str(path),'expected_sha256':expected_sha256,'edit_operation':operation,
        'parameters':parameters,'template':template,'tin_edit_protocol':1}
    executable,bas=backend_configuration(bridge.root)
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,template['name'])
    if not new:return receipt
    try:
        expected=_expected(bridge,stage,payload);xyz,faces,constraints,mapping,rebuild,*_=expected
        process=psutil.Process();(stage/'producer.json').write_text(json_text({'pid':os.getpid(),'start':process.create_time(),'executable':process.exe()}),encoding='utf8')
        native=stage/'native';native.mkdir()
        if rebuild:
            control=native/'points.txt';mode='tin'
            control.write_text(f'{len(xyz)} {len(constraints)}\n'+''.join(' '.join(map(repr,map(float,row)))+'\n' for row in xyz)+
                ''.join(f'{a} {b}\n' for a,b in constraints),encoding='ascii')
        else:control=native/'model.bin';mode='tin.write';_transport(control,xyz,faces)
        with NativePathAlias(native) as alias:
            execution,events=_run_child([str(executable),'terrain',mode,str(bas.parent/'W60_Tin.dll'),
                str(alias.path/control.name),str(alias.path/'model.tin')],native,'native-tin.json')
        return finish_tin_edit(bridge,request_id,stage,payload,execution,events,expected=expected)
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
