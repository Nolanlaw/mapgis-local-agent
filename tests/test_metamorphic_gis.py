"""Seeded GIS relations with explicit conditions and order-sensitive counterexamples."""
import math
from pathlib import Path
import random

import numpy as np
from pyproj import CRS, Transformer
import pytest
from shapely import union_all
from shapely.affinity import rotate, translate
from shapely.geometry import LineString, Point, Polygon, box, mapping

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.creation import prepare_creation, prepare_modern_crs_creation
from mapgis_agent.crs_contract import read_crs_sidecar
from mapgis_agent.drc import run_drc_audit
from mapgis_agent.map_plan import compute_cache_key, parse_spec
from mapgis_agent.topology import validate_wp_topology
from mapgis_agent.vector import prepare_vector_operation
from mapgis_agent.visual_preview import _read_layers, _read_png, _render, render_diff_heatmap


SEED = 20261127
LOCAL = {'kind': 'local', 'name': 'Seeded test grid', 'unit': 'metre', 'axis_order': 'xy',
         'reference_sha256': '0' * 64}


def _rng(case):
    return random.Random(random.Random(SEED).getrandbits(64) + case)


def _collection(geometries, code='Q'):
    return {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': str(index), 'geometry': mapping(geometry),
         'properties': {'ID': index, 'CODE': code}}
        for index, geometry in enumerate(geometries, 1)]}


def _save(bridge, name, geometries, *, epsg=None, code='Q'):
    request = name.replace('.', '-')
    frame = LOCAL if epsg is None else {'definition': f'EPSG:{epsg}', 'axis_order': 'xy'}
    prepare = prepare_creation if epsg is None else prepare_modern_crs_creation
    prepare(bridge, _collection(geometries, code), {'name': name, 'map_scale': 1000, 'crs': frame}, request)
    return Path(bridge.apply(request)['output'])


def _geometries(geometry_type, rng):
    geometries = []
    for index in range(4):
        x, y = index * 50 + rng.uniform(-4, 4), rng.uniform(-4, 4)
        w, h = rng.uniform(8, 18), rng.uniform(7, 21)
        if geometry_type == 'wt':
            geometry = Point(x, y)
        elif geometry_type == 'wl':
            geometry = LineString([(x, y), (x + w, y + h / 3), (x + w * .6, y + h)])
        else:
            geometry = Polygon(box(x, y, x + w, y + h).exterior.coords, [
                box(x + w / 4, y + h / 4, x + w / 2, y + h / 2).exterior.coords])
        geometries.append(geometry)
    return geometries


@pytest.mark.parametrize('case', range(4))
@pytest.mark.parametrize('kind', ['wt', 'wl', 'wp'])
def test_rigid_motion_equivariance_on_saved_native_geometries(tmp_path, kind, case):
    rng = _rng(case)
    geometries = _geometries(kind, rng)
    dx, dy, theta = rng.uniform(-250, 250), rng.uniform(-250, 250), rng.uniform(-175, 175)

    def moved(geometry):
        return translate(rotate(geometry, theta, origin=(0, 0)), dx, dy)

    bridge = Bridge(tmp_path)
    original = _save(bridge, f'original.{kind}', geometries)
    transformed = _save(bridge, f'transformed.{kind}', [moved(geometry) for geometry in geometries])
    _, before, _ = read_native(original)
    _, after, _ = read_native(transformed)
    assert len(before.geom) == len(after.geom) == len(geometries)
    for a, b in zip(before.geom, after.geom, strict=True):
        assert a.is_valid and b.is_valid
        assert abs(a.area - b.area) < 1e-5
        assert abs(a.length - b.length) < 1e-5
        assert moved(a.centroid).distance(b.centroid) < 1e-5
        assert moved(a).hausdorff_distance(b) < 1e-5
    if kind == 'wp':
        left, right = validate_wp_topology(original), validate_wp_topology(transformed)
        assert left == right


@pytest.mark.parametrize('case,window', list(enumerate([
    (119.05, 119.34, 27.55, 28.08),
    (119.12, 119.41, 29.43, 29.72),
    (119.54, 119.82, 32.42, 32.59),
    (118.88, 119.11, 36.38, 36.55),
])))
def test_cgcs2000_loops_and_bound_false_easting(tmp_path, case, window):
    rng = _rng(case)
    geographic, unprefixed, prefixed = (CRS.from_epsg(code) for code in (4490, 4549, 4528))
    geod = geographic.get_geod()
    a = Transformer.from_crs(geographic, unprefixed, always_xy=True)
    b = Transformer.from_crs(unprefixed, prefixed, always_xy=True)
    c = Transformer.from_crs(prefixed, geographic, always_xy=True)
    p = Transformer.from_crs(geographic, prefixed, always_xy=True)
    q = Transformer.from_crs(prefixed, unprefixed, always_xy=True)
    r = Transformer.from_crs(unprefixed, geographic, always_xy=True)
    layer_a, layer_b = [], []
    for _ in range(16):
        lon, lat = rng.uniform(*window[:2]), rng.uniform(*window[2:])
        x, y = a.transform(lon, lat, errcheck=True)
        x_zone, y_zone = b.transform(x, y, errcheck=True)
        assert abs(x_zone - x - 40_000_000) < 1e-6 and abs(y_zone - y) < 1e-6
        direct = p.transform(lon, lat, errcheck=True)
        assert math.dist(direct, (x_zone, y_zone)) < 1e-6
        for loop in (c.transform(x_zone, y_zone, errcheck=True),
                     r.transform(*q.transform(*direct, errcheck=True), errcheck=True)):
            assert abs(geod.inv(lon, lat, *loop)[2]) < 1e-4
        layer_a.append(Point(x, y))
        layer_b.append(Point(x_zone, y_zone))

    bridge = Bridge(tmp_path)
    plain = _save(bridge, 'plain.wt', layer_a, epsg=4549)
    zonal = _save(bridge, 'zonal.wt', layer_b, epsg=4528)
    for file, epsg, offset in [(plain, 4549, 500_000), (zonal, 4528, 40_500_000)]:
        document, resolved = read_crs_sidecar(file)
        assert resolved.to_epsg() == epsg and document['false_easting_m'] == offset
        assert run_drc_audit([file], reference_crs=f'EPSG:{epsg}', enforce_frames=True)['summary']['passed']
    for files in ([plain, zonal], [zonal, plain]):
        assert 'AUDIT_FALSE_EASTING' in {row['rule_id'] for row in run_drc_audit(files)['violations']}
        with pytest.raises(BridgeError, match='AUDIT_FALSE_EASTING'):
            run_drc_audit(files, enforce_frames=True)


@pytest.mark.parametrize('case', range(3))
def test_integer_pixel_translation_in_fixed_unclipped_canvas(tmp_path, case):
    rng = _rng(case)
    width, height = 600, 420
    map_width, map_height = width - 80 - min(230, max(120, width // 5)) - 16, height - 58 - 82
    view = (0, 0, map_width, map_height)  # exactly one map unit per pixel, independent of data extent
    left, bottom = rng.randrange(80, 105) + .5, rng.randrange(80, 105) + .5
    geometry = box(left, bottom, left + 51, bottom + 26)
    dx, dy = rng.randrange(8, 26), -rng.randrange(7, 20)
    bridge = Bridge(tmp_path)
    files = [_save(bridge, 'before.wp', [geometry]),
             _save(bridge, 'after.wp', [translate(geometry, xoff=dx, yoff=dy)])]
    paths = [tmp_path / name for name in ('before.png', 'after.png')]
    for file, output in zip(files, paths, strict=True):
        _render(_read_layers([{'file': file, 'name': 'Unit'}]), output, width, height, 300, None,
                'Fixed viewport', view=view)

    # Crop inside the frame stroke. Geometry stays well clear of the crop boundary.
    top, left = 62, 84
    a, b = [_read_png(path)[top:54 + map_height, left:76 + map_width] for path in paths]
    background = np.array([255, 255, 251])
    a_mask, b_mask = (np.any(image != background, axis=2) for image in (a, b))
    assert 1000 < a_mask.sum() == b_mask.sum()
    assert np.array_equal(np.roll(a, (-dy, dx), axis=(0, 1)), b)
    expected_difference = np.any(a != np.roll(a, (-dy, dx), axis=(0, 1)), axis=2)
    assert np.array_equal(np.any(a != b, axis=2), expected_difference)
    result = render_diff_heatmap(*paths, tmp_path / 'diff.png')
    assert result['changed_pixel_ratio'] == np.count_nonzero(expected_difference) / (width * height)
    ys, xs = np.nonzero(expected_difference)
    assert result['diff_bbox'] == [left + int(xs.min()), top + int(ys.min()),
                                   left + int(xs.max()) + 1, top + int(ys.max()) + 1]


@pytest.mark.parametrize('case', range(6))
def test_random_partition_union_and_double_entry_area(tmp_path, case):
    rng = _rng(case)
    x, y = rng.uniform(-50, 50), rng.uniform(-50, 50)
    w, h = rng.uniform(60, 90), rng.uniform(30, 50)
    original = box(x, y, x + w, y + h)
    cuts = [x, *sorted(x + rng.uniform(.05, .95) * w for _ in range(4)), x + w]
    pieces = [box(a, y, b, y + h) for a, b in zip(cuts, cuts[1:])]
    rng.shuffle(pieces)
    recombined = union_all(pieces, grid_size=1e-10)
    assert original.symmetric_difference(recombined).area < 1e-6
    bridge = Bridge(tmp_path)
    baseline = _save(bridge, 'baseline.wp', [original])
    partition = _save(bridge, 'partition.wp', pieces)
    merged = _save(bridge, 'merged.wp', [recombined])
    for file, count in [(partition, len(pieces)), (merged, 1)]:
        report = run_drc_audit([file], before_files=[baseline])
        ledger = report['summary']['accounting']
        assert ledger['before']['record_count'] == 1 and ledger['after']['record_count'] == count
        assert abs(ledger['area_delta']) < 1e-6
        assert 'ACCOUNTING_AREA_DRIFT' not in {record['rule_id'] for record in report['violations']}


def test_cache_key_changes_on_layer_reordering_and_role_swap(tmp_path):
    bridge = Bridge(tmp_path)
    files = [_save(bridge, name, [box(offset, 0, offset + 10, 10)])
             for name, offset in [('left.wp', 0), ('right.wp', 8)]]
    bindings = [{'role': role, 'source_file': str(path), 'expected_sha256': sha(path.read_bytes())}
                for role, path in zip(('left', 'right'), files, strict=True)]
    raw = {'schema_version': 'mapgis-plan/v1', 'plan_id': 'cache-mr', 'crs': 'LOCAL', 'unit': 'local',
           'scale': 1000, 'seed': SEED, 'nodes': [{'id': 'pair', 'op': 'file.compare', 'inputs': bindings}]}
    plan = parse_spec(raw)
    rows = [{'role': spec['role'], 'sha256': spec['expected_sha256']} for spec in bindings]
    base = compute_cache_key(plan.nodes[0], rows, plan)
    reversed_layers = compute_cache_key(plan.nodes[0], rows[::-1], plan)
    swapped_roles = compute_cache_key(plan.nodes[0], [rows[0] | {'role': 'right'}, rows[1] | {'role': 'left'}], plan)
    assert len({base, reversed_layers, swapped_roles}) == 3


def test_naive_float_addition_changes_on_reassociation():
    large, small = 1e16, 1.0
    assert (large + small) - large == 0.0
    assert small + (large - large) == 1.0
    assert math.fsum((large, small, -large)) == 1.0


def _operate(bridge, op, request, features, params):
    template = {'name': request + '.wp', 'crs': LOCAL, 'map_scale': 1000}
    prepare_vector_operation(bridge, op, features, template, params, request)
    return read_native(Path(bridge.apply(request)['output']))[1]


def test_first_layer_wins_after_snap_changes_partition_and_owner(tmp_path):
    bridge = Bridge(tmp_path)
    layers = {'A': box(.08, .08, 2.41, 2.09), 'B': box(1.47, .16, 3.72, 3.12)}
    snapped = {name: _operate(bridge, 'topology.snap_grid', 'snap' + name.lower(),
                              _collection([part], name), {'grid_size': 1.}).geom[0]
               for name, part in layers.items()}
    witness = Point(1.5, 1.25)
    assert all(geom.contains(witness) for geom in snapped.values())
    winner_ids, seam_lengths = [], []
    for first, second in (('A', 'B'), ('B', 'A')):
        remaining = _operate(bridge, 'overlay.difference', 'trim' + second.lower(),
                             _collection([snapped[second]], second), {'mask': mapping(snapped[first])})
        assert remaining.data['CODE'].tolist() == [second]
        partition = [(first, snapped[first]), (second, remaining.geom[0])]
        assert union_all([geometry for _, geometry in partition]).equals(union_all(list(snapped.values())))
        assert partition[0][1].intersection(partition[1][1]).area == 0.
        winner_ids.extend(name for name, geometry in partition if geometry.contains(witness))
        seam_lengths.append(partition[0][1].boundary.intersection(partition[1][1].boundary).length)
    assert winner_ids == ['A', 'B']
    assert seam_lengths == [3., 2.]
