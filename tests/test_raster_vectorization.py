"""Pixel-exact thematic regions, shared boundaries and native integer attributes."""
import json

import numpy as np
import pytest
import rasterio
from pymapgis import Reader
from rasterio.control import GroundControlPoint
from rasterio.transform import Affine
from shapely.geometry import Point

from mapgis_agent.core import Bridge, BridgeError, sha

GRID = Affine(2, .3, 500000, .2, -2, 3500000)
CRS = {'definition': '+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs', 'axis_order': 'xy'}


def source(folder, values, mask=None, gcps=False):
    path = folder / 'classes.tif'
    with rasterio.open(path, 'w', driver='GTiff', width=values.shape[1], height=values.shape[0],
                       count=1, dtype=values.dtype, crs='+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs', transform=GRID) as ds:
        ds.write(values, 1)
        if mask is not None:
            ds.write_mask(mask.astype('uint8') * 255)
        if gcps:
            ds.gcps = ([GroundControlPoint(row=r, col=c, x=(GRID*(c, r))[0], y=(GRID*(c, r))[1])
                        for r, c in [(0, 0), (0, 3), (3, 0), (3, 3)]], ds.crs)
    from mapgis_agent.native_raster import raster_source_info
    info = raster_source_info(str(path))
    return path, info


def prepare(folder, path, info, identity='regions', **options):
    from mapgis_agent.raster_vectorization import prepare_raster_vectorization
    bridge = Bridge(folder)
    result = prepare_raster_vectorization(bridge, str(path), info['source_sha256'],
        {'band': 1, **options}, {'name': 'regions.wp', 'crs': CRS, 'map_scale': 1000,
        'style': {'fill_color': 3}}, identity, info['members'])
    return bridge, result


def assert_saved_pixels(reader, values, valid):
    # Independent point membership at every pixel centre; this does not call
    # the production rasterizer or infer NoData from the zero class.
    for row, col in np.ndindex(values.shape):
        point = Point(*(GRID*(col+.5, row+.5)))
        owners = [i for i, polygon in enumerate(reader.geom) if polygon.covers(point)]
        if valid[row, col]:
            assert len(owners) == 1
            assert int(reader.data['class_id'].iloc[owners[0]]) == int(values[row, col])
        else:
            assert not owners


def test_holes_rotated_affine_and_valid_zero_survive_native_wp(tmp_path):
    values = np.zeros((7, 8), dtype='int32'); values[1:6, 2:6] = 7
    valid = np.ones(values.shape, dtype=bool); valid[3, 3] = False
    path, info = source(tmp_path, values, valid); before = path.read_bytes()
    bridge, result = prepare(tmp_path, path, info)
    applied = bridge.apply('regions'); reader = Reader(applied['output'], make_valid=False)
    assert all(g.is_valid for g in reader.geom)
    assert_saved_pixels(reader, values, valid)
    parts = [part for g in reader.geom for part in (g.geoms if g.geom_type == 'MultiPolygon' else [g])]
    assert sum(len(g.interiors) for g in parts) == 2
    assert reader.geom[0].boundary.intersection(reader.geom[1].boundary).length > 0
    determinant = abs(GRID.a*GRID.e-GRID.b*GRID.d)
    for label in [0, 7]:
        rows = reader.data['class_id'] == label
        count = int(np.count_nonzero(valid & (values == label)))
        assert int(reader.data.loc[rows, 'pixel_count'].sum()) == count
        assert sum(g.area for g, selected in zip(reader.geom, rows) if selected) == pytest.approx(count*determinant, rel=1e-9)
    assert result['qa']['raster_vectorization']['valid_pixels'] == 55
    assert path.read_bytes() == before
    assert bridge.status('regions')['output_matches']
    _, replay = prepare(tmp_path, path, info)
    assert replay['state'] == 'APPLIED' and replay['candidate_sha256'] == applied['candidate_sha256']


@pytest.mark.parametrize('connectivity,regions', [(4, 3), (8, 2)])
def test_point_touching_diagonal_cells_have_explicit_region_connectivity(tmp_path, connectivity, regions):
    values = np.zeros((3, 3), dtype='int32'); values[0, 0] = values[1, 1] = 7
    path, info = source(tmp_path, values)
    bridge, result = prepare(tmp_path, path, info, connectivity=connectivity)
    reader = Reader(bridge.apply('regions')['output'], make_valid=False)
    assert result['qa']['raster_vectorization']['regions'] == regions
    assert len(reader.geom) == result['feature_count'] == 3
    assert all(g.is_valid for g in reader.geom)
    assert_saved_pixels(reader, values, np.ones(values.shape, dtype=bool))
    if connectivity == 8:
        parts = reader.data[reader.data['class_id'] == 7]
        assert len(parts) == 2 and parts['region_id'].nunique() == 1
        assert set(parts['part_id']) == {1, 2}


def test_long_diagonal_keeps_disconnected_interiors_as_valid_native_parts(tmp_path):
    values = np.zeros((8, 8), dtype='int32'); np.fill_diagonal(values, 7)
    path, info = source(tmp_path, values)
    bridge, result = prepare(tmp_path, path, info, connectivity=8)
    reader = Reader(bridge.apply('regions')['output'], make_valid=False)
    assert all(g.is_valid for g in reader.geom)
    assert result['qa']['raster_vectorization']['regions'] == 2
    assert len(reader.geom) == 10
    assert_saved_pixels(reader, values, np.ones(values.shape, dtype=bool))


def test_signed_large_integer_classes_and_mask_are_lossless(tmp_path):
    values = np.array([[-2147483648, -2147483648, 16777217, 16777217],
                       [2147483647, 2147483647, 0, 0]], dtype='int32')
    valid = np.ones(values.shape, dtype=bool); valid[1, 3] = False
    path, info = source(tmp_path, values, valid)
    bridge, _ = prepare(tmp_path, path, info)
    reader = Reader(bridge.apply('regions')['output'], make_valid=False)
    assert {int(x) for x in reader.data['class_id']} == {-2147483648, 16777217, 2147483647, 0}
    assert_saved_pixels(reader, values, valid)


@pytest.mark.parametrize('bad', [.5, 2147483648., -2147483649., float('inf')])
def test_nonintegral_overflow_or_nonfinite_valid_labels_are_rejected(tmp_path, bad):
    values = np.zeros((3, 3), dtype='float64'); values[1, 1] = bad
    path, info = source(tmp_path, values)
    with pytest.raises(BridgeError, match='VECTORIZE_LABELS'):
        prepare(tmp_path, path, info)


def test_gcp_grid_needs_explicit_rectification(tmp_path):
    path, info = source(tmp_path, np.ones((3, 3), dtype='int32'), gcps=True)
    with pytest.raises(BridgeError, match='VECTORIZE_GEOREFERENCE'):
        prepare(tmp_path, path, info)


def test_region_budget_and_source_change_are_rejected(tmp_path):
    values = (np.indices((3, 3)).sum(axis=0) % 2).astype('int32')
    path, info = source(tmp_path, values)
    with pytest.raises(BridgeError, match='VECTORIZE_REGION_LIMIT'):
        prepare(tmp_path, path, info, identity='too-many', max_regions=2)
    bridge, result = prepare(tmp_path, path, info)
    path.write_bytes(path.read_bytes()+b'changed-source')
    with pytest.raises(BridgeError, match='STALE_SOURCE'):
        bridge.apply('regions')
    assert not __import__('pathlib').Path(result['output']).exists()


def test_explicit_output_crs_must_equal_the_observed_source_crs(tmp_path):
    from mapgis_agent.raster_vectorization import prepare_raster_vectorization
    path, info = source(tmp_path, np.ones((3, 3), dtype='int32'))
    with pytest.raises(BridgeError, match='VECTORIZE_CRS'):
        prepare_raster_vectorization(Bridge(tmp_path), str(path), info['source_sha256'], {'band': 1},
            {'name': 'wrong.wp', 'crs': {'definition': 'EPSG:4326', 'axis_order': 'xy'}, 'map_scale': 1000},
            'wrong-crs', info['members'])
