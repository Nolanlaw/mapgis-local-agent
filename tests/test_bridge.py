import json
from pathlib import Path
import sqlite3

import geopandas as gpd
import pytest
from pymapgis import Reader, Writer
from shapely.geometry import Point, LineString, Polygon

from mapgis_agent.core import Bridge, BridgeError, sha, read_native


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    bridge = Bridge(root)
    source = tmp_path / "original.wl"
    gdf = gpd.GeoDataFrame({"ID": [9, 9], "类别": ["断层", "界线"], "值": [0.0, 1.5]},
                           geometry=[LineString([(110, 30), (111, 31)]),
                                     LineString([(111, 30), (112, 31)])], crs="EPSG:4326")
    Writer(gdf).to_file(source)
    copied = bridge.copy(str(source), "copy")
    return bridge, source, Path(copied["path"])


def test_explicit_physical_ids_and_json(case):
    bridge, _, work = case
    result = bridge.inspect(str(work))
    assert [row["record_id"] for row in result["rows"]] == [1, 2]
    assert [row["attributes"]["ID"] for row in result["rows"]] == [9, 9]
    assert result["rows"][0]["attributes"]["值"] == 0
    json.loads(json.dumps(result, allow_nan=False))


def test_preview_apply_preserves_original_and_all_other_bytes(case):
    bridge, original, work = case
    frozen = original.read_bytes()
    before = work.read_bytes()
    receipt = bridge.prepare(str(work), sha(before), [1], "edit", style={"color": 2})
    assert work.read_bytes() == before and original.read_bytes() == frozen
    applied = bridge.apply("edit")
    after = Path(applied["output"]).read_bytes()
    offsets = {c["offset"] + i for c in receipt["changes"] for i in range(len(bytes.fromhex(c["after"])))}
    assert len(before) == len(after)
    assert all(i in offsets for i, (a, b) in enumerate(zip(before, after)) if a != b)
    parsed = Reader(applied["output"], make_valid=False, read_graphic_params=True)
    assert parsed.line_params.color.tolist() == [2, 1]
    assert [g.wkb for g in parsed.geom] == [g.wkb for g in Reader(work).geom]
    assert work.read_bytes() == before and original.read_bytes() == frozen


def test_stale_and_request_id_conflicts(case):
    bridge, _, work = case
    digest = sha(work.read_bytes())
    bridge.prepare(str(work), digest, [1], "edit", style={"color": 2})
    with pytest.raises(BridgeError, match="REQUEST_ID_CONFLICT"):
        bridge.prepare(str(work), digest, [2], "edit", style={"color": 2})
    blob = bytearray(work.read_bytes())
    blob[-1] ^= 1
    work.write_bytes(blob)
    with pytest.raises(BridgeError, match="STALE_SOURCE"):
        bridge.apply("edit")


def test_idempotency_and_interrupted_receipt_reconciliation(case):
    bridge, _, work = case
    digest = sha(work.read_bytes())
    first = bridge.prepare(str(work), digest, [1], "edit", style={"color": 3})
    assert bridge.prepare(str(work), digest, [1], "edit", style={"color": 3}) == first
    applied = bridge.apply("edit")
    with sqlite3.connect(bridge.db_path) as db:
        db.execute("UPDATE commands SET state='PREPARED' WHERE id='edit'")
    resumed = Bridge(bridge.root).apply("edit")
    assert resumed["output"] == applied["output"]
    assert resumed["reconciled_existing_output"]
    assert len(list((bridge.root / "deliverables/patches/edit").glob("*.wl"))) == 1
    assert bridge.status("edit")["output_matches"]


def test_changed_output_not_replayed(case):
    bridge, _, work = case
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "edit", style={"color": 2})
    output = Path(bridge.apply("edit")["output"])
    blob = bytearray(output.read_bytes())
    blob[-1] ^= 1
    output.write_bytes(blob)
    assert bridge.status("edit")["state"] == "OUTPUT_CHANGED"
    with pytest.raises(BridgeError, match="OUTPUT_CONFLICT"):
        bridge.apply("edit")


def test_recovery_retains_later_independent_attribute_changes(case):
    bridge, _, work = case
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "color", style={"color": 2})
    first = Path(bridge.apply("color")["output"])
    bridge.prepare(str(first), sha(first.read_bytes()), [2], "later", attributes={"类别": "岩脉"})
    later = Path(bridge.apply("later")["output"])
    recovery = bridge.recover("color", str(later), sha(later.read_bytes()), "recover")
    recovered = Reader(bridge.apply("recover")["output"], read_graphic_params=True)
    assert recovered.line_params.color.tolist() == [1, 1]
    assert recovered.data["类别"].tolist() == ["断层", "岩脉"]
    assert recovery["retained_later_independent_bytes"]


def test_recovery_does_not_overwrite_later_same_field(case):
    bridge, _, work = case
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "first", style={"color": 2})
    first = Path(bridge.apply("first")["output"])
    bridge.prepare(str(first), sha(first.read_bytes()), [1], "second", style={"color": 3})
    later = Path(bridge.apply("second")["output"])
    with pytest.raises(BridgeError, match="FIELD_CONFLICT"):
        bridge.recover("first", str(later), sha(later.read_bytes()), "bad-recovery")


@pytest.mark.parametrize('duplicate_target', [False, True])
def test_structural_recovery_relocates_only_a_unique_unchanged_record(case, duplicate_target):
    bridge, _, work = case
    bridge.prepare(str(work), sha(work.read_bytes()), [1], 'original-color', style={'color': 2})
    bridge.apply('original-color')
    later = bridge.root / 'work/later-structure.wl'
    first = LineString([(110, 30), (111, 31)])
    new = first if duplicate_target else LineString([(113, 30), (114, 31)])
    gdf = gpd.GeoDataFrame({'ID': [9, 9, 9], '类别': ['断层', '岩脉', '断层'],
                            '值': [0.0, 1.5, 0.0]},
                           geometry=[first, LineString([(111, 30), (112, 31)]), new], crs='EPSG:4326')
    Writer(gdf).to_file(later)
    bridge.prepare(str(later), sha(later.read_bytes()), [1], 'later-color', style={'color': 2})
    later = Path(bridge.apply('later-color')['output'])
    frozen = later.read_bytes()
    if duplicate_target:
        with pytest.raises(BridgeError, match='STRUCTURAL_CONFLICT.*ambiguous'):
            bridge.recover('original-color', str(later), sha(frozen), 'structural-recovery')
        return
    receipt = bridge.recover('original-color', str(later), sha(frozen), 'structural-recovery')
    restored = Path(bridge.apply('structural-recovery')['output'])
    result = bridge.inspect(str(restored))
    assert result['count'] == 3
    assert result['rows'][0]['style']['color'] == 1
    assert result['rows'][1]['attributes']['类别'] == '岩脉'
    assert result['rows'][2]['geometry_type'] == 'LineString'
    assert receipt['recovery_mapping'] == 'unique_unchanged_physical_record'
    allowed = {change['offset'] + i for change in receipt['changes']
               for i in range(len(bytes.fromhex(change['after'])))}
    assert all(i in allowed for i, (a, b) in enumerate(zip(frozen, restored.read_bytes())) if a != b)
    assert later.read_bytes() == frozen


def test_cancel_and_output_scope(case):
    bridge, original, work = case
    with pytest.raises(BridgeError, match="work copy"):
        bridge.prepare(str(original), sha(original.read_bytes()), [1], "no-write", style={"color": 2})
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "cancel", style={"color": 2})
    bridge.cancel("cancel")
    with pytest.raises(BridgeError, match="cancelled"):
        bridge.apply("cancel")


@pytest.mark.parametrize("attributes,style", [({"类别": "太长的地质类别值"}, {}),
                                              ({"类别": "🙂"}, {}), ({"值": float("nan")}, {}),
                                              ({}, {"color": 70000}), ({}, {"color": -1}),
                                              ({}, {"color": 1.5}), ({}, {"unverified_field": 3})])
def test_unsupported_or_lossy_input_is_explicit(case, attributes, style):
    bridge, _, work = case
    before = work.read_bytes()
    with pytest.raises((BridgeError, ValueError)):
        bridge.prepare(str(work), sha(before), [1], "invalid", attributes, style)
    assert work.read_bytes() == before


@pytest.mark.parametrize("kind", ["point", "polygon"])
def test_point_and_polygon_color_preserves_every_other_byte(tmp_path, kind):
    bridge = Bridge(tmp_path / "project")
    geom = Point(110, 30) if kind == "point" else Polygon([(110, 30), (111, 30), (111, 31), (110, 30)])
    gdf = gpd.GeoDataFrame({"ID": [1]}, geometry=[geom], crs="EPSG:4326")
    source = tmp_path / ("source.wt" if kind == "point" else "source.wp")
    Writer(gdf).to_file(source)
    work = Path(bridge.copy(str(source), "copy")["path"])
    field = "color" if kind == "point" else "fill_color"
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "color", style={field: 2})
    output = Path(bridge.apply("color")["output"])
    assert len(work.read_bytes()) == len(output.read_bytes())
    parsed = Reader(output, make_valid=False, read_graphic_params=True)
    params = parsed.point_params if kind == "point" else parsed.polygon_params
    assert params.iloc[0][field] == 2
    assert parsed.geom[0].wkb == Reader(work, make_valid=False).geom[0].wkb


def test_malformed_native_header_rejected(tmp_path):
    file = tmp_path / "bad.wl"
    file.write_bytes(b"WMAP`D21" + b"\xff" * 2000)
    with pytest.raises(BridgeError):
        read_native(file)
