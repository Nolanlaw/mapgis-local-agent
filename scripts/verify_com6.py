"""Exercise the six pinned Automation DLLs in fresh guarded x86 workers."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import uuid

import psutil

from mapgis_agent.core import Bridge, BridgeError, json_text
from mapgis_agent.raster import file_sha
from mapgis_agent.sdk_catalog import sdk_describe
from mapgis_agent.sdk_flow import prepare_sdk_flow


ROOT = Path(__file__).resolve().parents[1]


def configure_task_root(root):
    settings = json.loads((ROOT/'config.local.json').read_text(encoding='utf-8-sig'))
    selected = {key: settings[key] for key in ('mapgis_installation',
                'native_backend_legacy_dep_compatibility', 'native_general_private_ui') if key in settings}
    root.mkdir(parents=True, exist_ok=False)
    (root/'config.local.json').write_text(json_text(selected), encoding='utf-8')
    return Bridge(root)


def probe(bridge, identity, library, cls, steps, objects=()):
    parameters = {'library': library, 'inputs': [], 'outputs': [],
                  'objects': [{'id': 'obj', 'class': cls}, *objects], 'steps': steps}
    stage = None
    try:
        ready = prepare_sdk_flow(bridge, parameters, identity)
        stage = Path(ready['stage'])
        applied = bridge.apply(identity)
        outcome = json.loads(Path(applied['output']).read_text(encoding='utf-8'))
        result = {'status': applied['state'], 'artifact_sha256': file_sha(Path(applied['output'])),
                  'report': str(applied['output']), 'values': {x['id']: x.get('value', x.get('object')) for x in outcome['values']},
                  'variant_types': {x['id']: x['vt'] for x in outcome['values']}}
    except (BridgeError, OSError) as exc:
        try:
            stage = Path(bridge.status(identity)['stage'])
        except (BridgeError, KeyError):
            pass
        result = {'status': 'FAILED', 'error': str(exc)}
    if stage is not None and (stage/'sdk-native.json').is_file():
        trace = json.loads((stage/'sdk-native.json').read_text(encoding='utf-8'))
        started = json.loads((stage/'sdk-native.json.started.json').read_text(encoding='utf-8'))
        result['native'] = {**trace, 'process_start': started['process_start'],
                            'private_desktop': trace['desktop'].startswith('MapGISAgent_'),
                            'owned_process_exited': not psutil.pid_exists(trace['pid'])}
    return {'library': library, 'class': cls, **result}


def step(identity, member, kind, *arguments, target='obj', **checks):
    return {'id': identity, 'target': target, 'member': member, 'kind': kind,
            'arguments': list(arguments), **checks}


def roundtrip(member, value):
    return [step('written', member, 'put', {'value': value}),
            step('readback', member, 'get', expect=value)]


def attribute_rows():
    """Synthetic in-memory native table, one Chinese value and one null."""
    return (
        [step('label', 'fieldname', 'put', {'value': '孔号'}, target='head'),
         step('label_read', 'fieldname', 'get', target='head', expect='孔号'),
         step('character', 'fieldtype', 'put', {'value': 0}, target='head'),
         step('length', 'msk_leng', 'put', {'value': 24}, target='head'),
         step('header', 'AppendField', 'method', {'ref': 'head'}, target='schema', expect=True),
         step('schema_set', 'stru', 'put', {'ref': 'schema'}),
         step('field_set', 'FieldHD', 'put', {'ref': 'head'}, target='field'),
         step('field_value', 'Value', 'put', {'value': '钻孔甲'}, target='field'),
         step('record_value', 'Append', 'method', {'ref': 'field'}, target='record', expect=True),
         step('row_id', 'AppendRcd', 'method', {'ref': 'record'}),
         step('row_count', 'nCount', 'get'),
         step('read_row', 'Get1', 'method', {'ref': 'row_id'}, {'out': 'row_status'}),
         step('chinese_read', 'Value', 'get', {'value': 0}, target='read_row', expect='钻孔甲'),
         step('empty_set', 'Value', 'put', {'value': 0}, {'value': ''}, target='record'),
         step('empty_row_id', 'AppendRcd', 'method', {'ref': 'record'}),
         step('empty_row', 'Get1', 'method', {'ref': 'empty_row_id'}, {'out': 'empty_row_status'}),
         step('empty_read', 'Value', 'get', {'value': 0}, target='empty_row', expect='')],
        [{'id': 'head', 'class': 'Field_Head'}, {'id': 'schema', 'class': 'Record_Head'},
         {'id': 'field', 'class': 'Field'}, {'id': 'record', 'class': 'Record'}],
    )


def axes_steps():
    return [step('current_id', 'CurEllipID', 'get'),
            step('degree', 'SToDMS', 'method', {'value': 3600}, expect=10000.0),
            *[step(f'read_{idx}', 'GetEllipParaById', 'method', {'value': idx}, {'out': f'ellipsoid_{idx}'})
              for idx in range(1, 11)]]


def classify_ellipsoids(values):
    known = {'Beijing54/Krassowsky': (6378245.0, 298.3), 'Xian80/IAG1975': (6378140.0, 298.257),
             'WGS84': (6378137.0, 298.257223563), 'CGCS2000/GRS80': (6378137.0, 298.257222101)}
    matches = {}
    for key, axes in values.items():
        if not key.startswith('ellipsoid_') or not isinstance(axes, list) or len(axes) != 4:
            continue
        major, minor, flattening, _ = axes
        for name, (a, inverse_f) in known.items():
            if math.isclose(major, a, rel_tol=0, abs_tol=.01):
                inverse_observed = 1/flattening if 0 < flattening < 1 else major/(major-minor) if major > minor else 0
                if math.isclose(inverse_observed, inverse_f, rel_tol=0, abs_tol=2e-7):
                    matches[name] = {'native_id': int(key.split('_')[-1]), 'raw_values': axes,
                                     'inverse_flattening': inverse_observed}
    return matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    output = parser.parse_args().output.resolve()
    root = output.parent/'com6-runs'/uuid.uuid4().hex[:10]
    bridge = configure_task_root(root)
    bas = 'MapGisBasCom1.DLL'
    maps = 'MapGisMapCom.dll'
    layers = 'MapgisLayerCom.dll'
    att = 'AttCom.dll'
    rows, objects = attribute_rows()
    cases = [
        ('dot', bas, 'D_Dot', roundtrip('x', 120.125), []),
        ('ellipsoid', 'proj60.dll', 'EllipPara', axes_steps(), []),
        ('link', att, 'LinkAtt', [*roundtrip('linkFldName', '孔号'),
                                step('empty_set', 'linkedFldName', 'put', {'value': ''}),
                                step('empty_value', 'linkedFldName', 'get', expect='')], []),
        ('gis-object', att, 'GisObj', [], []),
        ('upload', att, 'UpLoad', [step('initial_error', 'GetUploadErrCode', 'method')], []),
        ('attribute-table', bas, 'TblArea', [*roundtrip('Name', '钻孔台账'),
                                           step('empty', 'Empty', 'get'), step('count', 'nCount', 'get')], []),
        ('attribute-record', bas, 'TblArea', rows, objects),
        ('attribute-null', bas, 'Field', [step('null_set', 'Value', 'put', {'value': None}),
                                         step('null_read', 'Value', 'get')], []),
        ('map', maps, 'Map', [*roundtrip('MapName', 'Geology'), step('layer_count', 'MapLayerCount', 'get')], []),
        ('layout', maps, 'MapLayout', [], []), ('scale-bar', maps, 'MapScaleBar', [], []),
        ('scale-text', maps, 'MapScaleText', [], []), ('north-arrow', maps, 'MapNorthArrow', [], []),
        ('legend', maps, 'MapLegend', roundtrip('MapLegName', 'Units'), []),
        ('page', maps, 'MapPrintParam', roundtrip('PageWidth', 210.0), []),
        ('group', layers, 'GroupLayer', [step('count', 'Count', 'get', expect=0),
                                        step('children', 'GetSubLayerCount', 'method', expect=0)], []),
        ('selection', layers, 'SelectSet', [*roundtrip('Name', 'Section'), step('count', 'Count', 'get', expect=0)], []),
        ('selection-item', layers, 'SelectItem', [step('item', 'ItemNo', 'get')], []),
        ('network-layer', layers, 'NetLayer', [step('visible', 'Visible', 'get')], []),
        ('vector-layer', layers, 'VectorLayer', [step('visible', 'Visible', 'get')], []),
        ('display', 'MapGisDspCom.dll', 'MapGisDC', [step('original', 'OriginalDisp', 'get', expect=0)], []),
    ]
    catalog = sdk_describe(root)['libraries']
    result = {'schema': 'mapgis-agent/com6-verification/1', 'run_root': str(root), 'catalog': catalog,
              'desktop_input_used': False, 'registry_registration_used': False, 'cases': {}}
    for name, library, cls, steps, objects in cases:
        record = probe(bridge, name, library, cls, steps, objects)
        result['cases'][name] = record
        print(name, record['status'], record.get('error', ''), flush=True)
    result['ellipsoid_matches'] = classify_ellipsoids(result['cases']['ellipsoid'].get('values', {}))
    passed = {x['library'] for x in result['cases'].values() if x['status'] == 'APPLIED'
              and x.get('native', {}).get('owned_process_exited')}
    result['passed_libraries'] = sorted(passed)
    result['limitations'] = {k: v.get('error') for k, v in result['cases'].items() if v['status'] != 'APPLIED'}
    result['complete'] = len(passed) == 6 and all(result['cases'][k]['status'] == 'APPLIED'
                                                 for k in ('dot', 'ellipsoid', 'link', 'group', 'display'))
    output.write_text(json_text(result), encoding='utf-8')
    print(f'{len(passed)}/6 libraries passed; {output}', flush=True)
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
