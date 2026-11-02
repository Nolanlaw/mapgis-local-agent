"""Spline editing through the real native path, with analytic readback checks."""
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import LineString

from mapgis_agent.core import BridgeError, read_native, sha
from mapgis_agent.native_backend import prepare_native_operation
from mapgis_agent.topology import read_wp_arcs
from mapgis_agent.workspace import NativeWorkspaces
from test_native_line_editing import native_case, create, LOCAL
from test_native_wp import wp_case, enable_native
from test_native_subset_auxiliary import populated_auxiliary


@pytest.mark.parametrize('projected', [False, True])
def test_native_smooth_preserves_ends_attributes_neighbour_and_analytic_curve(native_case, projected):
    bridge = native_case
    origin, factor = (500000., 100.) if projected else (0., 1.)
    points = [(origin, 0.), (origin+factor, factor), (origin+2*factor, 0.)]
    neighbour = LineString([(origin+3*factor, 0.), (origin+4*factor, 0.)])
    source = create(bridge, 'smooth.wl', LineString(points),
        {'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs', 'axis_order':'xy'} if projected else LOCAL,
        50000 if projected else 1000, neighbour)
    before = source.read_bytes()
    prepared = prepare_native_operation(bridge, 'line.smooth', str(source), sha(before),
        {'record_ids':[1], 'max_chord_error':factor*0.001}, 'smooth')
    output = Path(bridge.apply('smooth')['output'])
    old, actual = read_native(source)[1], read_native(output)[1]
    vertices = np.asarray(actual.geom[0].coords)
    assert len(vertices)>len(points)
    assert tuple(vertices[0])==points[0] and tuple(vertices[-1])==points[-1]
    x = (vertices[:,0]-origin)/factor
    left = np.where(x<=1., x, 2.-x)
    # Natural cubic through (0,0),(1,1),(2,0), equal chord lengths.
    assert np.max(np.abs(vertices[:,1]/factor-(1.5*left-0.5*left**3)))<2e-10
    dense_x = np.linspace(0.,2.,4001)
    left = np.minimum(dense_x,2.-dense_x)
    dense_y = 1.5*left-0.5*left**3
    chord_y = np.interp(dense_x,x,vertices[:,1]/factor)
    assert np.max(np.abs(dense_y-chord_y))<=0.001
    assert old.geom[1].equals_exact(actual.geom[1],0.)
    assert old.data.equals(actual.data) and old.line_params.equals(actual.line_params)
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    assert prepared['parameters']['smoothing']['algorithm']=='scipy-natural-periodic-cubic-v1'
    assert source.read_bytes()==before
    bridge.recover('smooth',str(output),sha(output.read_bytes()),'undo')
    assert Path(bridge.apply('undo')['output']).read_bytes()==before


def test_closed_periodic_curve_interpolates_square_and_exact_duplicate_vertices(native_case):
    source = create(native_case,'closed.wl',LineString([(0,0),(1,0),(1,0),(1,1),(0,1),(0,0)]),LOCAL,1000)
    prepared = prepare_native_operation(native_case,'line.smooth',str(source),sha(source.read_bytes()),
        {'record_ids':[1],'max_chord_error':.01},'closed')
    output = Path(native_case.apply('closed')['output'])
    vertices = np.asarray(read_native(output)[1].geom[0].coords)
    assert np.array_equal(vertices[0],vertices[-1])
    assert any(np.allclose(point,[.5,-.1875],atol=1e-12,rtol=0) for point in vertices)
    assert prepared['parameters']['smoothing']['curves'][0]['boundary']=='periodic'
    assert prepared['parameters']['smoothing']['curves'][0]['chord_error_bound']<=.01


def seed_shared(bridge,source):
    report = read_wp_arcs(str(source),sha(source.read_bytes()),limit=100)
    shared = next(a for a in report['arcs'] if a['owner_region_ids']==[1,2])
    prepare_native_operation(bridge,'arc.replace',str(source),sha(source.read_bytes()),
        {'updates':[{'arc_id':shared['arc_id'],'coordinates':[shared['coordinates'][0],[12,5],shared['coordinates'][-1]]}]},'seed')
    return Path(bridge.apply('seed')['output']), shared['arc_id']


def test_wp_shared_and_closed_arc_smoothing_retains_owners_nodes_hole_and_auxiliary_values(wp_case):
    bridge,source = wp_case
    enable_native(bridge)
    populated_auxiliary(source)
    source, shared_id = seed_shared(bridge,source)
    before = source.read_bytes()
    old = read_wp_arcs(str(source),sha(before),limit=100)
    closed = next(a for a in old['arcs'] if a['owner_region_ids']==[3] and len(a['coordinates'])>=5)
    selected = [shared_id,closed['arc_id']]
    prepared = prepare_native_operation(bridge,'arc.smooth',str(source),sha(before),
        {'arc_ids':selected,'max_chord_error':.005},'smooth-arcs')
    output = Path(bridge.apply('smooth-arcs')['output'])
    new = read_wp_arcs(str(output),sha(output.read_bytes()),limit=100)
    assert old['nodes']==new['nodes'] and old['attribute_tables']==new['attribute_tables']
    for a,b in zip(old['arcs'],new['arcs']):
        for key in ('arc_id','node_ids','owner_region_ids','style','attributes'):assert a[key]==b[key]
        if a['arc_id'] in selected:
            assert b['coordinates'][0]==a['coordinates'][0] and b['coordinates'][-1]==a['coordinates'][-1]
            assert len(b['coordinates'])>len(a['coordinates'])
        else:assert a['coordinates']==b['coordinates']
    old_reader,new_reader=read_native(source)[1],read_native(output)[1]
    assert old_reader.data.equals(new_reader.data)
    assert sum(len(poly.interiors) for g in new_reader.geom for poly in (g.geoms if g.geom_type=='MultiPolygon' else [g]))==1
    assert sum(g.area for g in new_reader.geom[:2])==pytest.approx(200.)
    assert prepared['comparison']['native_topology']['candidate']['rings']==5
    assert all(v['records_byte_identical'] for v in prepared['comparison']['auxiliary_attributes'].values())
    assert source.read_bytes()==before


@pytest.mark.parametrize('kind',['line','arc'])
def test_unsaved_smoothing_survives_native_child_loss_and_saves_without_reinterpolation(wp_case,kind):
    bridge,source=wp_case
    enable_native(bridge)
    if kind=='line':
        # Use another request namespace because wp_case already used create.
        from mapgis_agent.creation import prepare_creation
        prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1',
            'geometry':{'type':'LineString','coordinates':[[0,0],[1,1],[2,0]]},'properties':{}}]},
            {'name':'buffer.wl','map_scale':1000,'crs':LOCAL},'create-line')
        source=Path(bridge.apply('create-line')['output']);ids={'record_ids':[1]}
    else:
        source,aid=seed_shared(bridge,source);ids={'arc_ids':[aid]}
    before=source.read_bytes();registry=NativeWorkspaces(bridge)
    try:
        opened=registry.execute('open','smooth',{'source':str(source),'expected_sha256':sha(before)})
        dirty=registry.execute('edit','smooth',{'operation':kind+'.smooth','parameters':{**ids,'max_chord_error':.005}},0,'edit')
        assert dirty['dirty'] and dirty['native_pid']==opened['native_pid']
        checkpoint=Path(dirty['checkpoint']).read_bytes()
        child=registry.buffers['smooth'].process;child.kill();child.wait(timeout=5)
        restored=registry.execute('recover','smooth',{},1)
        assert restored['dirty'] and restored['native_pid']!=opened['native_pid']
        assert Path(restored['checkpoint']).read_bytes()==checkpoint
        saved=registry.execute('save','smooth',{},1,'save')
        assert not saved['dirty'] and source.read_bytes()==before
        registry.execute('close','smooth',{},1)
    finally:registry.close_all()


@pytest.mark.parametrize('parameters',[
    {'record_ids':[1],'max_chord_error':0.},
    {'record_ids':[1],'max_chord_error':float('nan')},
    {'record_ids':[True],'max_chord_error':.01},
    {'record_ids':[1,1],'max_chord_error':.01},
    {'record_ids':[999],'max_chord_error':.01},
    {'record_ids':[1],'max_chord_error':1e-99},
])
def test_invalid_smoothing_is_rejected_before_native_launch(native_case,parameters,monkeypatch):
    import mapgis_agent.native_backend as backend
    source=create(native_case,'reject.wl',LineString([(0,0),(1,1),(2,0)]),LOCAL,1000)
    calls=[];monkeypatch.setattr(backend,'_invoke',lambda *a,**k:calls.append(a))
    with pytest.raises(BridgeError,match='SMOOTH_'):
        prepare_native_operation(native_case,'line.smooth',str(source),sha(source.read_bytes()),parameters,'bad')
    assert not calls
