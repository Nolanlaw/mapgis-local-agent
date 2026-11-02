"""Actual BAS60 edits, in an owned process, with independent saved-file readback."""
import json
from pathlib import Path
import sys

import pytest
from shapely.geometry import LineString, Point, mapping

from mapgis_agent.core import Bridge, read_native, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import native_file_info, prepare_native_operation
from mapgis_agent.workspace import NativeWorkspaces

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {'kind': 'local', 'name': 'native analytic editing grid', 'axis_order': 'xy',
         'unit': 'metre', 'reference_sha256': '0' * 64}


@pytest.fixture
def native_case(tmp_path):
    if sys.platform != 'win32' or not (ROOT / 'config.local.json').is_file():
        pytest.skip('configured Windows native backend required')
    config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf8'))
    if not config.get('native_backend_legacy_dep_compatibility'):
        pytest.skip('explicit legacy host opt-in required')
    (tmp_path / 'config.local.json').write_text(json.dumps({k: config[k] for k in
        ('mapgis_installation', 'native_backend_legacy_dep_compatibility')}), encoding='utf8')
    return Bridge(tmp_path)


def create(bridge, name, geometry, crs, scale, second=None):
    geoms = [geometry] + ([second] if second is not None else [])
    prepare_creation(bridge, {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': str(i), 'geometry': mapping(g), 'properties': {'ID': 7, '地层': 'T2d'}}
        for i, g in enumerate(geoms, 1)]},
        {'name': name, 'crs': crs, 'map_scale': scale}, 'create')
    return Path(bridge.apply('create')['output'])


@pytest.mark.parametrize(('crs', 'scale', 'origin'), [(LOCAL, 1000, 0),
    ({'definition': '+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs', 'axis_order': 'xy'}, 50000, 500000)])
def test_native_line_vertex_batch_preserves_identity_and_has_fresh_reopen(native_case, crs, scale, origin):
    bridge = native_case
    source = create(bridge, 'lines.wl', LineString([(origin, 0), (origin + 10, 0)]), crs, scale,
                    LineString([(origin + 20, 0), (origin + 30, 0)]))
    original = source.read_bytes()
    coordinates = [[origin, 0], [origin + 4, 3], [origin + 8, 3], [origin + 10, 0]]
    result = prepare_native_operation(bridge, 'line.replace', str(source), sha(original),
        {'updates': [{'record_id': 1, 'coordinates': coordinates}]}, 'vertices')
    output = Path(bridge.apply('vertices')['output'])
    reader = read_native(output)[1]
    actual = list(reader.geom[0].coords)
    assert len(actual) == len(coordinates)
    for a, b in zip(actual, coordinates):
        assert a == pytest.approx(tuple(b), abs=1e-9, rel=0)
    assert reader.geom[1].equals_exact(read_native(source)[1].geom[1], 0)
    assert reader.data['ID'].tolist() == [7, 7] and reader.data['地层'].tolist() == ['T2d', 'T2d']
    assert result['comparison']['counts']['changed'] == 1
    assert result['native_execution']['pid'] != result['native_reopen']['pid']
    assert native_file_info(bridge.root, str(output), sha(output.read_bytes()))['active'] == 2
    assert source.read_bytes() == original


def test_native_point_move_uses_map_coordinates_on_a_scaled_projected_file(native_case):
    bridge = native_case
    source = create(bridge, 'point.wt', Point(500000, 3320000),
                    {'definition': '+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs', 'axis_order': 'xy'}, 50000)
    original = source.read_bytes()
    prepare_native_operation(bridge, 'point.move', str(source), sha(original),
        {'updates': [{'record_id': 1, 'x': 500012.5, 'y': 3320030.25}]}, 'move')
    output = Path(bridge.apply('move')['output'])
    assert tuple(read_native(output)[1].geom[0].coords[0]) == pytest.approx((500012.5, 3320030.25), abs=1e-9, rel=0)
    assert source.read_bytes() == original


def test_native_line_workspace_keeps_dirty_edit_after_child_loss(native_case):
    bridge = native_case
    source = create(bridge, 'buffer.wl', LineString([(0, 0), (10, 0)]), LOCAL, 1000)
    original = source.read_bytes()
    registry = NativeWorkspaces(bridge)
    try:
        opened = registry.execute('open', 'lines', {'source': str(source), 'expected_sha256': sha(original)})
        changed = registry.execute('edit', 'lines', {'operation': 'line.replace', 'parameters': {
            'updates': [{'record_id': 1, 'coordinates': [[0, 0], [5, 2], [10, 0]]}]}}, 0, 'edit')
        assert changed['dirty'] and changed['native_pid'] == opened['native_pid']
        child = registry.buffers['lines'].process
        child.kill()
        child.wait(timeout=5)
        recovered = registry.execute('recover', 'lines', {}, 1)
        assert recovered['dirty'] and recovered['revision'] == 1 and recovered['native_pid'] != opened['native_pid']
        assert list(read_native(Path(recovered['checkpoint']))[1].geom[0].coords) == [(0, 0), (5, 2), (10, 0)]
        shrunk = registry.execute('edit', 'lines', {'operation': 'line.replace', 'parameters': {
            'updates': [{'record_id': 1, 'coordinates': [[0, 0], [10, 2]]}]}}, 1, 'remove-vertex')
        assert shrunk['revision'] == 2 and shrunk['dirty']
        assert list(read_native(Path(shrunk['checkpoint']))[1].geom[0].coords) == [(0, 0), (10, 2)]
        saved = registry.execute('save', 'lines', {}, 2, 'save')
        assert not saved['dirty']
        registry.execute('close', 'lines', {}, 2)
        assert source.read_bytes() == original
    finally:
        registry.close_all()
