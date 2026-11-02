"""Known planes/ridges and explicit range boundaries, independent of native UI."""
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin, xy

from mapgis_agent.core import Bridge, BridgeError
from mapgis_agent.raster import file_sha

FRAME = {'definition': '+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs', 'axis_order': 'xy'}


def dem_case(tmp_path, ridge=False, nodata=False, zero=False):
    file = tmp_path/'dem.tif'
    transform = from_origin(500000, 3300410, 10, 10)
    data = np.full((41, 41), 0. if zero else 125., dtype='float32')
    if ridge:
        data[:, 20] = 180.
    if nodata:
        data[20, 12] = -9999
    with rasterio.open(file, 'w', driver='GTiff', width=41, height=41, count=1,
        dtype='float32', transform=transform, crs='+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs', nodata=-9999 if nodata else None) as ds:
        ds.write(data, 1)
        ds.set_band_unit(1, 'metre')
    return file, list(xy(transform, 20, 5))


@pytest.mark.parametrize('ridge', [False, True])
def test_viewshed_matches_analytic_plane_and_blocking_ridge_and_keeps_sources(tmp_path, ridge):
    from mapgis_agent.visibility import prepare_viewshed
    source, observer = dem_case(tmp_path, ridge=ridge)
    before = (file_sha(source), source.stat().st_mtime_ns)
    bridge = Bridge(tmp_path)
    prepared = prepare_viewshed(bridge, str(source), before[0],
        {'observer': observer, 'observer_height': 2., 'target_height': 0., 'vertical_unit': 'metre',
         'curvature_coefficient': 0., 'max_distance': 0.}, {'name': 'visibility.tif', 'crs': FRAME}, 'view')
    output = bridge.apply('view')
    with rasterio.open(output['output']) as ds:
        data = ds.read(1)
        assert ds.crs == rasterio.crs.CRS.from_string(FRAME['definition']) and data.shape == (41, 41)
        assert ds.transform == from_origin(500000, 3300410, 10, 10)
        assert np.all(data[:, :21] == 1)
        assert np.all(data[:, 21:] == (0 if ridge else 1))
    assert prepared['observer']['cell'] == {'row': 20, 'column': 5}
    assert prepared['gdal']['version'] == rasterio.__gdal_version__
    assert (file_sha(source), source.stat().st_mtime_ns) == before
    again = prepare_viewshed(bridge, str(source), before[0], prepared['payload']['parameters'],
        {'name': 'visibility.tif', 'crs': FRAME}, 'view')
    assert again['state'] == 'APPLIED'


def test_limited_plane_range_uses_exact_cell_centers_not_gdal_unfilled_corner_zero(tmp_path):
    from mapgis_agent.visibility import prepare_viewshed
    source, observer = dem_case(tmp_path)
    bridge = Bridge(tmp_path)
    result = prepare_viewshed(bridge, str(source), file_sha(source),
        {'observer': observer, 'observer_height': 2., 'target_height': 0., 'vertical_unit': 'metre',
         'curvature_coefficient': 0., 'max_distance': 80.}, {'name': 'range.tif', 'crs': FRAME}, 'range')
    output = bridge.apply('range')
    with rasterio.open(output['output']) as ds:
        data = ds.read(1)
        row, col = np.mgrid[:ds.height, :ds.width]
        x = ds.transform.c+(col+.5)*ds.transform.a
        y = ds.transform.f+(row+.5)*ds.transform.e
        inside = np.hypot(x-observer[0], y-observer[1]) <= 80+1e-9
        assert np.count_nonzero(inside) == 178 and np.all(data[inside] == 1)
        assert np.all(data[~inside] == 2) and np.count_nonzero(~inside) == 60
    assert result['range_reclassified_pixels'] == 26


def test_viewshed_rejects_missing_elevation_instead_of_treating_it_as_a_pit(tmp_path):
    from mapgis_agent.visibility import prepare_viewshed
    source, observer = dem_case(tmp_path, nodata=True)
    with pytest.raises(BridgeError, match='VISIBILITY_NODATA'):
        prepare_viewshed(Bridge(tmp_path), str(source), file_sha(source),
            {'observer': observer, 'vertical_unit': 'metre', 'curvature_coefficient': 0.},
            {'name': 'bad.tif', 'crs': FRAME}, 'bad')


def test_completed_gdal_result_survives_parent_loss_without_repeating_analysis(tmp_path, monkeypatch):
    from mapgis_agent import visibility
    source, observer = dem_case(tmp_path, ridge=True)
    bridge = Bridge(tmp_path)
    finish = visibility._finish_viewshed
    def interrupt(*args):
        raise SystemExit(116)
    monkeypatch.setattr(visibility, '_finish_viewshed', interrupt)
    with pytest.raises(SystemExit):
        visibility.prepare_viewshed(bridge, str(source), file_sha(source),
            {'observer': observer, 'vertical_unit': 'metre'}, {'name': 'ridge.tif', 'crs': FRAME}, 'recover')
    stage = Path(bridge.status('recover')['stage'])
    before = (file_sha(stage/'viewshed-raw.tif'), (stage/'viewshed-raw.tif').stat().st_mtime_ns)
    monkeypatch.setattr(visibility, '_finish_viewshed', finish)
    with pytest.raises(BridgeError, match='VISIBILITY_PRODUCER_RUNNING'):
        visibility.reconcile_viewshed(bridge, 'recover')
    monkeypatch.setattr(visibility, '_producer_alive', lambda _: False)
    monkeypatch.setattr(visibility, '_run_viewshed', lambda *args: pytest.fail('Completed GDAL analysis was replayed'))
    recovered = visibility.reconcile_viewshed(bridge, 'recover')
    assert recovered['state'] == 'PREPARED' and recovered['replayed'] is False
    bridge.apply('recover')
    assert bridge.status('recover')['output_matches']
    assert (file_sha(stage/'viewshed-raw.tif'), (stage/'viewshed-raw.tif').stat().st_mtime_ns) == before


@pytest.mark.parametrize('change', ['source', 'working', 'raw', 'checkpoint'])
def test_unsealed_recovery_rejects_changed_analysis_inputs_or_result(tmp_path, monkeypatch, change):
    from mapgis_agent import visibility
    source, observer = dem_case(tmp_path)
    bridge = Bridge(tmp_path)
    finish = visibility._finish_viewshed
    def interrupt(*args):
        raise SystemExit(116)
    monkeypatch.setattr(visibility, '_finish_viewshed', interrupt)
    with pytest.raises(SystemExit):
        visibility.prepare_viewshed(bridge, str(source), file_sha(source),
            {'observer': observer, 'vertical_unit': 'metre'}, {'name': 'view.tif', 'crs': FRAME}, 'changed')
    stage = Path(bridge.status('changed')['stage'])
    target = {'source': source, 'working': stage/'elevation.tif', 'raw': stage/'viewshed-raw.tif',
              'checkpoint': stage/'viewshed-completed.json'}[change]
    if change == 'checkpoint':
        record = json.loads(target.read_text(encoding='utf8'))
        record['payload_sha256'] = '0'*64
        target.write_text(json.dumps(record), encoding='utf8')
    else:
        with target.open('ab') as stream:
            stream.write(b'changed')
    monkeypatch.setattr(visibility, '_finish_viewshed', finish)
    monkeypatch.setattr(visibility, '_producer_alive', lambda _: False)
    with pytest.raises(BridgeError, match='STALE_SOURCE|VISIBILITY_CHECKPOINT_CHANGED'):
        visibility.reconcile_viewshed(bridge, 'changed')


def test_height_units_band_calibration_and_zero_elevation_are_explicit(tmp_path):
    from mapgis_agent.visibility import prepare_viewshed
    metre, observer = dem_case(tmp_path, ridge=True, zero=True)
    feet = tmp_path/'feet.tif'
    with rasterio.open(metre) as ds:
        data, profile = ds.read(1), ds.profile
    # Stored values require scale/offset before conversion from feet.
    with rasterio.open(feet, 'w', **profile) as ds:
        ds.write((data/.3048-50)/2, 1)
        ds.scales = (2.,)
        ds.offsets = (50.,)
        ds.set_band_unit(1, 'foot')
    bridge = Bridge(tmp_path)
    for source, unit, height, request in [(metre, 'metre', 2., 'metre'), (feet, 'foot', 2/.3048, 'feet')]:
        result = prepare_viewshed(bridge, str(source), file_sha(source),
            {'observer': observer, 'vertical_unit': unit, 'observer_height': height},
            {'name': request+'.tif', 'crs': FRAME}, request)
        assert abs(result['observer']['ground_elevation_m']) < 1e-4
        bridge.apply(request)
    with rasterio.open(bridge.status('metre')['output']) as a, rasterio.open(bridge.status('feet')['output']) as b:
        assert np.array_equal(a.read(), b.read())
