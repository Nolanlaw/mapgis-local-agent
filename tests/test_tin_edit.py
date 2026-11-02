"""Saved TIN edits must preserve elevation/topology contracts in real native files."""
import json
from pathlib import Path
import numpy as np
import pytest
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_tin import prepare_native_tin,_read_native,native_tin_info
from test_native_raster import native_settings
from test_grid_analysis import FRAME

def model(root,constraint=False):
    b=Bridge(root);native_settings(root)
    xyz=[(0,0,100),(10,0,101),(10,8,102.6),(0,10,102)]
    features=[{'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':[x,y]},'properties':{'ID':i,'Z':z}} for i,(x,y,z) in enumerate(xyz,1)]
    prepare_creation(b,{'type':'FeatureCollection','features':features},{'name':'source.wt','crs':FRAME,'map_scale':1000},'points')
    source=b.apply('points')
    parameters={'operation':'native.tin','height_field':'Z','vertical_unit':'metre'}
    if constraint:parameters['constraints']=[[1,3]]
    prepare_native_tin(b,source['output'],source['candidate_sha256'],parameters,{'name':'source.tin','crs':FRAME},'model')
    return b,b.apply('model')

def edit(b,source,operation,parameters,identity):
    from mapgis_agent.tin_edit import prepare_tin_edit
    return prepare_tin_edit(b,source['output'],source['candidate_sha256'],operation,parameters,{'name':identity+'.tin'},identity)

def test_height_edit_preserves_native_xy_connectivity_and_source(tmp_path):
    b,source=model(tmp_path);before=Path(source['output']).read_bytes()
    xyz,faces,*_= _read_native(tmp_path,Path(source['output']),tmp_path/'before')
    prepared=edit(b,source,'node.height',{'updates':[{'node_id':2,'z':125.}]},'higher')
    result=b.apply('higher');new_xyz,new_faces,*_=_read_native(tmp_path,Path(result['output']),tmp_path/'after')
    np.testing.assert_array_equal(new_xyz[:,:2],xyz[:,:2]);np.testing.assert_array_equal(new_faces,faces)
    assert new_xyz[1,2]==125 and np.array_equal(new_xyz[[0,2,3],2],xyz[[0,2,3],2])
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    assert Path(source['output']).read_bytes()==before

def test_explicit_edge_exchange_changes_only_two_faces_and_rejects_constraint(tmp_path):
    b,source=model(tmp_path);xyz,faces,edges,*_=_read_native(tmp_path,Path(source['output']),tmp_path/'original')
    edge=next(e for e,owners in edges.items() if len(owners)==2)
    edit(b,source,'edge.flip',{'nodes':[int(v+1) for v in edge]},'flipped');result=b.apply('flipped')
    changed,new_faces,new_edges,*_=_read_native(tmp_path,Path(result['output']),tmp_path/'flipped')
    np.testing.assert_array_equal(changed,xyz);assert edge not in new_edges
    assert len(new_faces)==len(faces)==2
    constrained_root=tmp_path/'constraint';cb,cs=model(constrained_root,True)
    with pytest.raises(BridgeError,match='TIN_CONSTRAINED_EDGE'):
        edit(cb,cs,'edge.flip',{'nodes':[1,3]},'forbidden')

def test_insert_move_delete_retriangulates_with_stable_source_mapping(tmp_path):
    b,source=model(tmp_path)
    edit(b,source,'node.insert',{'points':[[4.,4.,108.]]},'inserted');inserted=b.apply('inserted')
    info=native_tin_info(tmp_path,inserted['output'],inserted['candidate_sha256'])
    assert info['metrics']['point_count']==5 and info['metrics']['triangle_count']==4
    edit(b,inserted,'node.move',{'updates':[{'node_id':5,'x':5.,'y':4.}]},'moved');moved=b.apply('moved')
    xyz,*_=_read_native(tmp_path,Path(moved['output']),tmp_path/'moved-read')
    assert tuple(xyz[4])==(5.,4.,108.)
    edit(b,moved,'node.delete',{'node_ids':[5]},'deleted');deleted=b.apply('deleted')
    xyz,*_=_read_native(tmp_path,Path(deleted['output']),tmp_path/'deleted-read')
    assert len(xyz)==4
    meta=json.loads(Path(deleted['output']+'.json').read_text(encoding='utf8'))
    assert meta['edit']['node_mapping']=={'1':1,'2':2,'3':3,'4':4,'5':None}

def test_tin_edit_constraints_and_publication_source_guard(tmp_path):
    b,source=model(tmp_path)
    edit(b,source,'constraints.replace',{'edges':[[1,3]]},'breakline');result=b.apply('breakline')
    meta=json.loads(Path(result['output']+'.json').read_text(encoding='utf8'))
    assert meta['constraint_count']==1
    with pytest.raises(BridgeError,match='TIN_CONSTRAINT_NODE_DELETE'):
        edit(b,result,'node.delete',{'node_ids':[1]},'delete-endpoint')
    edit(b,result,'node.height',{'updates':[{'node_id':2,'z':120}]},'stale')
    Path(result['output']+'.json').write_text('{}',encoding='utf8')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('stale')


def test_native_model_quality_rejects_overlapping_nonduplicate_faces(tmp_path):
    from mapgis_agent.tin_edit import _transport
    xyz=np.array([[0.,0.,0.],[10,0,0],[10,10,0],[0,10,0],[5,3,0]])
    with pytest.raises(BridgeError,match='TIN_OVERLAPPING_FACES'):
        _transport(tmp_path/'overlap.bin',xyz,np.array([[0,1,2],[0,1,3]]))


def test_public_tin_tools_publish_strict_nested_schemas_and_action_semantics(tmp_path):
    import asyncio
    from pydantic import ValidationError
    from mapgis_agent.server import build_server
    from mapgis_agent.tin_edit import TinEditParameters
    from mapgis_agent.tin_profile import ProfileParameters
    async def inspect():
        tools={t.name:t for t in await build_server(tmp_path).list_tools()}
        for name,definition in [('mapgis_prepare_tin_edit','TinEditParameters'),('mapgis_prepare_tin_profile','ProfileParameters')]:
            t=tools[name]
            assert t.annotations.idempotentHint and not t.annotations.destructiveHint
            assert t.inputSchema['$defs'][definition]['additionalProperties'] is False
    asyncio.run(inspect())
    for value in [{'nodes':[True,2]},{'updates':[{'node_id':1,'z':float('inf')}]},{'extra':1}]:
        with pytest.raises(ValidationError):TinEditParameters.model_validate(value)
    with pytest.raises(ValidationError):ProfileParameters.model_validate({'path':[[0,0],[1,1]],'spacing':0})
    with pytest.raises(ValidationError):ProfileParameters.model_validate({'path':[[0,0],[1,1]],'spacing':1,'outside':'guess'})
