from pathlib import Path

import pytest

from mapgis_agent.core import BridgeError, native_ids, read_native, sha
from mapgis_agent.native_backend import prepare_native_operation
from mapgis_agent.workspace import NativeWorkspaces
from test_native_wp import wp_case, enable_native


def shared_update(source):
    from mapgis_agent.topology import read_wp_arcs
    report = read_wp_arcs(str(source), sha(source.read_bytes()), limit=100)
    shared = next(arc for arc in report['arcs'] if arc['owner_region_ids'] == [1, 2])
    a, b = shared['coordinates'][0], shared['coordinates'][-1]
    return {'arc_id': shared['arc_id'], 'coordinates': [a, [12, 5], b]}


def test_arc_read_exposes_owners_separate_from_regions_and_rejects_stale_source(wp_case):
    from mapgis_agent.topology import read_wp_arcs
    _, source = wp_case
    report = read_wp_arcs(str(source), sha(source.read_bytes()), limit=2)
    assert len(report['arcs']) == 2 and report['next_offset'] == 2
    assert all(arc['arc_id'] > 0 for arc in report['arcs'])
    shared = shared_update(source)
    assert shared['coordinates'][0][0] == shared['coordinates'][-1][0] == 10
    with pytest.raises(BridgeError, match='STALE_SOURCE'):
        read_wp_arcs(str(source), '0' * 64)


def test_native_shared_arc_moves_both_neighbours_and_preserves_holes_identity_and_recovery(wp_case):
    bridge, source = wp_case
    enable_native(bridge)
    original = source.read_bytes()
    update = shared_update(source)
    prepared = prepare_native_operation(bridge, 'arc.replace', str(source), sha(original), {'updates': [update]}, 'edit-shared')
    output = Path(bridge.apply('edit-shared')['output'])
    result = read_native(output)[1]
    assert native_ids(result) == [1, 2, 3, 4]
    assert [g.area for g in result.geom] == pytest.approx([110, 90, 300, 36])
    assert result.data['ID'].tolist() == [7, 7, 7, 7]
    assert prepared['comparison']['changed_ids'] == [1, 2]
    assert prepared['comparison']['native_topology']['candidate']['rings'] == 5
    assert prepared['native_execution']['pid'] != prepared['native_reopen']['pid']
    bridge.recover('edit-shared', str(output), sha(output.read_bytes()), 'undo-shared')
    assert Path(bridge.apply('undo-shared')['output']).read_bytes() == original
    assert source.read_bytes() == original


def test_arc_edit_rejects_moving_a_shared_endpoint_before_starting_native_child(wp_case, monkeypatch):
    import mapgis_agent.native_backend as backend
    bridge, source = wp_case
    enable_native(bridge)
    original = source.read_bytes()
    update = shared_update(source)
    update['coordinates'][0][0] += 1
    calls = []
    monkeypatch.setattr(backend, '_invoke', lambda *a, **k: calls.append(a))
    with pytest.raises(BridgeError, match='ARC_ENDPOINTS'):
        prepare_native_operation(bridge, 'arc.replace', str(source), sha(original),
                                 {'updates': [update]}, 'bad-endpoint')
    assert not calls and source.read_bytes() == original


def test_shared_arc_workspace_survives_loss_and_can_remove_inserted_vertex(wp_case):
    bridge, source = wp_case
    enable_native(bridge)
    original = source.read_bytes()
    update = shared_update(source)
    registry = NativeWorkspaces(bridge)
    try:
        opened = registry.execute('open', 'wp-arcs', {'source': str(source), 'expected_sha256': sha(original)})
        edited = registry.execute('edit', 'wp-arcs', {'operation': 'arc.replace', 'parameters': {'updates': [update]}}, 0, 'arc-edit')
        assert edited['dirty'] and edited['native_pid'] == opened['native_pid']
        child = registry.buffers['wp-arcs'].process
        child.kill()
        child.wait(timeout=5)
        recovered = registry.execute('recover', 'wp-arcs', {}, 1)
        assert recovered['dirty'] and recovered['native_pid'] != opened['native_pid']
        shortened = {'arc_id': update['arc_id'], 'coordinates': [update['coordinates'][0], update['coordinates'][-1]]}
        shrunk = registry.execute('edit', 'wp-arcs', {'operation': 'arc.replace', 'parameters': {'updates': [shortened]}}, 1, 'remove-arc-node')
        assert [g.area for g in read_native(Path(shrunk['checkpoint']))[1].geom] == pytest.approx([100, 100, 300, 36])
        saved = registry.execute('save', 'wp-arcs', {}, 2, 'save')
        assert not saved['dirty']
        registry.execute('close', 'wp-arcs', {}, 2)
        assert source.read_bytes() == original
    finally:
        registry.close_all()


def test_shared_node_move_updates_all_incident_arcs_and_both_owners_with_exact_recovery(wp_case):
    from mapgis_agent.topology import read_wp_arcs
    bridge,source=wp_case;enable_native(bridge);before=source.read_bytes()
    report=read_wp_arcs(str(source),sha(before),limit=100)
    shared=next(a for a in report['arcs'] if a['owner_region_ids']==[1,2])
    nodes={n['node_id']:n for n in report['nodes']}
    updates=[{'node_id':nid,'x':12,'y':nodes[nid]['coordinates'][1]} for nid in shared['node_ids']]
    assert all(len(nodes[nid]['incident_arc_ids'])>=2 for nid in shared['node_ids'])
    prepared=prepare_native_operation(bridge,'node.move',str(source),sha(before),{'updates':updates},'nodes')
    output=Path(bridge.apply('nodes')['output']);reader=read_native(output)[1]
    assert [g.area for g in reader.geom]==pytest.approx([120,80,300,36])
    assert reader.data['ID'].tolist()==[7]*4 and native_ids(reader)==[1,2,3,4]
    assert prepared['comparison']['changed_ids']==[1,2]
    assert prepared['comparison']['edited_node_ids']==sorted(shared['node_ids'])
    assert prepared['comparison']['native_topology']['candidate']['rings']==5
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    after=read_wp_arcs(str(output),sha(output.read_bytes()),limit=100)
    actual={n['node_id']:n for n in after['nodes']}
    assert all(actual[u['node_id']]['coordinates']==[u['x'],u['y']] for u in updates)
    bridge.recover('nodes',str(output),sha(output.read_bytes()),'undo-nodes')
    assert Path(bridge.apply('undo-nodes')['output']).read_bytes()==before and source.read_bytes()==before


def test_shared_node_workspace_dirty_save_discard_and_recovery(wp_case):
    from mapgis_agent.topology import read_wp_arcs
    bridge,source=wp_case;enable_native(bridge);before=source.read_bytes()
    report=read_wp_arcs(str(source),sha(before),limit=100)
    shared=next(a for a in report['arcs'] if a['owner_region_ids']==[1,2]);nid=shared['node_ids'][0]
    old=shared['coordinates'][0];parameters={'updates':[{'node_id':nid,'x':12,'y':old[1]}]}
    registry=NativeWorkspaces(bridge)
    try:
        opened=registry.execute('open','wp-nodes',{'source':str(source),'expected_sha256':sha(before)})
        edited=registry.execute('edit','wp-nodes',{'operation':'node.move','parameters':parameters},0,'move')
        assert edited['dirty'] and edited['native_pid']==opened['native_pid']
        assert [g.area for g in read_native(Path(edited['checkpoint']))[1].geom]==pytest.approx([110,90,300,36])
        child=registry.buffers['wp-nodes'].process;child.kill();child.wait(timeout=5)
        recovered=registry.execute('recover','wp-nodes',{},1)
        assert recovered['dirty'] and recovered['native_pid']!=opened['native_pid']
        saved=registry.execute('save','wp-nodes',{},1,'save')
        assert not saved['dirty']
        registry.execute('edit','wp-nodes',{'operation':'node.move','parameters':{'updates':[{'node_id':nid,'x':13,'y':old[1]}]}},1,'second')
        closed=registry.execute('close','wp-nodes',{'discard':True},2)
        recovered=registry.execute('recover','wp-nodes',{},closed['revision'])
        assert [g.area for g in read_native(Path(recovered['checkpoint']))[1].geom]==pytest.approx([110,90,300,36])
        registry.execute('close','wp-nodes',{},recovered['revision'])
        assert source.read_bytes()==before
    finally:registry.close_all()


def test_shared_node_invalid_identity_and_collision_never_start_native_edit(wp_case,monkeypatch):
    from mapgis_agent.topology import read_wp_arcs
    import mapgis_agent.native_backend as backend
    bridge,source=wp_case;enable_native(bridge);before=source.read_bytes()
    report=read_wp_arcs(str(source),sha(before),limit=100);nodes=report['nodes'];first,second=nodes[:2]
    calls=[];monkeypatch.setattr(backend,'_invoke',lambda *args,**kwargs:calls.append(args))
    for index,updates in enumerate([
        [{'node_id':-1,'x':0,'y':0}],
        [{'node_id':first['node_id'],'x':float('nan'),'y':0}],
        [{'node_id':first['node_id'],'x':second['coordinates'][0],'y':second['coordinates'][1]}],
        [{'node_id':first['node_id'],'x':3,'y':4}]*2,
    ]):
        with pytest.raises(BridgeError,match='NODE_'):
            prepare_native_operation(bridge,'node.move',str(source),sha(before),{'updates':updates},'bad-node-'+str(index))
    assert not calls and source.read_bytes()==before
