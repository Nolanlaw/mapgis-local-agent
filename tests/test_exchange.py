import json
from pathlib import Path

import geopandas as gpd
import pytest
from pymapgis import Reader, Writer
from shapely.geometry import LineString, Point, Polygon, mapping

from mapgis_agent.core import Bridge, BridgeError, annotation_text, native_ids, read_native, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.exchange import export_vector, prepare_import, query_attributes


CRS_4549 = {"definition": "+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs", "axis_order": "xy"}


def _create_sample_points_wt(bridge: Bridge, request_id: str = "create-sample-points") -> Path:
    features = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "pt-1",
                "geometry": mapping(Point(500000, 3500000)),
                "properties": {"ID": 10, "名称": "ZK01", "地质代号": "T1", "val": 100.5},
                "text": "钻孔ZK01",
                "style": {"point_type": 0, "color": 1, "height": 3.0, "width": 3.0},
            },
            {
                "type": "Feature",
                "id": "pt-2",
                "geometry": mapping(Point(500100, 3500100)),
                "properties": {"ID": 10, "名称": "ZK02", "地质代号": "T2", "val": 200.5},
                "style": {"point_type": 1, "symbol_no": 1, "color": 2, "height": 2.0, "width": 2.0},
            },
        ],
    }
    template = {
        "name": "sample_points.wt",
        "crs": CRS_4549,
        "map_scale": 50000,
    }
    prepare_creation(bridge, features, template, request_id)
    applied = bridge.apply(request_id)
    return Path(applied["output"])


def _create_sample_lines_wl(bridge: Bridge, request_id: str = "create-sample-lines") -> Path:
    features = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "ln-1",
                "geometry": mapping(LineString([(500000, 3500000), (500100, 3500100)])),
                "properties": {"ID": 1, "名称": "断层F1", "地质代号": "F1", "val": 10.0},
                "style": {"linetype": 1, "color": 3, "width": 0.5},
            },
            {
                "type": "Feature",
                "id": "ln-2",
                "geometry": mapping(LineString([(500100, 3500100), (500200, 3500200)])),
                "properties": {"ID": 2, "名称": "地质界线", "地质代号": "J1", "val": 20.0},
                "style": {"linetype": 1, "color": 4, "width": 0.3},
            },
        ],
    }
    template = {
        "name": "sample_lines.wl",
        "crs": CRS_4549,
        "map_scale": 50000,
    }
    prepare_creation(bridge, features, template, request_id)
    applied = bridge.apply(request_id)
    return Path(applied["output"])


def _create_sample_polygons_wp(bridge: Bridge, request_id: str = "create-sample-polygons") -> Path:
    features = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "pg-1",
                "geometry": mapping(Polygon([(500000, 3500000), (500100, 3500000), (500100, 3500100), (500000, 3500100)])),
                "properties": {"ID": 1, "地层": "T1b", "area_code": "A01"},
                "style": {"fill_color": 3, "pattern": 0},
            },
            {
                "type": "Feature",
                "id": "pg-2",
                "geometry": mapping(Polygon([(500100, 3500000), (500200, 3500000), (500200, 3500100), (500100, 3500100)])),
                "properties": {"ID": 2, "地层": "T1c", "area_code": "A02"},
                "style": {"fill_color": 5, "pattern": 0},
            },
        ],
    }
    template = {
        "name": "sample_polygons.wp",
        "crs": CRS_4549,
        "map_scale": 50000,
    }
    prepare_creation(bridge, features, template, request_id)
    applied = bridge.apply(request_id)
    return Path(applied["output"])


@pytest.fixture
def scratch_bridge(tmp_path):
    return Bridge(tmp_path)


def test_query_attributes_duplicate_user_id_and_physical_id(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "query-attr-dup")
    file_sha = sha(wt_path.read_bytes())

    sql = 'SELECT __record_id, ID, "名称", "地质代号", val FROM features WHERE ID = 10 ORDER BY __record_id'
    res = query_attributes(str(wt_path), file_sha, sql)

    assert res["source_sha256"] == file_sha
    assert res["columns"] == ["__record_id", "ID", "名称", "地质代号", "val"]
    assert res["row_count"] == 2
    assert res["truncated"] is False
    assert res["rows"] == [
        [1, 10, "ZK01", "T1", 100.5],
        [2, 10, "ZK02", "T2", 200.5],
    ]


def test_query_attributes_aggregates_and_filter(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "query-attr-agg")
    file_sha = sha(wt_path.read_bytes())

    sql = 'SELECT COUNT(*) AS cnt, SUM(val) AS sum_val, AVG(val) AS avg_val, MAX("名称") AS max_name FROM features'
    res = query_attributes(str(wt_path), file_sha, sql)

    assert res["row_count"] == 1
    assert res["rows"][0] == [2, 301.0, 150.5, "ZK02"]


def test_query_attributes_cte_and_limit_truncation(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "query-attr-cte")
    file_sha = sha(wt_path.read_bytes())

    # Test CTE query
    sql_cte = """
    WITH ranked AS (
        SELECT __record_id, ID, "名称", val,
               ROW_NUMBER() OVER (ORDER BY val DESC) AS rn
        FROM features
    )
    SELECT __record_id, "名称", val FROM ranked WHERE rn = 1
    """
    res_cte = query_attributes(str(wt_path), file_sha, sql_cte)
    assert res_cte["row_count"] == 1
    assert res_cte["rows"][0] == [2, "ZK02", 200.5]
    assert res_cte["truncated"] is False

    # Test limit and truncation
    sql_all = "SELECT * FROM features ORDER BY __record_id"
    res_lim = query_attributes(str(wt_path), file_sha, sql_all, limit=1)
    assert res_lim["limit"] == 1
    assert res_lim["truncated"] is True
    assert res_lim["row_count"] == 1
    assert len(res_lim["rows"]) == 1


def test_query_attributes_security_rejections(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "query-attr-sec")
    file_sha = sha(wt_path.read_bytes())
    before_blob = wt_path.read_bytes()

    bad_queries = [
        "INSERT INTO features (__record_id, ID) VALUES (99, 99)",
        "UPDATE features SET val = 999.0 WHERE __record_id = 1",
        "DELETE FROM features WHERE __record_id = 1",
        "ATTACH DATABASE ':memory:' AS aux",
        "PRAGMA table_info(features)",
        "SELECT load_extension('nonexistent')",
    ]

    for bq in bad_queries:
        with pytest.raises(BridgeError):
            query_attributes(str(wt_path), file_sha, bq)

    assert wt_path.read_bytes() == before_blob


def test_query_attributes_reserved_column_collision(scratch_bridge):
    root = scratch_bridge.root / "deliverables/reserved_col"
    root.mkdir(parents=True, exist_ok=True)
    target_file = root / "colliding.wl"
    gdf = gpd.GeoDataFrame(
        {"__record_id": [1, 2], "ID": [10, 20]},
        geometry=[LineString([(0, 0), (1, 1)]), LineString([(1, 1), (2, 2)])],
        crs="EPSG:4549",
    )
    Writer(gdf).to_file(target_file)
    f_sha = sha(target_file.read_bytes())

    with pytest.raises(BridgeError, match="RESERVED_COLUMN"):
        query_attributes(str(target_file), f_sha, "SELECT * FROM features")


def test_query_attributes_stale_source(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "query-attr-stale")
    with pytest.raises(BridgeError, match="STALE_SOURCE"):
        query_attributes(str(wt_path), "0" * 64, "SELECT * FROM features")


def test_export_vector_existing_output_dir_unchanged(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "export-exist-dir")
    f_sha = sha(wt_path.read_bytes())

    existing_dir = scratch_bridge.root / "deliverables/already_exists"
    existing_dir.mkdir(parents=True, exist_ok=True)
    marker_file = existing_dir / "marker.txt"
    marker_file.write_text("do not touch", encoding="utf-8")

    with pytest.raises(BridgeError, match="DESTINATION_EXISTS"):
        export_vector(str(wt_path), f_sha, str(existing_dir), driver="GeoJSON")

    assert marker_file.exists()
    assert marker_file.read_text(encoding="utf-8") == "do not touch"
    assert len(list(existing_dir.iterdir())) == 1


def test_export_vector_geojson_roundtrip_with_chinese_and_text(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "export-geojson")
    f_sha = sha(wt_path.read_bytes())

    out_dir = scratch_bridge.root / "deliverables/export_geojson_out"
    res = export_vector(str(wt_path), f_sha, str(out_dir), driver="GeoJSON")

    assert res["status"] == "SUCCESS"
    assert res["driver"] == "GeoJSON"
    assert res["feature_count"] == 2

    exported_file = Path(res["exported_file"])
    manifest_file = Path(res["manifest_file"])
    assert exported_file.is_file()
    assert manifest_file.is_file()

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["driver"] == "GeoJSON"
    assert manifest["native_type"] == "POINT"
    assert manifest["source_sha256"] == f_sha
    assert len(manifest["features"]) == 2
    assert manifest["features"][0]["text"] == "钻孔ZK01"
    assert manifest["features"][0]["style"]["point_type"] == 0
    assert manifest["features"][0]["original_attributes"]["名称"] == "ZK01"
    assert manifest["features"][1]["text"] is None
    assert manifest["features"][1]["style"]["point_type"] == 1

    readback_gdf = gpd.read_file(exported_file)
    assert len(readback_gdf) == 2
    assert list(readback_gdf["名称"]) == ["ZK01", "ZK02"]
    assert list(readback_gdf[manifest['physical_id_column']]) == [1, 2]


def test_export_vector_gpkg_roundtrip(scratch_bridge):
    wl_path = _create_sample_lines_wl(scratch_bridge, "export-gpkg")
    f_sha = sha(wl_path.read_bytes())

    out_dir = scratch_bridge.root / "deliverables/export_gpkg_out"
    res = export_vector(str(wl_path), f_sha, str(out_dir), driver="GPKG")

    assert res["status"] == "SUCCESS"
    assert res["driver"] == "GPKG"
    exported_file = Path(res["exported_file"])
    readback_gdf = gpd.read_file(exported_file)
    assert len(readback_gdf) == 2
    assert list(readback_gdf["名称"]) == ["断层F1", "地质界线"]


def test_export_vector_shapefile_collision_and_truncation_rejection(scratch_bridge):
    root = scratch_bridge.root / "deliverables/shapefile_checks"
    root.mkdir(parents=True, exist_ok=True)

    # 1. Field name collision on 10 characters
    collision_file = root / "collision.wl"
    gdf_collision = gpd.GeoDataFrame(
        {"LONG_FIELD_1": [1], "LONG_FIELD_2": [2]},
        geometry=[LineString([(0, 0), (1, 1)])],
        crs="EPSG:4549",
    )
    Writer(gdf_collision).to_file(collision_file)
    c_sha = sha(collision_file.read_bytes())

    out_dir_col = scratch_bridge.root / "deliverables/out_shp_col"
    with pytest.raises(BridgeError, match="SHAPEFILE_FIELD_COLLISION"):
        export_vector(str(collision_file), c_sha, str(out_dir_col), driver="ESRI Shapefile")
    assert not out_dir_col.exists()

    # 2. String attribute truncation exceeding 254 bytes
    trunc_file = root / "trunc.wl"
    long_str = "A" * 300
    gdf_trunc = gpd.GeoDataFrame(
        {"ID": [1], "DESC": [long_str]},
        geometry=[LineString([(0, 0), (1, 1)])],
        crs="EPSG:4549",
    )
    Writer(gdf_trunc).to_file(trunc_file)
    t_sha = sha(trunc_file.read_bytes())

    out_dir_trunc = scratch_bridge.root / "deliverables/out_shp_trunc"
    with pytest.raises(BridgeError, match="SHAPEFILE_STRING_TRUNCATION"):
        export_vector(str(trunc_file), t_sha, str(out_dir_trunc), driver="ESRI Shapefile")
    assert not out_dir_trunc.exists()


def test_export_vector_shapefile_success_and_readback(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "export-shp-ok")
    f_sha = sha(wt_path.read_bytes())

    out_dir = scratch_bridge.root / "deliverables/out_shp_success"
    res = export_vector(str(wt_path), f_sha, str(out_dir), driver="ESRI Shapefile")

    assert res["status"] == "SUCCESS"
    assert res["driver"] == "ESRI Shapefile"
    assert res["feature_count"] == 2

    exported_file = Path(res["exported_file"])
    manifest_file = Path(res["manifest_file"])
    assert exported_file.is_file()
    assert manifest_file.is_file()

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["driver"] == "ESRI Shapefile"
    assert "field_map" in manifest
    assert manifest["field_map"]["地质代号"] == "地质代"

    readback_gdf = gpd.read_file(exported_file)
    assert len(readback_gdf) == 2
    assert "地质代" in readback_gdf.columns
    assert list(readback_gdf["地质代"]) == ["T1", "T2"]


def test_prepare_import_geojson_and_gpkg_roundtrip_native(scratch_bridge):
    # 1. GeoJSON with sidecar
    wt_path = _create_sample_points_wt(scratch_bridge, "import-orig-wt")
    f_sha = sha(wt_path.read_bytes())
    export_dir = scratch_bridge.root / "deliverables/import_exp_geojson"
    exp = export_vector(str(wt_path), f_sha, str(export_dir), driver="GeoJSON")

    geojson_path = exp["exported_file"]
    geo_sha = sha(Path(geojson_path).read_bytes())

    template_wt = {
        "name": "imported_points.wt",
        "crs": CRS_4549,
        "map_scale": 50000,
    }
    preview = prepare_import(scratch_bridge, geojson_path, geo_sha, template_wt, "imp-geojson-req")
    assert preview["kind"] == "CREATE" and preview["state"] == "PREPARED"
    applied = scratch_bridge.apply("imp-geojson-req")
    imported_wt = Path(applied["output"])

    blob, reader, sections = read_native(imported_wt)
    ids = native_ids(reader)
    assert len(ids) == 2
    assert annotation_text(blob, sections, ids[0]) == "钻孔ZK01"
    assert annotation_text(blob, sections, ids[1]) is None
    assert reader.data["名称"].tolist() == ["ZK01", "ZK02"]
    assert reader.point_params["point_type"].tolist() == [0, 1]

    # 2. GPKG import
    wl_path = _create_sample_lines_wl(scratch_bridge, "import-orig-wl")
    wl_sha = sha(wl_path.read_bytes())
    gpkg_dir = scratch_bridge.root / "deliverables/import_exp_gpkg"
    exp_gpkg = export_vector(str(wl_path), wl_sha, str(gpkg_dir), driver="GPKG")
    gpkg_file = exp_gpkg["exported_file"]
    gpkg_sha = sha(Path(gpkg_file).read_bytes())

    template_wl = {
        "name": "imported_lines.wl",
        "crs": CRS_4549,
        "map_scale": 50000,
    }
    preview_wl = prepare_import(scratch_bridge, gpkg_file, gpkg_sha, template_wl, "imp-gpkg-req")
    applied_wl = scratch_bridge.apply("imp-gpkg-req")
    imported_wl = Path(applied_wl["output"])

    _, reader_wl, _ = read_native(imported_wl)
    assert len(reader_wl) == 2
    assert reader_wl.data["名称"].tolist() == ["断层F1", "地质界线"]


def test_export_import_polygon_wp_roundtrip(scratch_bridge):
    wp_path = _create_sample_polygons_wp(scratch_bridge, "import-orig-wp")
    wp_sha = sha(wp_path.read_bytes())
    gpkg_dir = scratch_bridge.root / "deliverables/export_wp_gpkg"
    exp = export_vector(str(wp_path), wp_sha, str(gpkg_dir), driver="GPKG")

    gpkg_file = exp["exported_file"]
    gpkg_sha = sha(Path(gpkg_file).read_bytes())

    template_wp = {
        "name": "imported_polygons.wp",
        "crs": CRS_4549,
        "map_scale": 50000,
    }
    preview = prepare_import(scratch_bridge, gpkg_file, gpkg_sha, template_wp, "imp-wp-req")
    assert preview["kind"] == "CREATE" and preview["state"] == "PREPARED"
    applied = scratch_bridge.apply("imp-wp-req")
    imported_wp = Path(applied["output"])

    blob, reader_wp, sections = read_native(imported_wp)
    assert len(reader_wp) == 2
    assert reader_wp.data["地层"].tolist() == ["T1b", "T1c"]
    assert reader_wp.polygon_params["fill_color"].tolist() == [3, 5]


def test_prepare_import_sidecar_tampering_and_isolation(scratch_bridge):
    wt_path = _create_sample_points_wt(scratch_bridge, "import-tamper-wt")
    f_sha = sha(wt_path.read_bytes())
    export_dir = scratch_bridge.root / "deliverables/import_tamper_exp"
    exp = export_vector(str(wt_path), f_sha, str(export_dir), driver="GeoJSON")

    geojson_path = Path(exp["exported_file"])
    geo_sha = sha(geojson_path.read_bytes())
    manifest_path = Path(exp["manifest_file"])

    # Tamper with file_hashes inside manifest
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["file_hashes"][geojson_path.name] = "0" * 64
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    template = {
        "name": "tampered.wt",
        "crs": CRS_4549,
        "map_scale": 50000,
    }
    with pytest.raises(BridgeError, match="SIDECAR_HASH_VERIFICATION_FAILED"):
        prepare_import(scratch_bridge, str(geojson_path), geo_sha, template, "tamper-req")

    # Sidecar filename traversal rejection
    manifest_data["file_hashes"]["../escaped.txt"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    with pytest.raises(BridgeError, match="SIDECAR_INVALID_FILENAME"):
        prepare_import(scratch_bridge, str(geojson_path), geo_sha, template, "tamper-traversal-req")


def test_prepare_import_validation_rejections(scratch_bridge):
    root = scratch_bridge.root / "deliverables/imp_validations"
    root.mkdir(parents=True, exist_ok=True)

    # 1. Stale hash
    valid_geojson = root / "valid.geojson"
    gdf = gpd.GeoDataFrame(
        {"ID": [1]},
        geometry=[Point(500000, 3500000)],
        crs="EPSG:4549",
    )
    gdf.to_file(valid_geojson, driver="GeoJSON")
    v_sha = sha(valid_geojson.read_bytes())

    valid_template = {"name": "pts.wt", "crs": CRS_4549, "map_scale": 50000}
    with pytest.raises(BridgeError, match="STALE_SOURCE"):
        prepare_import(scratch_bridge, str(valid_geojson), "0" * 64, valid_template, "req-stale")

    # 2. Missing explicit CRS
    with pytest.raises(BridgeError, match="EXPLICIT_CRS"):
        prepare_import(scratch_bridge, str(valid_geojson), v_sha, {"name": "pts.wt", "map_scale": 50000}, "req-no-crs")

    # 3. Invalid template name extension
    with pytest.raises(BridgeError, match="INVALID_EXTENSION"):
        prepare_import(scratch_bridge, str(valid_geojson), v_sha, {"name": "pts.txt", "crs": CRS_4549, "map_scale": 50000}, "req-ext")

    # 4. Mixed geometry
    mixed_geojson = root / "mixed.geojson"
    gdf_mixed = gpd.GeoDataFrame(
        {"ID": [1, 2]},
        geometry=[Point(500000, 3500000), LineString([(500000, 3500000), (500100, 3500100)])],
        crs="EPSG:4549",
    )
    gdf_mixed.to_file(mixed_geojson, driver="GeoJSON")
    m_sha = sha(mixed_geojson.read_bytes())
    with pytest.raises(BridgeError, match="INVALID_GEOMETRY"):
        prepare_import(scratch_bridge, str(mixed_geojson), m_sha, valid_template, "req-mixed")

    # 5. Ambiguous GPKG layers
    multi_gpkg = root / "multi.gpkg"
    gdf1 = gpd.GeoDataFrame({"ID": [1]}, geometry=[Point(500000, 3500000)], crs="EPSG:4549")
    gdf2 = gpd.GeoDataFrame({"ID": [2]}, geometry=[Point(500100, 3500100)], crs="EPSG:4549")
    gdf1.to_file(multi_gpkg, layer="layer1", driver="GPKG")
    gdf2.to_file(multi_gpkg, layer="layer2", driver="GPKG")
    g_sha = sha(multi_gpkg.read_bytes())
    with pytest.raises(BridgeError, match="AMBIGUOUS_GPKG_LAYERS"):
        prepare_import(scratch_bridge, str(multi_gpkg), g_sha, valid_template, "req-multi-gpkg")

    # 6. Conflicting identities
    conflict_geojson = root / "conflict.geojson"
    raw_features = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": "dup-id", "geometry": mapping(Point(0, 0)), "properties": {"a": 1}},
            {"type": "Feature", "id": "dup-id", "geometry": mapping(Point(1, 1)), "properties": {"a": 2}},
        ],
    }
    conflict_geojson.write_text(json.dumps(raw_features), encoding="utf-8")
    c_sha = sha(conflict_geojson.read_bytes())
    with pytest.raises(BridgeError, match="CONFLICTING_IDENTITIES"):
        prepare_import(scratch_bridge, str(conflict_geojson), c_sha, valid_template, "req-conflict")
