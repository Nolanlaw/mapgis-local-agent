import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pymapgis import Reader, Writer
from shapely.geometry import LineString, Point, Polygon

from mapgis_agent.comparison import compare_native_files
from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.deletion import prepare_deletion


@pytest.fixture
def test_root(tmp_path):
    root = tmp_path / "comparison_tests"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_unchanged_files(test_root):
    f1 = test_root / "lines1.wl"
    f2 = test_root / "lines2.wl"
    gdf = gpd.GeoDataFrame(
        {"ID": [1, 2], "类别": ["断层", "界线"], "值": [10.5, 20.0]},
        geometry=[
            LineString([(110, 30), (111, 31)]),
            LineString([(111, 30), (112, 31)]),
        ],
        crs="EPSG:4326",
    )
    Writer(gdf).to_file(f1)
    f2.write_bytes(f1.read_bytes())

    result = compare_native_files(str(f1), str(f2))

    assert result["scope"] == "PHYSICAL_IDS_ONLY"
    assert result["native_reopen"] == "NOT_VERIFIED"
    assert result["type"] == "LINE"
    assert result["before"]["sha256"] == result["after"]["sha256"]
    assert result["before"]["count"] == 2
    assert result["after"]["count"] == 2
    assert result["counts"] == {
        "added": 0,
        "removed": 0,
        "changed": 0,
        "unchanged": 2,
    }
    assert result["total"] == 0
    assert result["returned"] == 0
    assert result["truncated"] is False
    assert result["changes"] == []

    # Ensure JSON serializability with allow_nan=False
    serialized = json.dumps(result, allow_nan=False)
    assert "NOT_VERIFIED" in serialized


def test_style_and_attribute_changes_with_duplicate_id(test_root):
    f_before = test_root / "dup_before.wl"
    f_after = test_root / "dup_after.wl"

    # Two records with duplicate attribute ID=9
    gdf_before = gpd.GeoDataFrame(
        {"ID": [9, 9], "类别": ["断层", "界线"], "值": [0.0, 1.5]},
        geometry=[
            LineString([(110, 30), (111, 31)]),
            LineString([(111, 30), (112, 31)]),
        ],
        crs="EPSG:4326",
    )
    Writer(gdf_before).to_file(f_before)

    # In f_after, physical record 1 changes attribute "类别" and style "color"
    # Physical record 2 is unchanged
    bridge = Bridge(test_root / "bridge_dup")
    work = Path(bridge.copy(str(f_before), "dup_copy")["path"])
    before_sha = sha(work.read_bytes())
    bridge.prepare(
        str(work),
        before_sha,
        [1],
        "dup_edit",
        attributes={"类别": "岩脉"},
        style={"color": 2},
    )
    applied = bridge.apply("dup_edit")
    f_after.write_bytes(Path(applied["output"]).read_bytes())

    result = compare_native_files(str(f_before), str(f_after))

    assert result["counts"]["changed"] == 1
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["added"] == 0
    assert result["counts"]["removed"] == 0
    assert result["total"] == 1
    assert result["returned"] == 1
    assert len(result["changes"]) == 1

    change = result["changes"][0]
    assert change["record_id"] == 1
    assert change["status"] == "changed"
    assert change["geometry"] is None
    assert change["attributes"]["类别"] == {"before": "断层", "after": "岩脉"}
    assert "ID" not in change["attributes"]
    assert change["style"]["color"] == {"before": 1, "after": 2}


def test_geometry_differences(test_root):
    f_before = test_root / "geom_before.wt"
    f_after = test_root / "geom_after.wt"

    p1_b = Point(110.0, 30.0)
    p2 = Point(115.0, 35.0)
    p1_a = Point(110.5, 30.5)

    gdf_before = gpd.GeoDataFrame(
        {"ID": [1, 2], "name": ["A", "B"]},
        geometry=[p1_b, p2],
        crs="EPSG:4326",
    )
    Writer(gdf_before).to_file(f_before)

    gdf_after = gpd.GeoDataFrame(
        {"ID": [1, 2], "name": ["A", "B"]},
        geometry=[p1_a, p2],
        crs="EPSG:4326",
    )
    Writer(gdf_after).to_file(f_after)

    result = compare_native_files(str(f_before), str(f_after))

    assert result["counts"]["changed"] == 1
    assert result["counts"]["unchanged"] == 1
    assert result["total"] == 1
    change = result["changes"][0]
    assert change["record_id"] == 1
    assert change["status"] == "changed"
    assert change["geometry"] is not None
    assert change["geometry"]["before_wkb_sha256"] == sha(p1_b.wkb)
    assert change["geometry"]["after_wkb_sha256"] == sha(p1_a.wkb)
    assert change["geometry"]["before_wkb_sha256"] != change["geometry"]["after_wkb_sha256"]
    assert change["attributes"] == {}
    assert change["style"] == {}


def test_addition_and_removal_including_physical_slot_holes(test_root):
    f_original = test_root / "slots_orig.wt"
    gdf = gpd.GeoDataFrame(
        {"ID": [1, 2, 3], "name": ["P1", "P2", "P3"]},
        geometry=[Point(110, 30), Point(111, 31), Point(112, 32)],
        crs="EPSG:4326",
    )
    Writer(gdf).to_file(f_original)

    # Create tombstoned file using prepare_deletion to tombstone physical record 2
    bridge = Bridge(test_root / "bridge_del")
    work = Path(bridge.copy(str(f_original), "del_copy")["path"])
    orig_sha = sha(work.read_bytes())
    prepare_deletion(bridge, str(work), orig_sha, [2], "del_p2")
    del_applied = bridge.apply("del_p2")
    f_tombstoned = Path(del_applied["output"])

    # Compare original (1, 2, 3) vs tombstoned (1, 3) -> record 2 is removed
    res_removed = compare_native_files(str(f_original), str(f_tombstoned))
    assert res_removed["counts"]["removed"] == 1
    assert res_removed["counts"]["unchanged"] == 2
    assert res_removed["counts"]["added"] == 0
    assert res_removed["counts"]["changed"] == 0
    assert res_removed["removed_ids"] == [2]
    assert len(res_removed["changes"]) == 1
    ch_rem = res_removed["changes"][0]
    assert ch_rem["record_id"] == 2
    assert ch_rem["status"] == "removed"
    assert ch_rem["geometry"]["before_wkb_sha256"] == sha(Point(111, 31).wkb)
    assert ch_rem["geometry"]["after_wkb_sha256"] is None

    # Compare tombstoned (1, 3) vs original (1, 2, 3) -> record 2 is added into the hole
    res_added = compare_native_files(str(f_tombstoned), str(f_original))
    assert res_added["counts"]["added"] == 1
    assert res_added["counts"]["unchanged"] == 2
    assert res_added["counts"]["removed"] == 0
    assert res_added["counts"]["changed"] == 0
    assert res_added["added_ids"] == [2]
    assert len(res_added["changes"]) == 1
    ch_add = res_added["changes"][0]
    assert ch_add["record_id"] == 2
    assert ch_add["status"] == "added"
    assert ch_add["geometry"]["before_wkb_sha256"] is None
    assert ch_add["geometry"]["after_wkb_sha256"] == sha(Point(111, 31).wkb)


def test_column_additions_removals_and_na_safety(test_root):
    f_before = test_root / "cols_before.wl"
    f_after = test_root / "cols_after.wl"

    # Before has columns [ID, 旧字段, na_field]
    # With NaN values that will be handled safely
    gdf_before = gpd.GeoDataFrame(
        {
            "ID": [1],
            "旧字段": ["待删除"],
            "na_field": [float("nan")],
        },
        geometry=[LineString([(110, 30), (111, 31)])],
        crs="EPSG:4326",
    )
    Writer(gdf_before).to_file(f_before)

    # After has columns [ID, 新字段, na_field]
    # na_field is still NaN/None (should not be reported as changed)
    gdf_after = gpd.GeoDataFrame(
        {
            "ID": [1],
            "新字段": ["新添加"],
            "na_field": [float("nan")],
        },
        geometry=[LineString([(110, 30), (111, 31)])],
        crs="EPSG:4326",
    )
    Writer(gdf_after).to_file(f_after)

    result = compare_native_files(str(f_before), str(f_after))
    assert result["counts"]["changed"] == 1
    change = result["changes"][0]
    # "旧字段" removed -> before has value, after is None
    assert change["attributes"]["旧字段"] == {"before": "待删除", "after": None}
    # "新字段" added -> before is None, after has value
    assert change["attributes"]["新字段"] == {"before": None, "after": "新添加"}
    # "na_field" remained NaN -> not in diff
    assert "na_field" not in change["attributes"]


def test_polygon_parameters_and_wp(test_root):
    f_before = test_root / "poly_before.wp"
    f_after = test_root / "poly_after.wp"

    poly = Polygon([(110, 30), (111, 30), (111, 31), (110, 30)])
    gdf = gpd.GeoDataFrame({"ID": [1]}, geometry=[poly], crs="EPSG:4326")
    Writer(gdf).to_file(f_before)

    bridge = Bridge(test_root / "bridge_wp")
    work = Path(bridge.copy(str(f_before), "wp_copy")["path"])
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "wp_style", style={"fill_color": 3})
    applied = bridge.apply("wp_style")
    f_after.write_bytes(Path(applied["output"]).read_bytes())

    result = compare_native_files(str(f_before), str(f_after))
    assert result["type"] == "POLYGON"
    assert result["counts"]["changed"] == 1
    change = result["changes"][0]
    assert change["style"]["fill_color"] == {"before": 0, "after": 3}
    assert change["geometry"] is None


def test_limit_and_truncation(test_root):
    f_before = test_root / "limit_before.wt"
    f_after = test_root / "limit_after.wt"

    pts_b = [Point(110 + i, 30 + i) for i in range(5)]
    pts_a = [Point(110.1 + i, 30.1 + i) for i in range(5)]
    gdf_b = gpd.GeoDataFrame({"ID": list(range(1, 6))}, geometry=pts_b, crs="EPSG:4326")
    gdf_a = gpd.GeoDataFrame({"ID": list(range(1, 6))}, geometry=pts_a, crs="EPSG:4326")
    Writer(gdf_b).to_file(f_before)
    Writer(gdf_a).to_file(f_after)

    # Test limit=2
    res_2 = compare_native_files(str(f_before), str(f_after), limit=2)
    assert res_2["total"] == 5
    assert res_2["returned"] == 2
    assert res_2["limit"] == 2
    assert res_2["truncated"] is True
    assert len(res_2["changes"]) == 2
    assert [c["record_id"] for c in res_2["changes"]] == [1, 2]

    # Test limit=5
    res_5 = compare_native_files(str(f_before), str(f_after), limit=5)
    assert res_5["total"] == 5
    assert res_5["returned"] == 5
    assert res_5["truncated"] is False
    assert len(res_5["changes"]) == 5

    # Test limit=0 (counts only)
    res_0 = compare_native_files(str(f_before), str(f_after), limit=0)
    assert res_0["total"] == 5
    assert res_0["returned"] == 0
    assert res_0["truncated"] is True
    assert len(res_0["changes"]) == 0
    assert res_0["counts"]["changed"] == 5


def test_errors_and_validation(test_root):
    f_wt = test_root / "err.wt"
    f_wl = test_root / "err.wl"

    gdf_pt = gpd.GeoDataFrame({"ID": [1]}, geometry=[Point(110, 30)], crs="EPSG:4326")
    gdf_ln = gpd.GeoDataFrame({"ID": [1]}, geometry=[LineString([(110, 30), (111, 31)])], crs="EPSG:4326")
    Writer(gdf_pt).to_file(f_wt)
    Writer(gdf_ln).to_file(f_wl)

    # Incompatible types
    with pytest.raises(BridgeError, match="Incompatible native types"):
        compare_native_files(str(f_wt), str(f_wl))

    # Invalid limits
    with pytest.raises(BridgeError, match="limit must be an integer"):
        compare_native_files(str(f_wt), str(f_wt), limit=True)

    with pytest.raises(BridgeError, match="limit must be an integer"):
        compare_native_files(str(f_wt), str(f_wt), limit="10")

    with pytest.raises(BridgeError, match="limit must be non-negative"):
        compare_native_files(str(f_wt), str(f_wt), limit=-1)

    with pytest.raises(BridgeError, match="limit exceeds maximum"):
        compare_native_files(str(f_wt), str(f_wt), limit=1001)

    # Malformed file
    bad_file = test_root / "bad.wl"
    bad_file.write_bytes(b"WMAP`D21" + b"\xff" * 2000)
    with pytest.raises(BridgeError):
        compare_native_files(str(bad_file), str(f_wl))

    # Non-existent file
    with pytest.raises(BridgeError, match="File not found"):
        compare_native_files("does_not_exist.wt", str(f_wt))


def test_annotation_pool_changes_are_not_reported_as_unchanged(test_root):
    bridge = Bridge(test_root / "annotation")
    features = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "note", "geometry": {"type": "Point", "coordinates": [1, 2]},
         "properties": {"ID": 9}, "text": "甲"}]}
    template = {"name": "note.wt", "map_scale": 1000,
                "crs": {"kind": "local", "name": "drawing", "axis_order": "xy",
                        "unit": "unknown", "reference_sha256": "0" * 64}}
    prepare_creation(bridge, features, template, "note")
    before = Path(bridge.apply("note")["output"])
    blob, _, sections = read_native(before)
    changed = bytearray(blob)
    pool, size = sections[1]
    old = blob[pool:pool + size]
    changed[pool:pool + size] = old.replace("甲".encode("gbk"), "乙".encode("gbk"))
    after = test_root / "changed-note.wt"
    after.write_bytes(changed)
    result = compare_native_files(str(before), str(after))
    assert result["counts"]["changed"] == 1
    assert result["changes"][0]["text"] == {"before": "甲", "after": "乙"}


def test_counts_only_output_and_coordinate_metadata_are_explicit(test_root):
    import struct

    before = test_root / "header.wt"
    after = test_root / "header-changed.wt"
    Writer(gpd.GeoDataFrame({"ID": [1]}, geometry=[Point(110, 30)], crs="EPSG:4326")).to_file(before)
    data = bytearray(before.read_bytes())
    struct.pack_into("<I", data, 260, 0)
    after.write_bytes(data)
    result = compare_native_files(str(before), str(after), limit=0)
    assert result["unchanged_ids"] == []
    assert result["ids_truncated"]["unchanged"] is True
    assert result["metadata_changes"]["header_record_count"] == {"before": 1, "after": 0}
    assert result["records_equal"] is True and result["bytes_equal"] is False
