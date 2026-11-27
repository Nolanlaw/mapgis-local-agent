import json
import os
from pathlib import Path
import shutil

from pydantic import ValidationError
import pytest
from shapely.geometry import LineString, box

from mapgis_agent.core import Bridge, BridgeError, annotation_text, read_native
from mapgis_agent.fdr import RedactionPolicy, export_fdr_bundle
from mapgis_agent.map_plan import MapPlanEngine, compute_cache_key, parse_spec
from mapgis_agent.native_project import prepare_native_project
from mapgis_agent.object_store import file_sha256
from mapgis_agent.visual_preview import _read_png, render_diff_heatmap, render_drc_crops, render_overview
from test_drc_lvs import write_layer
from test_label_placement import annotations
from test_native_raster import native_settings


def source(role, path):
    return {'role': role, 'source_file': str(path), 'expected_sha256': file_sha256(path)}


def upstream(role, node, key):
    return {'role': role, 'from_node': node, 'output_key': key}


def spec(nodes, *, plan_id='sprint2', **options):
    return {'schema_version': 'mapgis-plan/v1', 'plan_id': plan_id, 'description': 'Synthetic cartography',
            'crs': 'LOCAL', 'unit': 'local', 'scale': 1000, 'seed': 19, 'nodes': nodes} | options


def test_order_role_and_parameter_sensitive_cache_key(tmp_path):
    path1, path2 = tmp_path / 'first.txt', tmp_path / 'second.txt'
    path1.write_text('first', encoding='utf8')
    path2.write_text('second', encoding='utf8')
    original = {'id': 'combine', 'op': 'file.compare',
                'inputs': [source('left', path1), source('right', path2)], 'params': {'rules': [1, 2], 'value': 3}}
    typed = parse_spec(json.dumps(spec([original])))
    assert typed.nodes[0].inputs[0].role == 'left'
    rows = [{'role': item.role, 'sha256': item.expected_sha256} for item in typed.nodes[0].inputs]
    key = compute_cache_key(typed.nodes[0], rows, typed)
    assert key != compute_cache_key(typed.nodes[0], rows[::-1], typed)
    assert key != compute_cache_key(typed.nodes[0], [rows[0] | {'role': 'right'}, rows[1] | {'role': 'left'}], typed)
    assert key == compute_cache_key(original | {'params': {'value': 3, 'rules': [1, 2]}}, rows, typed)
    assert key != compute_cache_key(original | {'params': {'rules': [2, 1], 'value': 3}}, rows, typed)
    with pytest.raises(ValidationError, match='expected_sha256'):
        parse_spec(spec([{'id': 'u', 'op': 'work.copy', 'inputs': [{'role': 'file', 'source_file': 'u'}]}]))


def test_reject_dag_cycles_unknown_outputs_and_static_frame(tmp_path):
    engine = MapPlanEngine(tmp_path)
    one = {'id': 'a', 'op': 'work.copy', 'inputs': [upstream('file', 'b', 'file')]}
    two = {'id': 'b', 'op': 'work.copy', 'inputs': [upstream('file', 'a', 'file')]}
    with pytest.raises(BridgeError, match='PLAN_CYCLE'):
        engine.plan(spec([one, two]))
    with pytest.raises(BridgeError, match='PLAN_UNKNOWN_NODE'):
        engine.plan(spec([one]))
    with pytest.raises(BridgeError, match='PLAN_OUTPUT_KEY'):
        engine.plan(spec([{'id': 'a', 'op': 'work.copy'},
                          {'id': 'b', 'op': 'work.copy', 'inputs': [upstream('file', 'a', 'unknown')]}]))
    bounds = {'spatial_bounds': [450_000, 3_100_000, 550_000, 3_300_000]}
    with pytest.raises(BridgeError, match='AUDIT_FALSE_EASTING'):
        engine.plan(spec([{'id': 'proj', 'op': 'work.copy', 'effect': bounds}], crs='EPSG:4528', unit='m'))
    with pytest.raises(BridgeError, match='AUDIT_CRS_MISMATCH'):
        engine.plan(spec([{'id': 'proj', 'op': 'work.copy'}], crs='EPSG:4549', unit='degree'))
    with pytest.raises(BridgeError, match='PLAN_EXCLUSIVE_SESSION'):
        engine.plan(spec([{'id': key, 'op': 'work.copy', 'effect': {'exclusive_session': 'native'}}
                          for key in ('left', 'right')]))


def test_epoch_and_completed_cas_are_fenced(tmp_path):
    bridge = Bridge(tmp_path)
    engine = MapPlanEngine(bridge.root)
    file = tmp_path / 'source.txt'
    file.write_text('content', encoding='utf8')
    document = spec([{'id': 'copy', 'op': 'work.copy', 'inputs': [source('file', file)]},
                     {'id': 'tail', 'op': 'work.copy', 'inputs': [upstream('file', 'copy', 'file')]}])
    initial = engine.apply(document, fault_inject_node='tail', expected_epoch=0)
    assert initial['status'] == 'INTERRUPTED'
    assert bridge.advance_epoch() == 1
    with pytest.raises(BridgeError, match='STALE_PUBLISHER_EPOCH'):
        engine.resume(document['plan_id'], expected_epoch=0)
    # Resume from a sealed dependency after the mutable external file changes.
    file.write_text('different', encoding='utf8')
    result = engine.resume(document['plan_id'], expected_epoch=1)
    assert result['reused_nodes'] == ['copy'] and result['publisher_epoch'] == 1
    assert Path(result['completed']['tail']['paths']['file']).read_text() == 'content'
    obj = engine.store.path_for(initial['completed']['copy']['outputs']['file']['sha256'])
    obj.write_text('corrupted', encoding='utf8')
    with pytest.raises(BridgeError, match='PLAN_CHECKPOINT_CORRUPT'):
        engine.resume(document['plan_id'], expected_epoch=1)


def _snapshot_paths(report):
    return {node: {key: Path(value) for key, value in entry['paths'].items()}
            for node, entry in report['completed'].items()}


def test_map_plan_native_vertical_slice_resume_and_preview(tmp_path):
    native_settings(tmp_path)
    layer_paths = {
        'wp': write_layer(tmp_path, 'geology.wp', [box(0, 0, 50, 45), box(50, 0, 100, 45)],
                          properties=[{'CODE': 'Q'}, {'CODE': 'K'}]),
        'wl': write_layer(tmp_path, 'fault.wl', [LineString([(4, 2), (28, 22), (90, 27)])]),
        'wt': annotations(tmp_path),
    }
    copied = [{'id': f'copy-{kind}', 'op': 'work.copy', 'inputs': [source('layer', path)]}
              for kind, path in layer_paths.items()]
    ordered_layers = [upstream('layer', 'copy-wp', 'file'), upstream('layer', 'copy-wl', 'file'),
                      upstream('annotation', 'patch-labels', 'file')]
    nodes = [*copied,
             {'id': 'labels', 'op': 'labels.place', 'inputs': [upstream('annotation', 'copy-wt', 'file')],
              'params': {'solver': 'simulated_annealing', 'locked_ids': [1], 'iterations': 1000}},
             {'id': 'patch-labels', 'op': 'file.patch', 'effect': {'exclusive_session': 'file-edit'},
              'inputs': [upstream('annotation', 'copy-wt', 'file'), upstream('patch_plan', 'labels', 'report')]},
             {'id': 'inspect', 'op': 'native.inspect', 'effect': {'exclusive_session': 'file-edit'},
              'inputs': [upstream('annotation', 'patch-labels', 'file')]},
             {'id': 'audit', 'op': 'drc.audit', 'inputs': ordered_layers},
             {'id': 'preview', 'op': 'preview.render', 'inputs': [*ordered_layers, upstream('drc_report', 'audit', 'report')],
              'params': {'title': '区域地质图 · 注记避让', 'width_px': 1000, 'height_px': 720}}]
    document = spec(nodes)
    engine = MapPlanEngine(tmp_path)
    prediction = engine.plan(document)
    assert len(prediction['nodes']) == 8 and prediction['nodes'][3]['deferred_inputs'] == ['copy-wt']
    invocations = []

    def trace(node, _inputs, _context):
        invocations.append(node.id)

    interrupted = engine.apply(document, executor=trace, fault_inject_node='inspect', expected_epoch=0)
    assert interrupted['status'] == 'INTERRUPTED' and interrupted['interruption']['node'] == 'inspect'
    assert invocations == ['copy-wp', 'copy-wl', 'copy-wt', 'labels', 'patch-labels']
    past_paths = _snapshot_paths(interrupted)
    checkpoint_stamps = {node: {name: path.stat().st_mtime_ns for name, path in outputs.items()}
                         for node, outputs in past_paths.items()}
    artifacts = os.environ.get('MAPGIS_SPRINT2_ARTIFACTS')
    if artifacts:
        deliver = Path(artifacts)
        deliver.mkdir(parents=True, exist_ok=True)
        export_fdr_bundle(tmp_path, deliver / 'checkpoint.mapgis-fdr.zip', plan_id='sprint2')

    invocation_count = len(invocations)
    done = MapPlanEngine(tmp_path).resume('sprint2', executor=trace, expected_epoch=0)
    assert done['status'] == 'COMPLETE' and done['reused_nodes'] == invocations[:invocation_count]
    assert invocations[invocation_count:] == ['inspect', 'audit', 'preview']
    for node, outputs in past_paths.items():
        assert {name: path.stat().st_mtime_ns for name, path in outputs.items()} == checkpoint_stamps[node]
    saved = _snapshot_paths(done)
    native_info = json.loads(saved['inspect']['report'].read_text(encoding='utf8'))
    assert native_info['active'] == 4 and native_info['private_desktop']
    original = read_native(layer_paths['wt'])
    edited = read_native(saved['patch-labels']['file'])
    assert original[1].geom[0].equals_exact(edited[1].geom[0], 0)
    assert any(not a.equals_exact(b, 0) for a, b in zip(original[1].geom[1:], edited[1].geom[1:]))
    assert [annotation_text(original[0], original[2], i) for i in range(1, 5)] == [
            annotation_text(edited[0], edited[2], i) for i in range(1, 5)]
    png = saved['preview']['preview']
    assert _read_png(png).shape == (720, 1000, 3)
    same = engine.diff(document, done)
    assert same['unchanged_cached'] == done['topological_order'] and same['recompute_required'] == []
    revised = json.loads(json.dumps(document))
    revised['nodes'][3]['params']['iterations'] = 800
    delta = engine.diff(revised, done)
    assert {'labels', 'patch-labels', 'audit', 'preview'} <= set(delta['recompute_required'])
    assert any('labels' in reason for reason in delta['reasons']['preview'])

    if artifacts:
        # The evidence previews are synthetic and contain only public sample labels.
        report = json.loads(saved['audit']['report'].read_text(encoding='utf8'))
        shutil.copyfile(png, deliver / 'overview.png')
        original_preview = render_overview(list(layer_paths.values()), deliver / 'before.png',
                                            title='区域地质图 · 注记避让', width_px=1000, height_px=720, drc_report=report)
        heatmap = render_diff_heatmap(original_preview['path'], deliver / 'overview.png', deliver / 'heatmap.png')
        crops = render_drc_crops([layer_paths['wp'], layer_paths['wl'], saved['patch-labels']['file']],
                                 report, deliver / 'crops', max_crops=2)
        export_fdr_bundle(tmp_path, deliver / 'recompute.mapgis-fdr.zip', mode='deterministic_recompute',
                          plan_id='sprint2', drc_report=report, preview_paths=[deliver / 'overview.png'],
                          redaction=RedactionPolicy(coordinate_offset=(200., 300.), previews_are_redacted=True))
        summary = {'interrupted': interrupted['interruption'], 'executed_before_fault': invocations[:invocation_count],
                   'executed_after_resume': invocations[invocation_count:], 'reused': done['reused_nodes'],
                   'native_active_count': native_info['active'], 'heatmap': heatmap, 'crops': crops,
                   'label_report': json.loads(saved['labels']['report'].read_text(encoding='utf8')),
                   'cache_keys': {key: value['cache_key'] for key, value in done['completed'].items()}}
        (deliver / 'vertical-slice.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf8')


def test_import_real_saved_mpj_keeps_native_layer_order(tmp_path):
    bridge = Bridge(tmp_path)
    native_settings(tmp_path)
    wp = write_layer(tmp_path, 'areas.wp', [box(0, 0, 8, 8)])
    wl = write_layer(tmp_path, 'boundary.wl', [LineString([(0, 0), (8, 8)])])
    wt = annotations(tmp_path)
    layers = [wp, wl, wt]
    bindings = [{'file': str(path), 'expected_sha256': file_sha256(path)} for path in layers]
    prepare_native_project(bridge, bindings, {'name': 'map.mpj', 'coordinate_frame': 'native'}, 'project')
    project = Path(bridge.apply('project')['output'])
    result = MapPlanEngine(tmp_path).import_mpj(project)
    copied = result['nodes'][:3]
    assert [Path(node['inputs'][0]['source_file']).suffix for node in copied] == ['.wp', '.wl', '.wt']
    assert [node['inputs'][0]['expected_sha256'] for node in copied] == [row['expected_sha256'] for row in bindings]
    assert [node['op'] for node in result['nodes'][-2:]] == ['drc.audit', 'preview.render']
    assert MapPlanEngine(tmp_path).plan(result)['topological_order'][-2:] == ['audit', 'preview']
