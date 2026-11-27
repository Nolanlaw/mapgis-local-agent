"""Run the native drillhole section demo with a MapPlan checkpoint and resume."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import shutil
import time
import uuid

from mapgis_agent.core import BridgeError, json_text, sha
from mapgis_agent.drc import run_drc_audit
from mapgis_agent.geology_section import build_section_spec, read_drillhole_ledger, prepare_drillhole_assets
from mapgis_agent.map_plan import MapPlanEngine
from mapgis_agent.private_control import private_control
from mapgis_agent.raster import file_sha
from mapgis_agent.tables import write_literal_workbook
from mapgis_agent.visual_preview import _read_layers, _render, render_overview
from verify_com6 import configure_task_root


def make_inputs(root):
    inputs = root/'work/source-ledger'; inputs.mkdir()
    collar = inputs/'collar.csv'
    with collar.open('w', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerows([
            ['hole_id', 'easting', 'northing', 'elevation'], ['ZK01', 40500120, 2800011, 156],
            ['ZK02', 40500740, 2800030, 170],
        ])
    survey = inputs/'survey.json'
    survey.write_text(json_text([
        {'hole_id': hole, 'md': md, 'inclination': angle, 'azimuth': azimuth}
        for hole, angles, azimuth in (('ZK01', [6, 6, 11, 11], 90), ('ZK02', [13, 13, 19, 19], 270))
        for md, angle in zip((0, 42, 82, 125), angles)
    ]), encoding='utf-8')
    workbook = inputs/'geology.xlsx'
    write_literal_workbook(workbook, {
        'lithology': [['hole_id', 'top', 'base', 'unit', 'description'],
            ['ZK01', 0, 32, '砂岩', '细粒'], ['ZK01', 32, 67, '板岩', '板状'], ['ZK01', 72, 125, '花岗岩', '细晶'],
            ['ZK02', 0, 25, '砂岩', '细粒'], ['ZK02', 25, 81, '板岩', '板状'], ['ZK02', 81, 125, '花岗岩', '细晶']],
        'assay': [['hole_id', 'top', 'base', 'analyte', 'value', 'unit'], *[
            [hole, top, base, element, value, '%'] for hole in ('ZK01', 'ZK02')
            for element, values in (('Cu', [.10, .35, .48, .16]), ('Mo', [.015, .03, .09, .04]))
            for (top, base), value in zip(((0, 24), (24, 52), (56, 86), (86, 125)), values)
        ]],
    })
    return {'collar': collar, 'survey': survey, 'lithology': workbook, 'assay': workbook}


def verify_section_open(bridge, project):
    session = 'flagship-section-v5'
    members = [{'name': p.name, 'file': str(p), 'expected_sha256': file_sha(p)}
               for p in sorted(project.parent.iterdir()) if p.is_file() and p != project]
    status = private_control(bridge, 'open', module='section', session_id=session, parameters={
        'inputs': [{'id': 'project', 'file': str(project), 'expected_sha256': file_sha(project), 'dependencies': members}],
        'idle_seconds': 90})
    result = {'open': status, 'desktop_input_used': False}
    try:
        ready = None
        for attempt in range(25):
            observation = private_control(bridge, 'observe', session_id=session, request_id=f'wait-{attempt}')
            ready = next((w for w in observation.get('snapshot', {}).get('windows', [])
                          if w.get('menu') and w['visible'] and w['enabled']), None)
            if ready:
                break
            time.sleep(.25)
        if ready is None:
            raise BridgeError('SECTION_V5_MAIN_WINDOW_REQUIRED')
        main = {'class': ready['class'], 'visible': True, 'enabled': True}
        dialog = {'class': '#32770', 'title_regex': '打开MapGis文件', 'visible': True}
        edit = {'class': 'Edit', 'id': 1152, 'visible': True}
        okay = {'class': 'Button', 'id': 1, 'visible': True}
        steps = [
            {'preconditions': [{'window': main}],
             'action': {'operation': 'command', 'command_id': 57601, 'target': {'window': main}},
             'postconditions': [{'window': dialog, 'control': edit}]},
            {'preconditions': [{'window': dialog, 'control': edit}],
             'action': {'operation': 'set_text', 'binding': 'project', 'target': {'window': dialog, 'control': edit}},
             'postconditions': [{'window': dialog, 'control': {**edit, 'text_regex': r'(?i)\.mpj$'}}]},
            {'preconditions': [{'window': dialog, 'control': okay}],
             'action': {'operation': 'button', 'target': {'window': dialog, 'control': okay}},
             'postconditions': [{'window': {'class': '#32770', 'visible': True}, 'present': False},
                                {'window': {**main, 'title_regex': r'(?i)\[section\.mpj\]'}}]},
        ]
        result['macro'] = private_control(bridge, 'macro', session_id=session, request_id='read-native-project',
                                          parameters={'steps': steps, 'timeout_seconds': 10})
        result['readback'] = private_control(bridge, 'observe', session_id=session, request_id='readback')
        trees = []
        for window in result['readback'].get('snapshot', {}).get('windows', []):
            for control in window.get('children', []):
                if control['class'].lower() == 'systreeview32' and control['visible'] and control['enabled']:
                    trees.append(control)
        if trees:
            result['root_tree_items'] = private_control(bridge, 'act', session_id=session, request_id='project-tree',
                token=result['readback']['token'], parameters={'operation': 'tree_items', 'hwnd': trees[0]['hwnd'],
                                                               'parent_item': 0, 'limit': 30})
    except (OSError, RuntimeError, ValueError) as error:
        result['error'] = str(error)
    finally:
        result['close'] = private_control(bridge, 'close', session_id=session, request_id='close', parameters={'discard': False})
        if result['close']['state'] != 'CLOSED':
            result['cleanup'] = private_control(bridge, 'close', session_id=session, request_id='discard', parameters={'discard': True})
        for _ in range(30):
            result['closed'] = private_control(bridge, 'status', session_id=session)
            if not any(result['closed']['processes_alive'].values()):
                break
            time.sleep(.1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    output = parser.parse_args().output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    bridge = configure_task_root(output/('run-'+uuid.uuid4().hex[:10]))
    sources = make_inputs(bridge.root)
    frame = {'crs': {'definition': 'EPSG:4528', 'axis_order': 'xy'}, 'vertical_unit': 'metre',
             'vertical_datum': 'Synthetic benchmark datum', 'azimuth_reference': 'grid_north',
             'inclination_reference': 'vertical_down'}
    params = {'title': 'A-A 勘探线剖面', 'section': {'start': [40500000, 2800000], 'end': [40500900, 2800000],
             'max_crossline_distance': 60}, 'horizontal_scale': 5000, 'vertical_exaggeration': 2,
             'lithology_styles': {'砂岩': {'fill_color': 3}, '板岩': {'fill_color': 5}, '花岗岩': {'fill_color': 8}},
             'attitudes': [{'dip_direction': 90, 'dip': 35}, {'dip_direction': 270, 'dip': 35}]}
    plan_id = 'flagship-geology'
    engine = MapPlanEngine(bridge.root, backend_version='mapgis-v3-sprint3-geology/1')
    plan = {'plan_id': plan_id, 'crs': 'LOCAL', 'unit': 'mm', 'scale': 1,
            'nodes': [
        {'id': 'bind', 'op': 'section.drillhole_workflow', 'params': {'frame': frame},
         'inputs': [{'role': key, 'source_file': str(p), 'expected_sha256': file_sha(p)} for key, p in sources.items()]},
        {'id': 'geometry', 'op': 'section.drillhole_workflow', 'params': params,
         'inputs': [{'role': 'ledger', 'from_node': 'bind', 'output_key': 'ledger'}]},
        {'id': 'publish', 'op': 'section.drillhole_workflow', 'params': {'publication': 'editable native MPJ'},
         'inputs': [{'role': 'geometry', 'from_node': 'geometry', 'output_key': 'drawings'}]},
    ]}
    calls = Counter()

    def execute(node, inputs, context):
        calls[node.id] += 1
        print('MapPlan', node.id, context.cache_key[:12], flush=True)
        if node.id == 'bind':
            bindings = {item.role: {'file': str(item.path), 'expected_sha256': item.sha256} for item in inputs}
            return {'ledger': read_drillhole_ledger(bindings, node.params['frame'])}
        values = json.loads(inputs[0].path.read_text(encoding='utf-8'))
        if node.id == 'geometry':
            return {'drawings': build_section_spec(values, node.params)}
        identifier = 'plan-'+context.cache_key[:24]
        prepare_drillhole_assets(bridge, values, identifier)
        ready = bridge.apply(identifier, context.publisher_epoch)
        return {'native': Path(ready['output']), 'artifact': {'request_id': identifier, 'output': ready['output']}}

    before = engine.apply(plan, executor=execute, fault_inject_node='publish')
    if before['status'] != 'INTERRUPTED' or before['interruption'] != {'node': 'publish', 'code': 'FAULT_INJECTED'}:
        raise BridgeError('FLAGSHIP_FAULT_INJECTION_UNVERIFIED')
    resumed = engine.resume(plan_id, executor=execute)
    if (resumed['status'] != 'COMPLETE' or resumed['executed_nodes'] != ['publish']
            or resumed['reused_nodes'] != ['bind', 'geometry'] or any(v != 1 for v in calls.values())):
        raise BridgeError('FLAGSHIP_CACHE_REUSE_UNVERIFIED')
    pointer = json.loads(Path(resumed['completed']['publish']['paths']['artifact']).read_text(encoding='utf-8'))
    native = Path(pointer['output'])
    receipt = json.loads(native.read_text(encoding='utf-8'))
    qa = {}
    previews = bridge.root/'previews'; previews.mkdir()
    for name, item in receipt['projects'].items():
        layers = [{'file': str(native.parent/row['file']), 'expected_sha256': row['sha256']} for row in item['layers']]
        drc = run_drc_audit(layers, 'LOCAL', enforce_frames=True)
        overview = render_overview(layers, previews/(name+'-overview.png'), width_px=1600, height_px=950, dpi=180)
        plottable = _read_layers(layers)
        first_polygon = next(v[2].geom[0] for v in plottable if v[2].shapeType == 'POLYGON')
        a, b, c, d = first_polygon.bounds
        detail = _render(plottable, previews/(name+'-detail.png'), 1200, 800, 240, None,
                         '钻孔与岩性细节', view=(a-15, b-12, c+42, d+15))
        qa[name] = {'drc': drc, 'overview': overview, 'detail': detail}
    app = verify_section_open(bridge, native.parent/receipt['projects']['section']['file'])
    app_ok = (app.get('macro', {}).get('state') == 'VERIFIED'
              and not any(app.get('closed', {}).get('processes_alive', {'missing': True}).values()))
    final = {'schema': 'mapgis-agent/flagship-geology/1', 'plan': plan, 'interrupted': before,
             'resumed': resumed, 'observed_executor_calls': dict(calls), 'native_bundle': str(native.parent),
             'native': receipt, 'qa': qa, 'section_v5': app, 'section_v5_native_open_verified': app_ok}
    fixed = output/'flagship-receipt.json'
    fixed.write_text(json_text(final), encoding='utf-8')
    print(f'flagship resumed; cache reused 2; section V5 open={app_ok}; {fixed}', flush=True)
    return 0 if app_ok and all(item['drc']['summary']['passed'] for item in qa.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
