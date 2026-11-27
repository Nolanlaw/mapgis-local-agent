"""Thirty GIS tasks and their independent artifact/semantic acceptance checks."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
import struct
from typing import Awaitable, Callable
from zipfile import ZipFile

from pyproj import Transformer
from shapely.geometry import LineString, Point, box, mapping

from mapgis_agent.core import Bridge, json_text, read_native, sha
from mapgis_agent.creation import prepare_creation, prepare_modern_crs_creation
from mapgis_agent.crs_contract import read_crs_sidecar
from mapgis_agent.fdr import verify_fdr_bundle
from mapgis_agent.map_sheets import map_sheets
from mapgis_agent.object_store import file_sha256


LOCAL = {'kind': 'local', 'name': 'Synthetic benchmark grid', 'axis_order': 'xy', 'unit': 'metre',
         'reference_sha256': '0' * 64}
TM = {'definition': '+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs',
      'axis_order': 'xy'}
SEED = 20261127


@dataclass(frozen=True)
class Task:
    id: str
    level: str
    title: str
    query: str
    expected_ops: tuple[str, ...]
    execute: Callable[..., Awaitable[None]]
    direct_tools: tuple[str, ...] = ()
    native_route: str | None = None
    safety_checks: int = 0
    required_tool: str | None = None


def _frame(epsg):
    return {'definition': f'EPSG:{epsg}', 'axis_order': 'xy'}


def _binding(path):
    return {'file': str(path), 'expected_sha256': file_sha256(path)}


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def _features(geometries, properties=None, texts=None):
    rows = properties if properties is not None else [{'ID': 7, 'GRADE': 10} for _ in geometries]
    records = []
    for index, (geometry, attributes) in enumerate(zip(geometries, rows, strict=True), 1):
        record = {'type': 'Feature', 'id': str(index), 'geometry': mapping(geometry), 'properties': attributes}
        if texts is not None:
            record |= {'text': texts[index-1], 'style': {'height': 3., 'width': 3., 'color': 1}}
        records.append(record)
    return {'type': 'FeatureCollection', 'features': records}


def _vector(root, name, geometries, *, properties=None, texts=None, epsg=None, crs=None, scale=1000):
    bridge = Bridge(root)
    request = 'fixture-' + name.replace('.', '-')
    template = {'name': name, 'map_scale': scale, 'crs': _frame(epsg) if epsg else (crs or LOCAL)}
    prepare = prepare_modern_crs_creation if epsg else prepare_creation
    prepare(bridge, _features(geometries, properties, texts), template, request)
    return Path(bridge.apply(request)['output'])


def _points(root, name='point.wt'):
    return _vector(root, name, [Point(0, 0), Point(4, 2), Point(8, 3)])


def _labels(root):
    return _vector(root, 'labels.wt', [Point(12, 12), Point(12.5, 12.2), Point(12.9, 12.4), Point(14, 13)],
                   texts=['全新统', 'K1 unit', '花岗岩', 'Qh'])


async def _inspect(c, path, **args):
    return await c.route('mapgis_inspect_file', {'file': str(path), **args}, op='file.inspect', read=True)


async def _copy(c, source):
    return await c.route('mapgis_make_work_copy', {'source': str(source), 'request_id': 'owned-copy'})


async def _patch(c, path, request='patch', **options):
    return await c.route('mapgis_prepare_patch', {
        **_binding(path), 'record_ids': [1], 'request_id': request, 'attributes': {'GRADE': 19},
    }, op='file.patch', **options)


async def _commit(c, request, epoch=0, **options):
    arguments = {'request_id': request, 'expected_epoch': epoch}
    return await c.call('mapgis_apply_patch' if c.surface == 'legacy' else 'mapgis_tx_commit', arguments, **options)


async def _audit(c, files, **options):
    expected_error = options.pop('expected_error', None)
    return await c.call('mapgis_audit_check', {'files': [_binding(path) for path in files], **options},
                        expected_error=expected_error)


async def inspect_file(c):
    source = _points(c.root)
    result = await _inspect(c, source, offset=1, limit=1)
    assert (result['type'], result['count'], result['next_offset']) == ('POINT', 3, 2)
    assert result['sha256'] == file_sha256(source) and result['rows'][0]['record_id'] == 2
    assert result['rows'][0]['attributes']['ID'] == 7


async def page_features(c):
    source = _points(c.root)
    args = _binding(source) | {'limit': 2}
    first = await c.route('mapgis_read_features', args, op='file.features', read=True)
    second = await c.route('mapgis_read_features', args | {'offset': first['next_offset']}, op='file.features', read=True)
    assert first['count'] == second['count'] == 3 and first['next_offset'] == 2 and second['next_offset'] is None
    assert [int(row['id']) for page in (first, second) for row in page['collection']['features']] == [1, 2, 3]


async def work_copy(c):
    fixture = _points(c.root)
    original = c.root / 'source.wt'
    shutil.copyfile(fixture, original)
    initial = file_sha256(original)
    first = await _copy(c, original)
    assert (await _copy(c, original)) == first and first['source_sha256'] == initial
    output = Path(first['path'])
    assert output.is_relative_to(c.root / 'deliverables') and file_sha256(output) == initial
    await _patch(c, original, expected_error='AUTHORIZATION_BOUNDARY')
    assert file_sha256(original) == initial and not (c.root / 'work' / 'stage' / 'patch').exists()


async def patch_and_commit(c):
    source = Path((await _copy(c, _points(c.root)))['path'])
    initial = file_sha256(source)
    pending = await _patch(c, source)
    assert pending['state'] == 'PREPARED' and not Path(pending['output']).exists()
    assert file_sha256(source) == initial
    applied = await _commit(c, 'patch')
    assert applied['state'] == 'APPLIED' and applied['candidate_sha256'] == file_sha256(applied['output'])
    saved = await _inspect(c, applied['output'])
    assert [row['attributes']['GRADE'] for row in saved['rows']] == [19, 10, 10]


async def compare_files(c):
    before = _points(c.root)
    after = _vector(c.root, 'after.wt', [Point(0, 0), Point(4, 2), Point(8, 3)],
                    properties=[{'ID': 7, 'GRADE': grade} for grade in (10, 19, 10)])
    tool = 'mapgis_compare_files' if c.surface == 'legacy' else 'mapgis_compare'
    result = await c.call(tool, {'before': str(before), 'after': str(after)})
    assert result['changed_ids'] == [2] and result['counts'] == {'changed': 1, 'unchanged': 2, 'added': 0, 'removed': 0}


async def capabilities(c):
    tool = 'mapgis_get_capabilities' if c.surface == 'legacy' else 'mapgis_environment'
    result = await c.call(tool, {})
    assert result['loaded_runtime']['tool_count'] == len(c.tool_names)
    assert result['loaded_runtime']['operation_count'] > 100
    detail = await c.describe('file.inspect')
    assert detail['input_schema']['properties']['file']['type'] == 'string'


async def projection(c):
    result = await c.route('mapgis_projection_points', {'points': [[120., 30.]], 'source_crs': _frame(4490),
                           'target_crs': _frame(4549)}, op='projection.point', read=True)
    x, y = result['points'][0]
    assert abs(x - 500_000) < 1e-6 and abs(y - 3320113.397845) < 1e-5
    assert result['transformer']['always_xy'] and not result['transformer']['ballpark']


async def abi(c):
    result = await c.route('mapgis_probe_abi', {}, op='abi.probe', read=True)
    assert result['tier'] == c.native['tier'] and result['capabilities']['headless_open_engine']
    if c.native['tier'] == 'A':
        assert len(result['modules']) == 4 and result['route'] == 'EXACT_CERTIFIED'


async def provider(c):
    result = await c.route('mapgis_provider_inspect', {'operation_id': 'interchange.export'},
                           op='provider.inspect', read=True)
    rows = {row['provider_id']: row for row in result['providers']}
    assert rows['open_headless']['available'] and rows['open_headless']['supports_operation']
    assert rows['mapgis67']['tier'] == c.native['tier'] and not rows['mapgis10']['available']
    assert len(rows['mapgis67']['capabilities']['com']['exact_library_matches']) == 6


async def topology(c):
    path = _vector(c.root, 'polygons.wp', [box(0, 0, 8, 6), box(8, 0, 16, 6)])
    result = await c.route('mapgis_read_wp_arcs', _binding(path), op='topology.arcs', read=True)
    assert result['topology']['shared_arcs'] >= 1 and result['topology']['regions'] == 2


async def modern_crs(c):
    features = _features([Point(40_500_100, 2_800_000)])
    template = {'name': 'cgcs.wt', 'map_scale': 50_000, 'crs': _frame(4528)}
    result = await c.route('mapgis_prepare_modern_crs', {'features': features, 'template': template,
                           'request_id': 'modern-crs'}, op='crs.modern_create')
    assert result['state'] == 'PREPARED'
    published = await _commit(c, 'modern-crs')
    path = Path(published['output'])
    contract, crs = read_crs_sidecar(path)
    assert crs.to_epsg() == 4528 and contract['false_easting_m'] == 40_500_000
    assert contract['file_sha256'] == file_sha256(path)


async def reject_false_easting(c):
    plain = _vector(c.root, 'plain.wt', [Point(500_500, 2_800_000)], epsg=4549)
    zone = _vector(c.root, 'zone.wt', [Point(40_500_500, 2_800_000)], epsg=4528)
    baseline = await _audit(c, [zone], reference_crs='EPSG:4528')
    assert baseline['summary']['passed']
    await _audit(c, [zone, plain], reference_crs='EPSG:4528',
                 expected_error='AUDIT_FALSE_EASTING')


async def legend_lvs(c):
    path = _vector(c.root, 'legend.wp', [box(0, 0, 8, 6), box(8, 0, 16, 6)],
                   properties=[{'CODE': 'Q', 'COLOR_NO': 7}, {'CODE': 'Q-local', 'COLOR_NO': 8}])
    result = await _audit(c, [path], legend_items=[{'code': 'Q', 'color_no': 9, 'color_code': 'Y55',
                          'rgb': [255, 255, 115]}, {'code': 'K', 'color_no': 10}],
                          options={'exceptions': [{'file': path.name, 'code': 'Q-local', 'reason': 'Synthetic legend exception'}]})
    checks = {row['rule_id'] for row in result['violations']}
    assert {'LVS_MISSING_LEGEND', 'LVS_UNUSED_LEGEND', 'LVS_STANDARD_DEVIATION'} <= checks
    assert not any(row.get('code') == 'Q-local' and row['rule_id'] == 'LVS_STANDARD_DEVIATION'
                   for row in result['violations'])


async def labels(c):
    path = _labels(c.root)
    result = await c.route('mapgis_place_labels', {
        **_binding(path), 'solver': 'simulated_annealing', 'options': {'locked_ids': [1], 'seed': SEED, 'iterations': 1200}},
        op='labels.place', read=True)
    assert result['overlaps_before'] >= 3 and result['overlaps_after'] < result['overlaps_before']
    assert result['positions'][0]['dx'] == result['positions'][0]['dy'] == 0.
    assert all(row['record_id'] != 1 for report in result['patch_plan'] for row in report['parameters']['updates'])


async def preview(c):
    unit = _vector(c.root, 'unit.wp', [box(0, 0, 60, 48)])
    fault = _vector(c.root, 'fault.wl', [LineString([(3, 3), (45, 30)])])
    for label, layers in [('base', [unit]), ('changed', [unit, fault])]:
        result = await c.route('mapgis_render_preview', {'files': [_binding(path) for path in layers],
                               'output_png': f'work/{label}.png', 'options': {'width_px': 600, 'height_px': 480, 'dpi': 300}},
                               op='preview.render')
        raw = Path(result['path']).read_bytes()
        assert struct.unpack_from('>IIB', raw, raw.index(b'pHYs') + 4) == (11811, 11811, 1)
    result = await c.route('mapgis_render_preview', {'files': [], 'output_png': 'work/diff.png',
                           'options': {'compare_png': ['work/base.png', 'work/changed.png']}}, op='preview.render')
    assert 0 < result['changed_pixel_ratio'] < .5 and Path(result['absolute_difference_png']).is_file()


async def fdr(c):
    marker = 'synthetic-private-grid-id'
    report = {'violations': [{'coords': [40_500_001.25, 3_250_009.5], 'file': 'C:/Private/draft.wp',
                             'attributes': {'SITE': marker}}]}
    archive = c.root / 'work' / 'exchange.zip'
    result = await c.route('mapgis_export_fdr', {'output_zip': str(archive),
                           'options': {'drc_report': report, 'redaction': {'sensitive_fields': ['site'],
                                       'coordinate_offset': [40_500_000, 3_250_000]}}}, op='fdr.export')
    assert result['verified'] and verify_fdr_bundle(archive)['structured_redaction_checked']
    with ZipFile(archive) as package:
        content = b''.join(package.read(name) for name in package.namelist())
        for token in (marker.encode(), b'C:/Private', b'40500001.25', b'3250009.5'):
            assert token not in content
        item = json.loads(package.read('drc_report.json'))['violations'][0]
        assert item['coords'] == [1.25, 9.5] and item['attributes']['SITE'] == '[REDACTED]'


def _neighbours(root):
    x, y = Transformer.from_crs(4490, 4528, always_xy=True).transform(120, 26.01)
    indices = map_sheets('index', {'points': [(119.99, 26.01), (120.01, 26.01)], 'scale': 50_000},
                         _frame(4490))['sheets']

    def layer(name, geometry, codes):
        return _binding(_vector(root, name, geometry, properties=[{'ID': i, 'UNIT': v} for i, v in enumerate(codes, 1)],
                                epsg=4528, scale=50_000))

    west = {'sheet_id': indices[0]['code'], 'layers': {
        'wt': layer('west.wt', [Point(x-10, y-20)], ['A']),
        'wl': layer('west.wl', [LineString([(x-100, y+20), (x-.2, y+20)]),
                               LineString([(x-100, y+40), (x-.1, y+40)])], ['A', 'B']),
        'wp': layer('west.wp', [box(x-120, y-30, x-.4, y-10)], ['A']),
    }}
    east = {'sheet_id': indices[1]['code'], 'layers': {
        'wt': layer('east.wt', [Point(x+10, y-20)], ['Z']),
        'wl': layer('east.wl', [LineString([(x+.6, y+20.1), (x+100, y+20.1)]),
                               LineString([(x+.1, y+40), (x+100, y+40)])], ['A', 'Z']),
        'wp': layer('east.wp', [box(x+.4, y-29.9, x+120, y-9.9)], ['A']),
    }}
    return west, east, x, y


async def dbs_schema(c):
    left, right, x, y = _neighbours(c.root)
    args = {'sheets': [left, right]}
    result = await c.route('mapgis_dbs_multisheet_validate', args, op='dbs.multisheet_validate', read=True)
    assert result['crs']['false_easting_m'] == 40_500_000 and result['adjacency'][0]['axis'] == 'x'
    bad = _vector(c.root, 'bad.wt', [Point(x + 1, y)], properties=[{'ID': 1, 'OTHER': 'Z'}],
                  epsg=4528, scale=50_000)
    changed = right | {'layers': right['layers'] | {'wt': _binding(bad)}}
    await c.route('mapgis_dbs_multisheet_validate', {'sheets': [left, changed]}, op='dbs.multisheet_validate',
                  read=True, expected_error='DBS_SCHEMA')


async def dbs_seams(c):
    left, right, x, y = _neighbours(c.root)
    args = {'left_sheet': left, 'right_sheet': right, 'boundary_x_or_y': 'x', 'tolerance': 1.,
            'key_fields': ['UNIT'], 'policy': 'midpoint_snap_matching_attrs'}
    receipt = await c.route('mapgis_dbs_multisheet_edge_match', args, op='dbs.multisheet_edge_match')
    assert len(receipt['branches']) == 2 and sum(pair.get('snapped', False) for pair in receipt['matches']) == 2
    assert sum(row['code'] == 'EDGE_ATTR_CONFLICT' for row in receipt['findings']) == 1
    head = read_native(Path(receipt['branches'][0]['layers']['wl']['file']))[1]
    assert math.dist(head.geom[0].coords[-1], (x + .2, y + 20.05)) < 1e-7
    assert file_sha256(left['layers']['wl']['file']) == left['layers']['wl']['expected_sha256']


def _drillholes(root, x=40_500_000, y=2_800_000, include_assay=False):
    datasets = {'collar': [{'hole_id': 'ZK01', 'easting': x + 100, 'northing': y, 'elevation': 150}],
                'survey': [{'hole_id': 'ZK01', 'md': 0, 'inclination': 0, 'azimuth': 90},
                           {'hole_id': 'ZK01', 'md': 100, 'inclination': 50, 'azimuth': 90}],
                'lithology': [{'hole_id': 'ZK01', 'top': 0, 'base': 100, 'unit': '灰岩'}]}
    if include_assay:
        datasets['assay'] = [{'hole_id': 'ZK01', 'top': 0, 'base': 100, 'analyte': 'Cu', 'value': .5, 'unit': '%'}]
    paths = {}
    for name, records in datasets.items():
        path = root / f'{name}.json'
        path.write_text(json_text(records), encoding='utf8')
        paths[name] = _binding(path)
    return paths


async def _handoff(c, sources, target, **options):
    result = await c.route('mapgis_interchange_export', {'sources': sources, 'target': target,
                           'output_dir': f'deliverables/{target}', **options}, op='interchange.export')
    audit = await c.route('mapgis_interchange_verify', {'package_dir': result['package_dir']},
                          op='interchange.verify', read=True)
    assert result['verified'] and audit['verified'] and result['layers'] == audit['layers']
    return audit


async def arcgis(c):
    unit = _vector(c.root, 'unit.wp', [box(40_500_010, 2_800_000, 40_500_065, 2_800_040)],
                   properties=[{'CODE': 'Q', '岩性': '灰岩'}], epsg=4528)
    receipt = await _handoff(c, [_binding(unit)], 'arcgis_pro')
    assert 'layers.gpkg' in receipt['verified_outputs'] and receipt['feature_count'] == 1
    assert receipt['retention_matrix']['topology']['status'] == 'PARTIAL'


async def surpac_corel(c):
    layer = _vector(c.root, 'marks.wt', [Point(40_500_100, 2_800_000)], texts=['钻孔甲'], epsg=4528)
    bound = [_binding(layer)]
    surpac = await _handoff(c, bound, 'surpac', metadata={'drillholes': _drillholes(c.root), 'default_z': 150})
    assert {'mapgis.str', 'collar.csv', 'survey.csv', 'lithology.csv'} <= set(surpac['verified_outputs'])
    corel = await _handoff(c, bound, 'coreldraw')
    assert {'mapgis.dxf', 'preview.svg', 'dz-t-0179-2025-colors.json'} <= set(corel['verified_outputs'])
    assert corel['feature_count'] == surpac['feature_count'] == 1


def _source(role, path):
    return {'role': role, 'source_file': str(path), 'expected_sha256': file_sha256(path)}


def _upstream(role, node, key='file'):
    return {'role': role, 'from_node': node, 'output_key': key}


def _plan(nodes, plan_id='bench'):
    return {'schema_version': 'mapgis-plan/v1', 'plan_id': plan_id, 'crs': 'LOCAL', 'unit': 'local',
            'scale': 1000, 'seed': SEED, 'nodes': nodes}


async def _run_plan(c, action, **args):
    return await c.call('mapgis_run_plan', {'action': action, 'arguments': args})


async def six_step_plan(c):
    files = [_vector(c.root, 'unit.wp', [box(0, 0, 60, 48)]),
             _vector(c.root, 'fault.wl', [LineString([(0, 0), (50, 40)])]), _labels(c.root)]
    copies = [{'id': f'copy-{kind}', 'op': 'work.copy', 'inputs': [_source('file', path)]}
              for kind, path in zip(('wp', 'wl', 'wt'), files, strict=True)]
    layers = [_upstream('layer', node['id']) for node in copies]
    nodes = [*copies, {'id': 'labels', 'op': 'labels.place', 'inputs': [_upstream('annotation', 'copy-wt')],
                       'params': {'locked_ids': [1]}},
             {'id': 'audit', 'op': 'drc.audit', 'inputs': layers},
             {'id': 'preview', 'op': 'preview.render', 'inputs': [*layers, _upstream('drc_report', 'audit', 'report')],
              'params': {'width_px': 600, 'height_px': 480, 'dpi': 300}}]
    spec = _plan(nodes)
    checked = await _run_plan(c, 'plan', spec=spec)
    result = await _run_plan(c, 'apply', spec=spec, expected_epoch=0)
    assert result['status'] == 'COMPLETE' and result['executed_nodes'] == checked['topological_order']
    assert len(result['completed']) == 6
    preview = result['completed']['preview']
    assert Path(preview['paths']['preview']).read_bytes()[:8] == b'\x89PNG\r\n\x1a\n'
    assert file_sha256(preview['paths']['preview']) == preview['outputs']['preview']['sha256']


async def resume_cas(c):
    path = _points(c.root)
    original = file_sha256(path)
    nodes = [{'id': 'head', 'op': 'work.copy', 'inputs': [_source('file', path)]},
             {'id': 'tail', 'op': 'work.copy', 'inputs': [_upstream('file', 'head')]}]
    interrupted = await _run_plan(c, 'apply', spec=_plan(nodes), fault_inject_node='tail', expected_epoch=0)
    assert interrupted['status'] == 'INTERRUPTED' and interrupted['interruption']['node'] == 'tail'
    head = interrupted['completed']['head']
    assert head['outputs']['file']['sha256'] == original
    path.write_bytes(b'test-only input mutation after sealed checkpoint')
    result = await _run_plan(c, 'resume', plan_id='bench', expected_epoch=0)
    assert result['status'] == 'COMPLETE' and result['reused_nodes'] == ['head']
    assert result['executed_nodes'] == ['tail'] and result['completed']['tail']['outputs']['file']['sha256'] == original


async def ordered_cache(c):
    a = _vector(c.root, 'left.wp', [box(0, 0, 24, 24)])
    b = _vector(c.root, 'right.wp', [box(12, 8, 36, 32)])
    inputs = [_source('layer', a), _source('layer', b)]
    node = {'id': 'preview', 'op': 'preview.render', 'inputs': inputs, 'params': {'width_px': 600, 'height_px': 480}}
    spec = _plan([node])
    initial = await _run_plan(c, 'apply', spec=spec, expected_epoch=0)
    same = await _run_plan(c, 'plan', spec=spec)
    changed = _plan([node | {'inputs': inputs[::-1]}])
    receipt = await _run_plan(c, 'plan', spec=changed)
    assert same['nodes'][0]['cache_hit'] and not receipt['nodes'][0]['cache_hit']
    assert same['nodes'][0]['cache_key'] != receipt['nodes'][0]['cache_key']
    rerun = await _run_plan(c, 'apply', spec=changed, expected_epoch=0)
    assert rerun['executed_nodes'] == ['preview'] and not rerun['reused_nodes']
    assert rerun['completed']['preview']['outputs']['preview']['sha256'] != initial['completed']['preview']['outputs']['preview']['sha256']


async def import_mpj(c):
    paths = [_vector(c.root, 'unit.wp', [box(0, 0, 8, 6)]),
             _vector(c.root, 'fault.wl', [LineString([(0, 0), (8, 6)])]), _labels(c.root)]
    request = 'native-project'
    result = await c.route('mapgis_prepare_native_project', {'layers': [_binding(path) for path in paths],
                           'parameters': {'name': 'bench.mpj', 'coordinate_frame': 'native'}, 'request_id': request},
                           op='project.new')
    assert result['state'] == 'PREPARED'
    project = await _commit(c, request)
    document = await _run_plan(c, 'import_mpj', mpj_path=project['output'])
    first = [row['inputs'][0] for row in document['nodes'][:3]]
    assert [Path(row['source_file']).suffix for row in first] == ['.wp', '.wl', '.wt']
    assert [row['expected_sha256'] for row in first] == [file_sha256(path) for path in paths]
    assert [row['op'] for row in document['nodes'][-2:]] == ['drc.audit', 'preview.render']


async def drillhole_section(c):
    sources = _drillholes(c.root, include_assay=True)
    frame = {'crs': _frame(4528), 'vertical_unit': 'metre', 'vertical_datum': 'Synthetic',
             'azimuth_reference': 'grid_north', 'inclination_reference': 'vertical_down'}
    params = {'title': 'A-A 剖面', 'section': {'start': [40_500_000, 2_800_000], 'end': [40_500_350, 2_800_000],
              'max_crossline_distance': 20}, 'horizontal_scale': 5000, 'vertical_exaggeration': 2,
              'lithology_styles': {'灰岩': {'fill_color': 3}}, 'attitudes': [{'dip_direction': 60, 'dip': 40}]}
    request = 'section'
    initial = await c.route('mapgis_prepare_drillhole_workflow', {'sources': sources, 'coordinate_frame': frame,
                            'parameters': params, 'request_id': request}, op='section.drillhole_workflow')
    assert initial['state'] == 'PREPARED'
    result = await _commit(c, request)
    report = _read_json(result['output'])
    assert set(report['projects']) == {'section', 'columns'}
    for project in report['projects'].values():
        assert len(project['layers']) == 3 and all(layer['active'] > 0 for layer in project['layers'])
        assert file_sha256(Path(result['output']).parent / project['file']) == project['sha256']
    attitude, = report['attitudes']
    tan_apparent = math.tan(math.radians(40)) * math.cos(math.radians(90 - 60))
    assert abs(attitude['displayed_apparent_dip_degrees'] - math.degrees(math.atan(2 * tan_apparent))) < 1e-9
    ledger = (Path(result['output']).parent / 'exchange' / 'projected_section.csv').read_text(encoding='utf-8-sig')
    assert 'ZK01' in ledger and '100.0' in ledger


async def repair_wt(c):
    original = _vector(c.root, 'survey.wt', [Point(700000, 2880000), Point(700100, 2880200)], crs=TM,
                       texts=['测点 A', '测点 B'], scale=50_000)
    blob, _, sections = read_native(original)
    broken = bytearray(blob)
    table, = struct.unpack_from('<i', blob, 12)
    struct.pack_into('<i', broken, 12, table + 1)
    struct.pack_into('<d', broken, 143, float('nan'))
    for index, (offset, size) in enumerate(sections):
        if size:
            struct.pack_into('<i', broken, table + 10 * index, offset + 8)
    path = c.root / 'work' / 'anomalous.wt'
    path.write_bytes(broken)
    result = await c.route('mapgis_normalize_survey_wt', {
        'source_path': str(path), 'output_path': 'deliverables/normalized.wt', 'expected_sha256': sha(broken),
        'map_scale': 50_000}, op='native.survey_wt_normalize')
    assert result['native_reopen']['active'] == 2 and result['section_offset_delta'] == -8
    assert (c.root / 'deliverables' / 'normalized.wt').read_bytes() == blob and path.read_bytes() == broken


async def ellipsoid_com(c):
    scope = {'library': 'proj60.dll', 'class_name': 'EllipPara'}
    if c.surface == 'legacy':
        info = await c.call('mapgis_describe_sdk', scope)
    else:
        info = await c.call('mapgis_sdk_flow', {'action': 'describe', 'arguments': scope})
    assert 'GetEllipParaById' in {row['name'] for row in info['methods']}
    parameters = {'library': 'proj60.dll', 'inputs': [], 'outputs': [],
                  'objects': [{'id': 'ellip', 'class': 'EllipPara'}], 'steps': [
                      {'id': f'read_{index}', 'target': 'ellip', 'member': 'GetEllipParaById', 'kind': 'method',
                       'arguments': [{'value': index}, {'out': f'ellipsoid_{index}'}]} for index in range(1, 11)]}
    plan = await c.route('mapgis_prepare_sdk_flow', {'parameters': parameters, 'request_id': 'ellipsoid'},
                         op='native.sdk_flow')
    assert plan['state'] == 'PREPARED'
    result = await _commit(c, 'ellipsoid')
    values = _read_json(result['output'])['values']
    axes = [row['value'] for row in values if row['id'].startswith('ellipsoid_')]
    assert len(axes) == 10 and all(len(row) == 4 for row in axes)
    assert any(abs(row[0] - 6_378_245.) < 2 for row in axes)


async def l2_projection(c):
    block = struct.pack('<b8f', -3, 1.25, -2.5, 3, 4, 9999, 6, 7, 50_000)
    result = await c.route('mapgis_probe_native_l2', {'format': '4x', 'parameter_block_hex': block.hex()},
                           op='native.l2_probe', read=True)
    assert result['library'] == 'read_4x.dll' and result['declared_slots'] == 29
    assert {row['index']: row['value'] for row in result['items']} == {
        0: -3, 2: 1.25, 3: -2.5, 4: 3, 5: 4, 6: 6, 7: 7, 8: 50_000}
    assert result['input_sha256'] == sha(block) and result['input_preserved'] and result['private_desktop']


async def stale_epoch(c):
    source = _points(c.root)
    receipt = await _patch(c, source, request='fenced')
    if c.surface == 'legacy':
        epoch = Bridge(c.root).advance_epoch()  # Simulate an independent new publisher before the MCP call.
    else:
        epoch = (await c.call('mapgis_tx_manage', {'action': 'advance_epoch'}))['epoch']
    assert epoch == 1
    await _commit(c, 'fenced', expected_error='STALE_PUBLISHER_EPOCH')
    assert not Path(receipt['output']).exists()
    assert (await _commit(c, 'fenced', epoch=1))['state'] == 'APPLIED'


async def window_reopen(c):
    west, east, x, y = _neighbours(c.root)
    result = await c.route('mapgis_dbs_multisheet_build', {'library_id': 'sheets', 'sheets': [west, east],
                           'tolerance': 1.}, op='dbs.multisheet_build')
    assert len(_read_json(result['index'])['sheets']) == 2
    result = await c.route('mapgis_dbs_multisheet_window', {
        'library_id': 'sheets', 'bbox': [x - 25, y - 35, x + 25, y + 30], 'output_dir': 'deliverables/window'},
        op='dbs.multisheet_window')
    assert result['record_counts'] == {'wt': 2, 'wl': 2, 'wp': 2}
    for kind, count in result['record_counts'].items():
        output = Path(result['output_dir']) / f'window.{kind}'
        native = await c.route('mapgis_native_file_info', _binding(output), op='native.info', read=True)
        assert native['mode'] == 'isolated_native_x86' and native['active'] == count
        assert {row['sheet_id'] for row in result['lineage'][kind]} == {west['sheet_id'], east['sheet_id']}


TASKS = (
    Task('B01', 'basic', 'Inspect a native WT', '读取点文件几何属性', ('file.inspect',), inspect_file),
    Task('B02', 'basic', 'Page physical feature IDs', '分页读要素 physical records', ('file.features',), page_features),
    Task('B03', 'basic', 'Copy a native source and guard the original', '安全工作副本', (), work_copy,
         ('mapgis_make_work_copy',), safety_checks=1),
    Task('B04', 'basic', 'Preview and commit an attribute patch', '属性补丁批量字段赋值', ('file.patch', 'attributes.values'),
         patch_and_commit),
    Task('B05', 'basic', 'Compare native vectors by physical record', '比较两个文件差异', ('file.compare',), compare_files),
    Task('B06', 'basic', 'Discover and describe capabilities', '本机能力发现', (), capabilities,
         ('mapgis_environment', 'mapgis_get_capabilities')),
    Task('B07', 'basic', 'Project a CGCS2000 coordinate', '坐标点投影转换', ('projection.point',), projection),
    Task('B08', 'basic', 'Probe the installed ABI tier', 'ABI 分级探测', ('abi.probe',), abi),
    Task('B09', 'basic', 'Inspect generation-specific providers', 'MapGIS Provider 探测', ('provider.inspect',), provider),
    Task('B10', 'basic', 'Read shared WP topology arcs', '面拓扑弧段记录', ('topology.arcs',), topology),
    Task('I01', 'intermediate', 'Publish a 40-zone CGCS2000 sidecar', 'CGCS2000 4528 伴生创建', ('crs.modern_create',), modern_crs),
    Task('I02', 'intermediate', 'Block a 40M false-easting mismatch', '40M 假东距 地质质检', ('drc.audit',),
         reject_false_easting, safety_checks=1),
    Task('I03', 'intermediate', 'Check LVS in both directions with an exception', 'GB/T 958 DZ/T 0179 LVS 图例',
         ('drc.audit',), legend_lvs),
    Task('I04', 'intermediate', 'Place labels with a locked physical ID', 'PFLP 注记避让', ('labels.place',), labels),
    Task('I05', 'intermediate', 'Render 300dpi PNG and pixel differences', '300dpi 预览', ('preview.render',), preview),
    Task('I06', 'intermediate', 'Export and inspect a redacted FDR', 'FDR 脱敏导出', ('fdr.export',), fdr),
    Task('I07', 'intermediate', 'Validate multi-sheet schemas', '多图幅 DBS Schema 校验', ('dbs.multisheet_validate',),
         dbs_schema, safety_checks=1),
    Task('I08', 'intermediate', 'Find seam conflicts and stitch matching edges', '多图幅 接边 缝合',
         ('dbs.multisheet_edge_match',), dbs_seams),
    Task('I09', 'intermediate', 'Export and verify ArcGIS Pro GeoPackage', 'ArcGIS Pro GPKG 交接', ('interchange.export',), arcgis),
    Task('I10', 'intermediate', 'Export and verify Surpac and CorelDRAW', 'Surpac CorelDRAW 跨软件互操作',
         ('interchange.export',), surpac_corel),
    Task('E01', 'expert', 'Execute a six-node MapPlan', 'MapPlan IR 完整编排', (), six_step_plan, required_tool='mapgis_run_plan'),
    Task('E02', 'expert', 'Interrupt MapPlan and resume from sealed CAS', 'MapPlan CAS 断点续跑', (), resume_cas, required_tool='mapgis_run_plan'),
    Task('E03', 'expert', 'Invalidate the ordered layer cache', 'MapPlan 图层顺序缓存', (), ordered_cache, required_tool='mapgis_run_plan'),
    Task('E04', 'expert', 'Import a real MPJ as an ordered MapPlan', 'MPJ 反向导入 MapPlan', (), import_mpj,
         native_route='file', required_tool='mapgis_run_plan'),
    Task('E05', 'expert', 'Publish a minimum-curvature drillhole section', '钻孔 最小曲率剖面',
         ('section.drillhole_workflow', 'geology.section_workflow'), drillhole_section, native_route='file'),
    Task('E06', 'expert', 'Normalize and reopen an anomalous WT', 'MapSuvL67 WT 文件头规范化',
         ('native.survey_wt_normalize',), repair_wt, native_route='file'),
    Task('E07', 'expert', 'Read ellipsoids through the original proj60 COM library', 'proj60 COM 椭球参数',
         ('native.sdk_flow',), ellipsoid_com, native_route='com'),
    Task('E08', 'expert', 'Decode read_4x projection parameters', 'L2 read_4x DLL 投影参数', ('native.l2_probe',),
         l2_projection, native_route='l2'),
    Task('E09', 'expert', 'Fence a stale publisher epoch', '过期 publisher_epoch Fencing', (), stale_epoch,
         ('mapgis_tx_commit', 'mapgis_apply_patch'), safety_checks=1),
    Task('E10', 'expert', 'Clip and reopen a cross-sheet window with BAS60', '跨图幅窗口裁剪',
         ('dbs.multisheet_window',), window_reopen, native_route='file'),
)
