import asyncio
import json
from pathlib import Path

from pyproj import CRS, Transformer
import pytest
from shapely.geometry import LineString, Point, box, mapping

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.creation import prepare_modern_crs_creation
from mapgis_agent.crs_contract import make_crs_sidecar, read_crs_sidecar, sidecar_path
from mapgis_agent.dbs_multisheet import (
    build_multisheet_library, edge_match_sheets, export_multisheet_window, validate_multisheet_schema,
)
from mapgis_agent.map_sheets import map_sheets
from mapgis_agent.native_backend import native_file_info
from mapgis_agent.server import build_server
from mapgis_agent.topology import validate_wp_topology
from test_native_raster import native_settings


def _layer(bridge, name, features, *, epsg=4528, code_field='岩性'):
    spec = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': str(i), 'geometry': mapping(geometry),
         'properties': {'ID': i, code_field: code}} for i, (geometry, code) in enumerate(features, 1)]}
    template = {'name': name, 'map_scale': 50000, 'crs': {'definition': f'EPSG:{epsg}', 'axis_order': 'xy'}}
    request = name.replace('.', '-')
    prepare_modern_crs_creation(bridge, spec, template, request)
    path = Path(bridge.apply(request)['output'])
    return {'file': str(path), 'expected_sha256': sha(path.read_bytes())}


@pytest.fixture
def neighbours(tmp_path):
    bridge = Bridge(tmp_path)
    x, y = Transformer.from_crs(4490, 4528, always_xy=True).transform(120., 26.01)
    codes = map_sheets('index', {'points': [(119.99, 26.01), (120.01, 26.01)], 'scale': 50000},
                       {'definition': 'EPSG:4490', 'axis_order': 'xy'})['sheets']
    west = {'sheet_id': codes[0]['code'], 'layers': {
        'wt': _layer(bridge, 'west.wt', [(Point(x-10, y-20), 'A')]),
        'wl': _layer(bridge, 'west.wl', [(LineString([(x-120, y+20), (x-.2, y+20)]), 'A'),
                                      (LineString([(x-100, y+40), (x-.1, y+40)]), 'B'),
                                      (LineString([(x-100, y+50), (x-.1, y+50)]), 'C')]),
        'wp': _layer(bridge, 'west.wp', [(box(x-120, y-30, x-.4, y-10), 'A'),
                                      (box(x-120, y+65, x-.1, y+75), 'B')]),
    }}
    east = {'sheet_id': codes[1]['code'], 'layers': {
        'wt': _layer(bridge, 'east.wt', [(Point(x+10, y-20), 'Z')]),
        'wl': _layer(bridge, 'east.wl', [(LineString([(x+.6, y+20.1), (x+120, y+20.1)]), 'A'),
                                      (LineString([(x+.1, y+40), (x+100, y+40)]), 'Z')]),
        'wp': _layer(bridge, 'east.wp', [(box(x+.4, y-29.9, x+120, y-9.9), 'A'),
                                      (box(x+.1, y+65, x+120, y+75), 'Z')]),
    }}
    return bridge, west, east, x, y


def test_strict_edges_report_gaps_conflicts_and_one_sided_contacts(neighbours):
    _, left, right, x, y = neighbours
    validated = validate_multisheet_schema([left, right])
    assert validated['crs']['false_easting_m'] == 40500000 and validated['scale'] == 50000
    assert validated['adjacency'][0]['axis'] == 'x'
    assert all({'ID', '岩性'} <= {field['name'] for field in schema}
               for schema in validated['schemas'].values())
    assert {'面积', '周长'} <= {field['name'] for field in validated['schemas']['wp']}
    result = edge_match_sheets(left, right, boundary_x_or_y={'x': x}, tolerance=1, key_fields=['岩性'])
    by_code = {code: [row for row in result['findings'] if row['code'] == code]
               for code in ('EDGE_GAP', 'EDGE_ATTR_CONFLICT', 'EDGE_UNMATCHED')}
    assert {key: len(rows) for key, rows in by_code.items()} == {
        'EDGE_GAP': 4, 'EDGE_ATTR_CONFLICT': 2, 'EDGE_UNMATCHED': 1}
    assert {row['layer'] for row in by_code['EDGE_ATTR_CONFLICT']} == {'wp', 'wl'}
    assert all(row['key_conflicts'] == ['岩性'] for row in by_code['EDGE_ATTR_CONFLICT'])
    assert by_code['EDGE_UNMATCHED'][0]['edge']['coordinates'][0][1] == y+50
    assert result['branches'] == [] and [row['left']['record_id'] for row in result['matches']] == [1, 2, 1, 2]
    for sheet in (left, right):
        for binding in sheet['layers'].values():
            assert sha(Path(binding['file']).read_bytes()) == binding['expected_sha256']


def test_schema_rejects_disagreeing_dictionary_false_easting_and_nonadjacency(neighbours):
    bridge, left, right, x, y = neighbours
    incompatible = _layer(bridge, 'dictionary.wt', [(Point(x+1, y), 'A')], code_field='MAP_UNIT')
    with pytest.raises(BridgeError, match='DBS_SCHEMA'):
        validate_multisheet_schema([left, right | {'layers': right['layers'] | {'wt': incompatible}}])
    plain = _layer(bridge, 'offset.wt', [(Point(500001, y), 'A')], epsg=4549)
    with pytest.raises(BridgeError, match='DBS_FALSE_EASTING'):
        validate_multisheet_schema([left, right | {'layers': right['layers'] | {'wt': plain}}])
    remote = map_sheets('index', {'points': [(121., 26.01)], 'scale': 50000},
                        {'definition': 'EPSG:4490', 'axis_order': 'xy'})['sheets'][0]['code']
    with pytest.raises(BridgeError, match='DBS_ADJACENCY'):
        validate_multisheet_schema([left, right | {'sheet_id': remote}])
    with pytest.raises(BridgeError, match='DBS_BOUNDARY'):
        edge_match_sheets(left, right, boundary_x_or_y='y', tolerance=1)


def test_schema_rejects_full_twenty_byte_gbk_field_without_terminator(neighbours):
    bridge, left, right, _, _ = neighbours
    path = Path(left['layers']['wt']['file'])
    original, _, sections = read_native(path)
    raw = bytearray(original)
    start = sections[2][0]+348
    raw[start:start+20] = ('地质'*5).encode('gbk')
    file = bridge.root/'work/oversize-field.wt'
    file.write_bytes(raw)
    sidecar_path(file).write_text(json.dumps(make_crs_sidecar(file.name, raw, CRS.from_epsg(4528))), encoding='utf-8')
    with pytest.raises(BridgeError, match='DBS_FIELD_GBK'):
        validate_multisheet_schema([left | {'layers': left['layers'] | {
            'wt': {'file': str(file), 'expected_sha256': sha(raw)}}}, right])


def test_midpoint_branches_ledger_and_native_window_export(neighbours):
    bridge, left, right, x, y = neighbours
    native_settings(bridge.root)
    options = {'boundary_x_or_y': 'x', 'tolerance': 1, 'key_fields': ['岩性'],
               'policy': 'midpoint_snap_matching_attrs', 'root': bridge.root}
    stitched = edge_match_sheets(left, right, **options)
    branches = stitched['branches']
    assert len(branches) == 2 and sum(pair.get('snapped', False) for pair in stitched['matches']) == 2
    assert edge_match_sheets(left, right, **options)['reconciled']
    after = edge_match_sheets(*branches, boundary_x_or_y='x', tolerance=1, key_fields=['岩性'])
    good = [pair for pair in after['matches'] if not pair['attribute_conflicts']]
    assert len(good) == 2 and all(pair['gap_m'] <= 1e-7 for pair in good)
    assert sum(row['code'] == 'EDGE_ATTR_CONFLICT' for row in after['findings']) == 2
    rebuilt = read_native(Path(branches[0]['layers']['wl']['file']))[1]
    assert rebuilt.geom[0].coords[-1] == pytest.approx((x+.2, y+20.05), abs=1e-8, rel=0)
    assert read_native(Path(branches[1]['layers']['wp']['file']))[1].data['岩性'].tolist() == ['A', 'Z']
    for original, branch in zip((left, right), branches):
        for kind in ('wl', 'wp'):
            source = Path(original['layers'][kind]['file'])
            assert sha(source.read_bytes()) == original['layers'][kind]['expected_sha256']
            old = read_native(source)[1]
            updated = read_native(Path(branch['layers'][kind]['file']))[1]
            assert old.geom[1].equals(updated.geom[1])  # Conflicting row remains unchanged.
            assert old.data['岩性'].tolist() == updated.data['岩性'].tolist()
    index = build_multisheet_library(bridge.root, 'sheets', branches, tolerance=1)
    ledger = json.loads(Path(index['index']).read_text(encoding='utf-8'))
    assert len(ledger['sheets']) == 2 and len(ledger['seams']) == 1
    assert ledger['sheets'][0]['layers']['wp']['sidecar_sha256']
    assert ledger['sheets'][1]['crs_contracts']['wt']['declared_epsg'] == 4528
    window = [x-25, y-35, x+25, y+30]
    output = 'deliverables/dbs-window'
    exported = export_multisheet_window(bridge.root, 'sheets', window, output)
    assert exported['record_counts'] == {'wt': 2, 'wl': 2, 'wp': 2}
    assert export_multisheet_window(bridge.root, 'sheets', window, output)['reconciled']
    for kind, count in exported['record_counts'].items():
        file = Path(exported['output_dir'])/f'window.{kind}'
        blob, reader, _ = read_native(file)
        assert native_file_info(bridge.root, str(file), sha(blob))['active'] == count
        assert read_crs_sidecar(file)[0]['false_easting_m'] == 40500000
        assert all(box(*window).covers(geometry) for geometry in reader.geom)
        assert {row['sheet_id'] for row in exported['lineage'][kind]} == {left['sheet_id'], right['sheet_id']}
    assert validate_wp_topology(Path(exported['output_dir'])/'window.wp')['shared_arcs'] > 0
    stale = sidecar_path(branches[0]['layers']['wt']['file'])
    stale.write_bytes(stale.read_bytes()+b'\n')
    with pytest.raises(BridgeError, match='CRS_SIDECAR_STALE'):
        export_multisheet_window(bridge.root, 'sheets', window, 'deliverables/second-window')


def test_dbs_build_and_empty_window_via_compact_execute(neighbours):
    bridge, left, right, x, y = neighbours
    server = build_server(bridge.root, 'compact')

    async def run():
        response = await server.call_tool('mapgis_execute', {'op_id': 'dbs.multisheet_build',
            'arguments': {'library_id': 'via-mcp', 'sheets': [left, right]}})
        assert Path(response['index']).is_file()
        result = await server.call_tool('mapgis_execute', {'op_id': 'dbs.multisheet_window',
            'arguments': {'library_id': 'via-mcp', 'bbox': [x-20, y+100, x+20, y+120],
                          'output_dir': 'deliverables/empty-window'}})
        assert result['record_counts'] == {'wt': 0, 'wl': 0, 'wp': 0}
        assert result['files'] == []
        assert (Path(result['output_dir'])/'dbs-receipt.json').is_file()

    asyncio.run(run())
