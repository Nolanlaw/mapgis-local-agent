import json
from pathlib import Path
import shutil

import geopandas as gpd
from pymapgis import Writer
import pytest
from shapely.geometry import LineString, Point, box, mapping

from mapgis_agent.core import Bridge, BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.drc import RULES_FILE, run_drc_audit


LOCAL = {'kind': 'local', 'name': 'Sprint 2 synthetic drawing', 'axis_order': 'xy',
         'unit': 'unknown', 'reference_sha256': '0' * 64}


def write_layer(folder, name, geometries, *, properties=None, style=None, texts=None, crs=LOCAL, scale=1000):
    bridge = Bridge(folder / 'maker')
    properties = properties if properties is not None else [{'ID': i} for i in range(1, len(geometries) + 1)]
    features = []
    for index, (geom, row) in enumerate(zip(geometries, properties, strict=True), 1):
        feature = {'type': 'Feature', 'id': str(index), 'geometry': mapping(geom), 'properties': row}
        if style:
            feature['style'] = style
        if texts is not None:
            feature['text'] = texts[index-1]
        features.append(feature)
    request = name.replace('.', '-')
    prepare_creation(bridge, {'type': 'FeatureCollection', 'features': features},
                     {'name': name, 'crs': crs, 'map_scale': scale}, request)
    path = folder / name
    shutil.copyfile(bridge.apply(request)['output'], path)
    return path


def findings(report):
    return {row['rule_id'] for row in report['violations']}


def test_drc_geometry_cardinality_and_location(tmp_path):
    wl = write_layer(tmp_path, 'edges.wl', [
        LineString([(0, 0), (10, 10), (0, 10), (10, 0)]), LineString([(10.05, 0), (12, 3)])])
    wp = write_layer(tmp_path, 'areas.wp', [box(0, 0, 4, 5), box(5, 0, 9, 5), box(10, 0, 16, .05)])
    wt = write_layer(tmp_path, 'labels.wt', [Point(1, 1), Point(2, 2), Point(100, 100)])
    report = run_drc_audit([wl, wp], label_files=[wt], dangle_tolerance=.1)
    assert {'DRC_DANGLE', 'DRC_SELF_INTERSECTION', 'DRC_SLIVER', 'DRC_LABEL_OUTSIDE',
            'DRC_MULTIPLE_LABELS', 'DRC_UNLABELLED_POLYGON'} <= findings(report)
    dangles = [row['coords'] for row in report['violations'] if row['rule_id'] == 'DRC_DANGLE']
    assert dangles == [[0., 0.], [12., 3.]]
    assert not report['summary']['passed']
    for row in report['violations']:
        assert {'rule_id', 'category', 'severity', 'message', 'file', 'record_ids', 'coords', 'suggested_fix'} <= row.keys()
    assert all(row['attributes']['aligned'] for row in report['files'])


def test_overlaps_are_errors_and_boundary_touches_are_not(tmp_path):
    # Independent saved areas can overlap. A shared boundary has zero overlap area.
    wp = tmp_path / 'overlap.wp'
    frame = gpd.GeoDataFrame({'ID': [1, 2, 3]}, geometry=[
        box(0, 0, 4, 4), box(2, 2, 5, 5), box(5, 2, 6, 5)])
    Writer(frame, make_valid=False, snap_tolerance=0).to_file(wp)
    report = run_drc_audit([wp])
    overlaps = [row for row in report['violations'] if row['rule_id'] == 'DRC_POLYGON_OVERLAP']
    assert len(overlaps) == 1 and overlaps[0]['record_ids'] == [1, 2]
    assert overlaps[0]['severity'] == 'ERROR'


def test_false_easting_and_geographic_mixing_are_preserved(tmp_path):
    paths = {}
    for epsg, x in ((4528, 40_500_000), (4549, 500_000), (4326, 120)):
        file = tmp_path / f'epsg{epsg}.wl'
        y = 30 if epsg == 4326 else 3_000_000
        Writer(gpd.GeoDataFrame({'ID': [1]}, geometry=[LineString([(x, y), (x+1, y+1)])],
                                crs=f'EPSG:{epsg}')).to_file(file)
        paths[epsg] = file
    report = run_drc_audit([paths[4528], paths[4549], paths[4326]])
    assert {'AUDIT_FALSE_EASTING', 'AUDIT_UNPROJECTED_MIX'} <= findings(report)
    with pytest.raises(BridgeError, match='AUDIT_FALSE_EASTING'):
        run_drc_audit([paths[4549]], 'EPSG:4528', enforce_frames=True)
    with pytest.raises(BridgeError, match='AUDIT_CRS_MISMATCH'):
        run_drc_audit([paths[4549]], unit='degree', enforce_frames=True)


def test_lvs_bidirectional_colors_and_scoped_exception(tmp_path):
    wp = write_layer(tmp_path, 'units.wp', [box(0, 0, 4, 4), box(5, 0, 9, 4)],
                     properties=[{'CODE': 'Q', 'COLOR_NO': 7}, {'CODE': 'Q-local', 'COLOR_NO': 8}])
    rules = json.loads(RULES_FILE.read_text(encoding='utf8'))
    assert [item['id'] for item in rules['standards']] == ['GB/T 958-2015', 'DZ/T 0179-2025']
    slib = tmp_path / 'Slib'
    slib.mkdir()
    manifest = {'legend_items': [{'code': 'Q', 'color_no': 9, 'color_code': 'Y55', 'rgb': [255, 255, 115]},
                                 {'code': 'K', 'color_no': 10}], 'palette': {'7': [255, 255, 115]}}
    (slib / 'legend_items.json').write_text(json.dumps(manifest), encoding='utf8')
    report = run_drc_audit([wp], slib_path=slib,
                           exceptions=[{'code': 'Q-local', 'file': wp.name, 'reason': 'Local Quaternary deposit'}])
    assert {'LVS_MISSING_LEGEND', 'LVS_UNUSED_LEGEND', 'LVS_STANDARD_DEVIATION'} <= findings(report)
    assert not any(row.get('code') == 'Q-local' and row['rule_id'] == 'LVS_STANDARD_DEVIATION'
                   for row in report['violations'])
    assert any(row.get('code') == 'Q' and row['rule_id'] == 'LVS_STANDARD_DEVIATION'
               and row['record_ids'] == [1] for row in report['violations'])


def test_double_entry_spatial_accounting(tmp_path):
    before = write_layer(tmp_path, 'before.wp', [box(0, 0, 4, 4), box(5, 0, 9, 4)],
                         properties=[{'ID': 1, 'CODE': 'Q'}, {'ID': 2, 'CODE': 'K'}])
    after = write_layer(tmp_path, 'after.wp', [box(0, 0, 4, 4)], properties=[{'ID': 1, 'CODE': ''}])
    report = run_drc_audit([after], before_files=[before])
    assert {'ACCOUNTING_AREA_DRIFT', 'ACCOUNTING_RECORD_LOSS'} <= findings(report)
    accounting = report['summary']['accounting']
    assert accounting['area_change_rate'] == -.5 and accounting['record_delta'] == -1
    assert accounting['attribute_coverage_delta'] < 0
    preserved = run_drc_audit([before], before_files=[before])
    assert preserved['summary']['accounting']['area_delta'] == 0
    assert not any(code.startswith('ACCOUNTING_') for code in findings(preserved))
