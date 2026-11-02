"""Profiles use saved triangles, including exchanged topology and outside gaps."""
import json,math
from pathlib import Path
import numpy as np
import pytest
from mapgis_agent.core import BridgeError,read_native
from test_tin_edit import model

def profile(b,source,parameters,identity):
    from mapgis_agent.tin_profile import prepare_tin_profile
    return prepare_tin_profile(b,source['output'],source['candidate_sha256'],parameters,{'name':identity+'.wl','map_scale':1000},identity)

def test_profile_plane_crossings_and_units_are_read_from_actual_native_triangles(tmp_path):
    b,source=model(tmp_path)
    prepared=profile(b,source,{'path':[[0.,4.],[10.,4.]],'spacing':3.,'outside':'error','vertical_exaggeration':2.},'section')
    result=b.apply('section');folder=Path(result['output']).parent
    rows=json.loads((folder/'profile.json').read_text(encoding='utf8'))['samples']
    distances=[r['distance'] for r in rows]
    assert 0 in distances and 10 in distances and 3 in distances and 6 in distances and 9 in distances
    for row in rows:
        assert row['z']==pytest.approx(100+.1*row['x']+.2*row['y'])
        assert row['triangle_id']>0
    reader=read_native(Path(result['output']))[1]
    assert len(reader.geom)==1
    actual=np.array(reader.geom[0].coords)
    np.testing.assert_allclose(actual[:,0],distances)
    np.testing.assert_allclose(actual[:,1],[r['z']*2 for r in rows])
    assert prepared['frame']['unit']=='metre' and prepared['metrics']['length']==10

def test_profile_keeps_explicit_outside_gap_and_source_metadata_guard(tmp_path):
    b,source=model(tmp_path)
    with pytest.raises(BridgeError,match='TIN_PROFILE_OUTSIDE'):
        profile(b,source,{'path':[[-2,4],[12,4]],'spacing':1,'outside':'error'},'outside')
    profile(b,source,{'path':[[-2,4],[12,4]],'spacing':1,'outside':'gap'},'gaps')
    result=b.apply('gaps');rows=json.loads((Path(result['output']).parent/'profile.json').read_text(encoding='utf8'))['samples']
    assert rows[0]['z'] is None and rows[-1]['z'] is None
    assert next(r for r in rows if r['x']==0)['z']==pytest.approx(100.8)
    profile(b,source,{'path':[[0,4],[10,4]],'spacing':1,'outside':'error'},'stale-profile')
    Path(source['output']+'.json').write_text('{}',encoding='utf8')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('stale-profile')

def test_profile_obeys_explicit_exchanged_native_topology_instead_of_retriangulating(tmp_path):
    from mapgis_agent.tin_edit import prepare_tin_edit
    from mapgis_agent.native_tin import _read_native
    b,source=model(tmp_path)
    prepare_tin_edit(b,source['output'],source['candidate_sha256'],'node.height',{'updates':[{'node_id':2,'z':150.}]},{'name':'ridge.tin'},'ridge')
    source=b.apply('ridge')
    xyz,faces,edges,*_=_read_native(tmp_path,Path(source['output']),tmp_path/'triangles')
    interior=next(e for e,owners in edges.items() if len(owners)==2)
    prepare_tin_edit(b,source['output'],source['candidate_sha256'],'edge.flip',{'nodes':[int(i+1) for i in interior]},{'name':'flipped.tin'},'flipped')
    changed=b.apply('flipped')
    values=[]
    for item,identity in [(source,'before-profile'),(changed,'after-profile')]:
        profile(b,item,{'path':[[0,4],[10,4]],'spacing':.5,'outside':'error'},identity)
        result=b.apply(identity);rows=json.loads((Path(result['output']).parent/'profile.json').read_text(encoding='utf8'))['samples']
        values.append(next(r['z'] for r in rows if math.isclose(r['distance'],5)))
    assert abs(values[0]-values[1])>10


def test_profile_converts_horizontal_millimetres_and_vertical_metres_explicitly(tmp_path):
    b,source=model(tmp_path);metadata=Path(source['output']+'.json')
    frame=json.loads(metadata.read_text(encoding='utf8'));frame['crs']['unit']='millimetre'
    metadata.write_text(json.dumps(frame),encoding='utf8')
    prepared=profile(b,source,{'path':[[0,4],[10,4]],'spacing':2},'mm-section')
    result=b.apply('mm-section');reader=read_native(Path(result['output']))[1]
    coordinates=np.array(reader.geom[0].coords)
    assert coordinates[-1,0]==pytest.approx(.01)
    assert coordinates[0,1]==pytest.approx(100.8)
    assert prepared['metrics']['length']==10 and prepared['metrics']['length_metres']==pytest.approx(.01)
