import math
from pathlib import Path
import pytest
from pymapgis import Reader
from pyproj import CRS
from shapely.geometry import Point, LineString

from mapgis_agent.core import Bridge, BridgeError, read_native, annotation_text
from mapgis_agent.cartography import projection_points, prepare_map_frame


WGS84_GEO = {"definition": "EPSG:4326", "axis_order": "xy"}
WGS84_TM_117 = {
    "definition": "+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs",
    "axis_order": "xy",
}
CGCS2000_GK_120 = {"definition": "EPSG:4549", "axis_order": "xy"}


def test_projection_points_wgs84_tm_central_equator():
    # Equator on central meridian (117E, 0N) gives exactly easting 500000.0, northing 0.0
    res = projection_points([[117.0, 0.0]], WGS84_GEO, WGS84_TM_117)
    assert len(res["points"]) == 1
    x, y = res["points"][0]
    assert math.isclose(x, 500000.0, abs_tol=1e-7)
    assert math.isclose(y, 0.0, abs_tol=1e-7)
    assert res["source_crs"]["axis_order"] == "xy"
    assert res["target_crs"]["axis_order"] == "xy"
    assert "Transverse Mercator" in res["operation"] or "tmerc" in res["operation"] or "axis order" in res["operation"]
    assert res["accuracy"] == 0.0
    assert "transformer" in res


def test_projection_points_roundtrip():
    known_points = [[116.5, 39.5], [117.0, 40.0], [117.8, 39.2]]
    fwd = projection_points(known_points, WGS84_GEO, WGS84_TM_117)
    assert len(fwd["points"]) == 3
    inv = projection_points(fwd["points"], WGS84_TM_117, WGS84_GEO)
    assert len(inv["points"]) == 3
    for orig, back in zip(known_points, inv["points"]):
        assert math.isclose(orig[0], back[0], abs_tol=1e-7)
        assert math.isclose(orig[1], back[1], abs_tol=1e-7)


def test_projection_points_invalid_coordinates():
    # Non-finite coordinates, boolean, string, 3D, 1D
    for bad_coords in [
        [[float("nan"), 0.0]],
        [[0.0, float("inf")]],
        [[True, 0.0]],
        [["117.0", 0.0]],
        [[117.0, 0.0, 100.0]],
        [[117.0]],
        [None],
        "not a list",
    ]:
        with pytest.raises(BridgeError):
            projection_points(bad_coords, WGS84_GEO, WGS84_TM_117)


def test_projection_points_source_and_target_crs_validation():
    # Local frame rejected
    local_crs = {
        "kind": "local",
        "name": "cad-drawing",
        "axis_order": "xy",
        "unit": "metre",
        "reference_sha256": "0" * 64,
    }
    with pytest.raises(BridgeError, match="EARTH_CRS_REQUIRED"):
        projection_points([[117.0, 0.0]], local_crs, WGS84_TM_117)
    with pytest.raises(BridgeError, match="EARTH_CRS_REQUIRED"):
        projection_points([[117.0, 0.0]], WGS84_GEO, local_crs)

    # Missing or invalid axis_order
    with pytest.raises(BridgeError):
        projection_points([[117.0, 0.0]], {"definition": "EPSG:4326"}, WGS84_TM_117)
    with pytest.raises(BridgeError):
        projection_points([[117.0, 0.0]], {"definition": "EPSG:4326", "axis_order": "yx"}, WGS84_TM_117)


def test_projection_points_missing_best_grids_and_datum_shift_accuracy():
    # Missing best grid (e.g. NAD27 EPSG:4267 from WGS84)
    with pytest.raises(BridgeError, match="MISSING_BEST_GRID"):
        projection_points([[117.0, 0.0]], WGS84_GEO, {"definition": "EPSG:4267", "axis_order": "xy"})

    # Custom datum shift with unknown accuracy across different datums requires opt-in
    custom_unknown_acc = {
        "definition": "+proj=longlat +ellps=krass +towgs84=24,-123,-94,0,0,0,0 +no_defs",
        "axis_order": "xy",
    }
    with pytest.raises(BridgeError, match="UNKNOWN_ACCURACY"):
        projection_points([[117.0, 0.0]], WGS84_GEO, custom_unknown_acc, allow_unknown_accuracy=False)

    # With explicit opt-in, it succeeds
    res = projection_points([[117.0, 0.0]], WGS84_GEO, custom_unknown_acc, allow_unknown_accuracy=True)
    assert len(res["points"]) == 1
    assert res["accuracy"] is None


def test_prepare_map_frame_border(tmp_path):
    bridge = Bridge(tmp_path)
    parameters = {
        "geographic_bounds": [116.0, 39.0, 118.0, 40.0],
        "grid_step_degrees": 0.5,
        "subdivisions": 8,
    }
    template = {
        "name": "frame_border.wl",
        "map_scale": 50000,
        "crs": WGS84_TM_117,
        "style": {"linetype": 1, "color": 3, "width": 0.3},
    }
    receipt = prepare_map_frame(bridge, "border", parameters, template, "req-border-1")
    assert receipt["kind"] == "CREATE"
    assert receipt["state"] == "PREPARED"
    assert receipt["qa"]["counts"]["vertices"] == 4 * 8 + 1
    assert receipt["qa"]["physical_scale"] == "1:50000"
    assert receipt["qa"]["geographic_bounds"] == [116.0, 39.0, 118.0, 40.0]

    applied = bridge.apply("req-border-1")
    assert applied["state"] == "APPLIED"

    blob, reader, sections = read_native(Path(applied["output"]))
    assert len(reader.geom) == 1
    line = reader.geom[0]
    assert line.is_closed
    coords = list(line.coords)
    assert len(coords) == 4 * 8 + 1

    # Verify projected corners match
    corners_geo = [[116.0, 39.0], [118.0, 39.0], [118.0, 40.0], [116.0, 40.0]]
    corners_proj = projection_points(corners_geo, WGS84_GEO, WGS84_TM_117)["points"]
    assert math.isclose(coords[0][0], corners_proj[0][0], abs_tol=1e-6)
    assert math.isclose(coords[0][1], corners_proj[0][1], abs_tol=1e-6)
    assert math.isclose(coords[8][0], corners_proj[1][0], abs_tol=1e-6)
    assert math.isclose(coords[8][1], corners_proj[1][1], abs_tol=1e-6)
    assert math.isclose(coords[16][0], corners_proj[2][0], abs_tol=1e-6)
    assert math.isclose(coords[16][1], corners_proj[2][1], abs_tol=1e-6)
    assert math.isclose(coords[24][0], corners_proj[3][0], abs_tol=1e-6)
    assert math.isclose(coords[24][1], corners_proj[3][1], abs_tol=1e-6)
    assert math.isclose(coords[32][0], corners_proj[0][0], abs_tol=1e-6)

    # Style preserved
    assert reader.line_params.color.tolist() == [3]


def test_prepare_map_frame_grid(tmp_path):
    bridge = Bridge(tmp_path)
    parameters = {
        "geographic_bounds": [116.0, 39.0, 118.0, 40.0],
        "grid_step_degrees": 0.5,
        "subdivisions": 16,
    }
    template = {
        "name": "frame_grid.wl",
        "map_scale": 50000,
        "crs": WGS84_TM_117,
        "style": {"linetype": 2, "color": 4, "width": 0.1},
    }
    receipt = prepare_map_frame(bridge, "grid", parameters, template, "req-grid-1")
    assert receipt["qa"]["counts"]["meridians"] == 3  # 116.5, 117.0, 117.5
    assert receipt["qa"]["counts"]["parallels"] == 1  # 39.5
    assert receipt["qa"]["counts"]["features"] == 4

    applied = bridge.apply("req-grid-1")
    blob, reader, sections = read_native(Path(applied["output"]))
    assert len(reader.geom) == 4
    for g in reader.geom:
        assert isinstance(g, LineString)
        assert len(g.coords) == 16 + 1
    # Native CRS equals target CRS
    target_crs = CRS.from_user_input(WGS84_TM_117["definition"])
    assert reader.crs.equals(target_crs)
    assert reader.line_params.color.tolist() == [4, 4, 4, 4]


def test_prepare_map_frame_labels(tmp_path):
    bridge = Bridge(tmp_path)
    parameters = {
        "geographic_bounds": [116.0, 39.0, 118.0, 40.0],
        "grid_step_degrees": 0.5,
    }
    template = {
        "name": "frame_labels.wt",
        "map_scale": 50000,
        "crs": WGS84_TM_117,
        "style": {"height": 2.5, "width": 2.5},
    }
    receipt = prepare_map_frame(bridge, "labels", parameters, template, "req-labels-1")
    # 3 meridians * 2 + 1 parallel * 2 = 8 ticks
    assert receipt["qa"]["counts"]["features"] == 8
    assert receipt["qa"]["counts"]["meridian_ticks"] == 6
    assert receipt["qa"]["counts"]["parallel_ticks"] == 2

    applied = bridge.apply("req-labels-1")
    blob, reader, sections = read_native(Path(applied["output"]))
    assert len(reader.geom) == 8
    for g in reader.geom:
        assert isinstance(g, Point)

    # Native point annotations readback
    texts = [annotation_text(blob, sections, i) for i in range(1, 9)]
    expected_texts = [
        '116°30\'00"E', '116°30\'00"E',
        '117°00\'00"E', '117°00\'00"E',
        '117°30\'00"E', '117°30\'00"E',
        '39°30\'00"N', '39°30\'00"N',
    ]
    assert sorted(texts) == sorted(expected_texts)
    assert reader.point_params.point_type.tolist() == [0] * 8
    target_crs = CRS.from_user_input(WGS84_TM_117["definition"])
    assert reader.crs.equals(target_crs)


def test_prepare_map_frame_rejects_unsupported_native_encoding_cgcs2000(tmp_path):
    bridge = Bridge(tmp_path)
    parameters = {
        "geographic_bounds": [119.0, 39.0, 121.0, 40.0],
        "grid_step_degrees": 0.5,
    }
    template = {
        "name": "cgcs2000_frame.wl",
        "map_scale": 50000,
        "crs": CGCS2000_GK_120,
    }
    # Writer encodes CGCS2000 as WGS84, so newly encoded reader CRS != target CRS
    with pytest.raises(BridgeError, match="UNSUPPORTED_NATIVE_CRS_ENCODING"):
        prepare_map_frame(bridge, "border", parameters, template, "req-cgcs-1")

    # Ensure clean rollback: no orphaned command left in ledger
    with bridge.connect() as db:
        row = db.execute("SELECT * FROM commands WHERE id='req-cgcs-1'").fetchone()
        assert row is None


def test_prepare_map_frame_validations(tmp_path):
    bridge = Bridge(tmp_path)
    template = {"name": "frame.wl", "map_scale": 50000, "crs": WGS84_TM_117}

    # Invalid part
    with pytest.raises(BridgeError, match="UNKNOWN_PART"):
        prepare_map_frame(bridge, "neatline", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": 0.5}, template, "r1")

    # Extension mismatch: border requires .wl, labels requires .wt
    with pytest.raises(BridgeError, match="INVALID_EXTENSION"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": 0.5}, {"name": "frame.wt", "map_scale": 50000, "crs": WGS84_TM_117}, "r2")
    with pytest.raises(BridgeError, match="INVALID_EXTENSION"):
        prepare_map_frame(bridge, "labels", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": 0.5}, {"name": "frame.wl", "map_scale": 50000, "crs": WGS84_TM_117}, "r3")

    # Antimeridian guessing / invalid bounds (west >= east or south >= north)
    with pytest.raises(BridgeError, match="INVALID_GEOGRAPHIC_BOUNDS"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [118, 39, 116, 40], "grid_step_degrees": 0.5}, template, "r4")
    with pytest.raises(BridgeError, match="INVALID_GEOGRAPHIC_BOUNDS"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [116, 40, 118, 39], "grid_step_degrees": 0.5}, template, "r5")

    # Empty grid: step too large so no interior lines/ticks
    with pytest.raises(BridgeError, match="EMPTY_GRID"):
        prepare_map_frame(bridge, "grid", {"geographic_bounds": [116.1, 39.1, 116.4, 39.4], "grid_step_degrees": 1.0}, template, "r6")

    # Non-positive grid step
    with pytest.raises(BridgeError, match="INVALID_GRID_STEP"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": -0.5}, template, "r7")
    with pytest.raises(BridgeError, match="INVALID_GRID_STEP"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": 0}, template, "r8")

    # Invalid subdivisions
    with pytest.raises(BridgeError, match="INVALID_SUBDIVISIONS"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": 0.5, "subdivisions": 1}, template, "r9")
    with pytest.raises(BridgeError, match="INVALID_SUBDIVISIONS"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": 0.5, "subdivisions": 1001}, template, "r10")

    # Non-geographic source CRS
    with pytest.raises(BridgeError, match="GEOGRAPHIC_CRS_REQUIRED"):
        prepare_map_frame(bridge, "border", {"geographic_bounds": [116, 39, 118, 40], "grid_step_degrees": 0.5, "geographic_crs": WGS84_TM_117}, template, "r11")
