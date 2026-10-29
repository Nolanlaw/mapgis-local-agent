import math
import tempfile
from pathlib import Path

import numpy as np
import pytest
from pymapgis import Reader
from shapely.geometry import Point, mapping

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.creation import prepare_creation


# The analytic plane is local. Old native headers encode GRS80 as WGS84;
# that approximation must not be used to declare these numeric tests CGCS2000.
CRS = {"kind":"local","name":"analytic metre plane","axis_order":"xy",
       "unit":"metre","reference_sha256":"0"*64}


def _make_wt_file(bridge: Bridge, pts: list[tuple[float, float]], heights: list[float],
                  name: str = "nodes.wt", height_field: str = "ELEV", request_id: str = "init-wt") -> tuple[Path, str]:
    features = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": f"pt-{i}",
                "geometry": mapping(Point(x, y)),
                "properties": {"ID": i, height_field: float(z)},
            }
            for i, ((x, y), z) in enumerate(zip(pts, heights), 1)
        ],
    }
    template = {"name": name, "crs": CRS, "map_scale": 1000}
    prepare_creation(bridge, features, template, request_id)
    applied = bridge.apply(request_id)
    out_path = Path(applied["output"])
    return out_path, sha(out_path.read_bytes())


def test_analyze_terrain_plane_25_nodes(tmp_path):
    from mapgis_agent.terrain import analyze_terrain

    bridge = Bridge(tmp_path)
    # Plane z = 100 + 0.1x + 0.2y on 25 nodes on [0, 100]^2
    xs = np.linspace(0, 100, 5)
    ys = np.linspace(0, 100, 5)
    gx, gy = np.meshgrid(xs, ys)
    pts = [(float(x), float(y)) for x, y in zip(gx.ravel(), gy.ravel())]
    heights = [100.0 + 0.1 * x + 0.2 * y for x, y in pts]

    wt_path, wt_sha = _make_wt_file(bridge, pts, heights, name="plane25.wt", request_id="init-plane25")
    orig_blob = wt_path.read_bytes()

    res = analyze_terrain(str(wt_path), wt_sha, height_field="ELEV", base_height=100.0)
    assert res["backend"] == "SciPy/NumPy"
    assert res["counts"]["points"] == 25
    assert res["counts"]["triangles"] == 32
    assert abs(res["planimetric_hull_area"] - 10000.0) < 1e-7
    assert abs(res["triangulated_surface_area"] - 10246.950766) < 1e-7
    assert abs(res["cut_volume"] - 150000.0) < 1e-7
    assert abs(res["fill_volume"] - 0.0) < 1e-7
    assert res["base_height"] == 100.0

    # Source file must remain completely untouched
    assert wt_path.read_bytes() == orig_blob

    # When base_height is None, volume keys omitted or None
    res_no_base = analyze_terrain(str(wt_path), wt_sha, height_field="ELEV", base_height=None)
    assert res_no_base["backend"] == "SciPy/NumPy"
    assert abs(res_no_base["planimetric_hull_area"] - 10000.0) < 1e-7
    assert abs(res_no_base["triangulated_surface_area"] - 10246.950766) < 1e-7
    assert "cut_volume" not in res_no_base or res_no_base.get("cut_volume") is None


def test_analyze_terrain_crossing_plane_unit_square(tmp_path):
    from mapgis_agent.terrain import analyze_terrain

    bridge = Bridge(tmp_path)
    # Crossing plane z = x - 0.5 on unit square [0, 1]^2
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    heights = [x - 0.5 for x, y in pts]  # [-0.5, 0.5, 0.5, -0.5]

    wt_path, wt_sha = _make_wt_file(bridge, pts, heights, name="crossing.wt", request_id="init-crossing")
    res = analyze_terrain(str(wt_path), wt_sha, height_field="ELEV", base_height=0.0)

    assert abs(res["cut_volume"] - 0.125) < 1e-7
    assert abs(res["fill_volume"] - 0.125) < 1e-7
    assert abs(res["planimetric_hull_area"] - 1.0) < 1e-7


def test_prepare_terrain_tin(tmp_path):
    from mapgis_agent.terrain import prepare_terrain

    bridge = Bridge(tmp_path)
    xs = np.linspace(0, 100, 5)
    ys = np.linspace(0, 100, 5)
    gx, gy = np.meshgrid(xs, ys)
    pts = [(float(x), float(y)) for x, y in zip(gx.ravel(), gy.ravel())]
    heights = [100.0 + 0.1 * x + 0.2 * y for x, y in pts]

    wt_path, wt_sha = _make_wt_file(bridge, pts, heights, name="nodes.wt", request_id="init-tin")
    orig_blob = wt_path.read_bytes()

    parameters = {"operation": "tin", "height_field": "ELEV"}
    template = {"name": "tin.wp", "crs": CRS, "map_scale": 1000}

    preview = prepare_terrain(bridge, str(wt_path), wt_sha, parameters, template, "req-tin")
    assert preview["state"] == "PREPARED"
    assert preview["backend"] == "SciPy/NumPy"
    assert preview["metrics"]["triangles"] == 32
    assert preview["input_sha256"] == wt_sha

    applied = bridge.apply("req-tin")
    assert applied["state"] == "APPLIED"

    # Read back native WP file
    reader = Reader(applied["output"], make_valid=False, read_graphic_params=True)
    assert len(reader.geom) == 32
    assert "elevation" in reader.data.columns
    total_area = sum(g.area for g in reader.geom)
    assert abs(total_area - 10000.0) < 1e-6

    # Source file unchanged
    assert wt_path.read_bytes() == orig_blob

    # Idempotent call with same request_id
    repeat = prepare_terrain(bridge, str(wt_path), wt_sha, parameters, template, "req-tin")
    assert repeat["state"] in ("PREPARED", "APPLIED")
    assert repeat["input_sha256"] == wt_sha

    # Conflict with altered parameters
    with pytest.raises(BridgeError, match="REQUEST_ID_CONFLICT"):
        prepare_terrain(bridge, str(wt_path), wt_sha, {"operation": "tin", "height_field": "ELEV"},
                        {"name": "tin.wp", "crs": CRS, "map_scale": 2000}, "req-tin")


def test_prepare_terrain_grid_step10(tmp_path):
    from mapgis_agent.terrain import prepare_terrain

    bridge = Bridge(tmp_path)
    xs = np.linspace(0, 100, 5)
    ys = np.linspace(0, 100, 5)
    gx, gy = np.meshgrid(xs, ys)
    pts = [(float(x), float(y)) for x, y in zip(gx.ravel(), gy.ravel())]
    heights = [100.0 + 0.1 * x + 0.2 * y for x, y in pts]

    wt_path, wt_sha = _make_wt_file(bridge, pts, heights, name="nodes.wt", request_id="init-grid")

    parameters = {
        "operation": "grid",
        "height_field": "ELEV",
        "bounds": [0.0, 0.0, 100.0, 100.0],
        "step": 10.0,
    }
    template = {"name": "sample_grid.wt", "crs": CRS, "map_scale": 1000}

    preview = prepare_terrain(bridge, str(wt_path), wt_sha, parameters, template, "req-grid")
    assert preview["state"] == "PREPARED"
    assert preview["backend"] == "SciPy/NumPy"
    assert preview["metrics"]["point_count"] == 121

    applied = bridge.apply("req-grid")
    assert applied["state"] == "APPLIED"

    reader = Reader(applied["output"], make_valid=False, read_graphic_params=True)
    assert len(reader.geom) == 121
    assert "elevation" in reader.data.columns

    # Check plane error <= 1e-10
    for geom, elev in zip(reader.geom, reader.data["elevation"]):
        expected_z = 100.0 + 0.1 * geom.x + 0.2 * geom.y
        err = abs(float(elev) - expected_z)
        assert err <= 1e-10


def test_prepare_terrain_contours(tmp_path):
    from mapgis_agent.terrain import prepare_terrain

    bridge = Bridge(tmp_path)
    xs = np.linspace(0, 100, 5)
    ys = np.linspace(0, 100, 5)
    gx, gy = np.meshgrid(xs, ys)
    pts = [(float(x), float(y)) for x, y in zip(gx.ravel(), gy.ravel())]
    heights = [100.0 + 0.1 * x + 0.2 * y for x, y in pts]

    wt_path, wt_sha = _make_wt_file(bridge, pts, heights, name="nodes.wt", request_id="init-contours")

    levels = [105.0, 110.0, 115.0, 120.0, 125.0]
    parameters = {
        "operation": "contours",
        "height_field": "ELEV",
        "levels": levels,
        "grid": {"bounds": [0.0, 0.0, 100.0, 100.0], "step": 10.0},
    }
    template = {"name": "contours.wl", "crs": CRS, "map_scale": 1000}

    preview = prepare_terrain(bridge, str(wt_path), wt_sha, parameters, template, "req-contours")
    assert preview["state"] == "PREPARED"
    assert preview["backend"] == "SciPy/NumPy"

    applied = bridge.apply("req-contours")
    assert applied["state"] == "APPLIED"

    reader = Reader(applied["output"], make_valid=False, read_graphic_params=True)
    assert "elevation" in reader.data.columns
    found_levels = sorted(set(round(float(v), 2) for v in reader.data["elevation"]))
    # All levels 105, 110, 115, 120, 125 present including terminal level
    assert found_levels == [105.0, 110.0, 115.0, 120.0, 125.0]

    # Check residual on plane equation <= 1e-9 for all vertices
    for geom, elev in zip(reader.geom, reader.data["elevation"]):
        target_lvl = float(elev)
        coords = list(geom.coords)
        for x, y in coords:
            z_plane = 100.0 + 0.1 * x + 0.2 * y
            res = abs(target_lvl - z_plane)
            assert res <= 1e-9


def test_masking_outside_convex_hull(tmp_path):
    from mapgis_agent.terrain import prepare_terrain

    bridge = Bridge(tmp_path)
    # Triangle points (0,0), (100,0), (0,100)
    pts = [(0.0, 0.0), (100.0, 0.0), (0.0, 100.0)]
    heights = [100.0, 110.0, 120.0]

    wt_path, wt_sha = _make_wt_file(bridge, pts, heights, name="tri_hull.wt", request_id="init-hull")

    # Grid over [0, 0, 100, 100] with step 10
    # Upper-right quadrant (x + y > 100) must be masked, not extrapolated
    parameters = {
        "operation": "grid",
        "height_field": "ELEV",
        "bounds": [0.0, 0.0, 100.0, 100.0],
        "step": 10.0,
    }
    template = {"name": "hull_grid.wt", "crs": CRS, "map_scale": 1000}

    preview = prepare_terrain(bridge, str(wt_path), wt_sha, parameters, template, "req-hull-grid")
    applied = bridge.apply("req-hull-grid")
    reader = Reader(applied["output"], make_valid=False, read_graphic_params=True)

    # All output points must be inside the convex hull (x + y <= 100 + 1e-7)
    assert len(reader.geom) < 121  # Total grid is 121, but outside hull is masked
    for geom in reader.geom:
        assert geom.x + geom.y <= 100.0 + 1e-7


def test_refusal_and_validation(tmp_path):
    from mapgis_agent.terrain import analyze_terrain, prepare_terrain

    bridge = Bridge(tmp_path)
    pts = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0)]
    heights = [10.0, 20.0, 30.0]
    wt_path, wt_sha = _make_wt_file(bridge, pts, heights, name="valid.wt", request_id="init-valid")

    # 1. Stale / wrong sha
    with pytest.raises(BridgeError, match="STALE_SOURCE"):
        analyze_terrain(str(wt_path), "a" * 64, height_field="ELEV")

    # 2. Missing height field
    with pytest.raises(BridgeError, match="MISSING_HEIGHT_FIELD"):
        analyze_terrain(str(wt_path), wt_sha, height_field="NONEXISTENT")

    # 3. Collinear input
    collinear_pts = [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]
    c_path, c_sha = _make_wt_file(bridge, collinear_pts, [1.0, 2.0, 3.0], name="collinear.wt", request_id="init-coll")
    with pytest.raises(BridgeError, match="COLLINEAR_INPUT"):
        analyze_terrain(str(c_path), c_sha, height_field="ELEV")

    # 4. Inconsistent duplicate points
    dup_pts = [(0.0, 0.0), (10.0, 0.0), (0.0, 10.0), (0.0, 0.0)]
    dup_h = [10.0, 20.0, 30.0, 99.0]  # Conflicting height for (0,0)
    d_path, d_sha = _make_wt_file(bridge, dup_pts, dup_h, name="dup.wt", request_id="init-dup")
    with pytest.raises(BridgeError, match="INCONSISTENT_DUPLICATE_POINTS"):
        analyze_terrain(str(d_path), d_sha, height_field="ELEV")

    # 5. Unexpected arguments in prepare_terrain
    bad_params = {"operation": "tin", "height_field": "ELEV", "unauthorized_extra": 123}
    with pytest.raises(BridgeError, match="UNEXPECTED_PARAMETERS"):
        prepare_terrain(bridge, str(wt_path), wt_sha, bad_params, {"name": "t.wp", "crs": CRS, "map_scale": 1000}, "req-bad")

    # 6. Template extension mismatch
    with pytest.raises(BridgeError, match="INVALID_TEMPLATE_NAME"):
        prepare_terrain(bridge, str(wt_path), wt_sha, {"operation": "tin", "height_field": "ELEV"},
                        {"name": "wrong_ext.wt", "crs": CRS, "map_scale": 1000}, "req-ext")


def test_terrain_does_not_relabel_native_wgs84_as_cgcs2000(tmp_path):
    from mapgis_agent.terrain import prepare_terrain
    import geopandas as gpd
    from pymapgis import Writer
    source=tmp_path/'wgs84.wt'
    Writer(gpd.GeoDataFrame({'ELEV':[1.,2.,3.]},geometry=[Point(500000,3000000),
        Point(500010,3000000),Point(500000,3000010)],
        crs='+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m'),map_scale=1000).to_file(source)
    with pytest.raises(BridgeError,match='TERRAIN_CRS_MISMATCH'):
        prepare_terrain(Bridge(tmp_path/'project'),str(source),sha(source.read_bytes()),
            {'operation':'tin','height_field':'ELEV'},
            {'name':'tin.wp','crs':{'definition':'EPSG:4549','axis_order':'xy'},'map_scale':1000},'mismatch')
