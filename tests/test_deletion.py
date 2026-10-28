import asyncio
import json
import struct
from pathlib import Path

import geopandas as gpd
import pytest
from pymapgis import Writer
from shapely.geometry import LineString, Point

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.deletion import prepare_deletion
from mapgis_agent.server import build_server


@pytest.fixture(params=["wt", "wl"])
def case(tmp_path, request):
    root = tmp_path / "project"
    bridge = Bridge(root)
    source = tmp_path / ("original." + request.param)
    geometries = [Point(10, 10), Point(20, 10), Point(30, 10), Point(40, 10)]
    if request.param == "wl":
        geometries = [LineString([(p.x, 10), (p.x, 20)]) for p in geometries]
    Writer(gpd.GeoDataFrame({"ID": [9, 9, 9, 9], "类别": ["甲", "乙", "丙", "丁"]},
                           geometry=geometries)).to_file(source)
    work = Path(bridge.copy(str(source), "copy")["path"])
    return bridge, source, work


def test_mcp_delete_removes_only_physical_ids_and_recovers_exact_bytes(case):
    bridge, source, work = case
    frozen = work.read_bytes()
    before = bridge.inspect(str(work))
    server = build_server(bridge.root)
    content = asyncio.run(server.call_tool("mapgis_prepare_delete", {
        "file": str(work), "expected_sha256": sha(frozen),
        "record_ids": [1, 3], "request_id": "delete-pair",
    }))
    result = json.loads(content[0].text)
    assert result["state"] == "PREPARED"
    assert work.read_bytes() == source.read_bytes() == frozen
    output = Path(bridge.apply("delete-pair")["output"])
    after = bridge.inspect(str(output))
    assert after["count"] == 2
    assert [r["record_id"] for r in after["rows"]] == [2, 4]
    assert [r["attributes"]["ID"] for r in after["rows"]] == [9, 9]
    assert [r["attributes"]["类别"] for r in after["rows"]] == ["乙", "丁"]
    assert after["rows"] == [before["rows"][1], before["rows"][3]]
    # Native BAS60 loads the active count from the header independently of flags.
    assert struct.unpack_from("<I", output.read_bytes(), 260)[0] == 2
    assert struct.unpack_from("<I", output.read_bytes(), 264)[0] == 5
    # Only record flags and native active-count bookkeeping change; slots stay put.
    assert len(output.read_bytes()) == len(frozen)
    assert sum(a != b for a, b in zip(frozen, output.read_bytes())) == 5
    assert Bridge(bridge.root).apply("delete-pair")["reconciled_existing_output"]
    bridge.recover("delete-pair", str(output), sha(output.read_bytes()), "restore")
    assert Path(bridge.apply("restore")["output"]).read_bytes() == frozen
    assert work.read_bytes() == source.read_bytes() == frozen


def test_delete_all_is_empty_and_retains_a_byte_exact_recovery(case):
    bridge, _, work = case
    frozen = work.read_bytes()
    receipt = prepare_deletion(bridge, str(work), sha(frozen), [1, 2, 3, 4], "empty")
    assert receipt["count_after"] == 0
    output = Path(bridge.apply("empty")["output"])
    assert bridge.inspect(str(output))["rows"] == []
    assert struct.unpack_from("<I", output.read_bytes(), 260)[0] == 0
    bridge.recover("empty", str(output), sha(output.read_bytes()), "restore-empty")
    assert Path(bridge.apply("restore-empty")["output"]).read_bytes() == frozen


def test_delete_rejects_inconsistent_native_count(case):
    bridge, _, work = case
    damaged = bytearray(work.read_bytes())
    struct.pack_into("<I", damaged, 260, 3)
    work.write_bytes(damaged)
    with pytest.raises(BridgeError, match="native active count"):
        prepare_deletion(bridge, str(work), sha(damaged), [1], "bad-count")


def test_applied_request_can_verify_a_native_saved_branch_and_reject_a_later_edit(case):
    bridge, _, work = case
    prepare_deletion(bridge, str(work), sha(work.read_bytes()), [1], "native-handoff")
    output = Path(bridge.apply("native-handoff")["output"])
    # Native save can alter unrelated file bookkeeping without changing records.
    saved = bytearray(output.read_bytes())
    saved[350] ^= 1
    output.write_bytes(saved)
    assert bridge.status("native-handoff")["state"] == "OUTPUT_CHANGED"
    verified = bridge.verify_output("native-handoff", str(output))
    assert verified["matches_candidate_records"] is True
    assert verified["native_reopen"] == "NOT_VERIFIED"
    bridge.prepare(str(output), sha(saved), [2], "later-handoff", attributes={"ID": 77})
    later = Path(bridge.apply("later-handoff")["output"])
    mismatch = bridge.verify_output("native-handoff", str(later))
    assert mismatch["matches_candidate_records"] is False
    assert mismatch["comparison"]["counts"]["changed"] == 1
    assert bridge.status("native-handoff")["state"] == "OUTPUT_CHANGED"


def test_delete_recovery_keeps_later_independent_edits_and_rejects_rebuilding(case):
    bridge, _, work = case
    prepare_deletion(bridge, str(work), sha(work.read_bytes()), [1], "delete")
    first = Path(bridge.apply("delete")["output"])
    with pytest.raises(BridgeError, match="DELETE_IDS"):
        prepare_deletion(bridge, str(first), sha(first.read_bytes()), [1], "again")
    bridge.prepare(str(first), sha(first.read_bytes()), [2], "later", attributes={"ID": 77})
    later = Path(bridge.apply("later")["output"])
    bridge.recover("delete", str(later), sha(later.read_bytes()), "recover-later")
    restored = bridge.inspect(bridge.apply("recover-later")["output"])
    assert [r["record_id"] for r in restored["rows"]] == [1, 2, 3, 4]
    assert [r["attributes"]["ID"] for r in restored["rows"]] == [9, 77, 9, 9]
    compacted = bridge.root / ("work/compacted" + work.suffix)
    # A freshly written file removes physical slots and changes the layout.
    _, reader, _ = read_native(later)
    Writer(reader.geodataframe).to_file(compacted)
    with pytest.raises(BridgeError, match="DELETE_RECOVERY_REQUIRES_SAME_LAYOUT"):
        bridge.recover("delete", str(compacted), sha(compacted.read_bytes()), "bad-restore")


def test_delete_identity_staleness_and_cancel_boundaries(case):
    bridge, original, work = case
    digest = sha(work.read_bytes())
    for i, ids in enumerate(([], [0], [True], [1, 1], [5], [-1], ["1"])):
        with pytest.raises(BridgeError, match="DELETE_IDS"):
            prepare_deletion(bridge, str(work), digest, ids, "invalid-" + str(i))
    with pytest.raises(BridgeError):
        prepare_deletion(bridge, str(original), digest, [1], "original")
    with pytest.raises(BridgeError, match="STALE_SOURCE"):
        prepare_deletion(bridge, str(work), "0" * 64, [1], "old-sha")
    preview = prepare_deletion(bridge, str(work), digest, [1], "preview")
    assert prepare_deletion(bridge, str(work), digest, [1], "preview") == preview
    with pytest.raises(BridgeError, match="REQUEST_ID_CONFLICT"):
        prepare_deletion(bridge, str(work), digest, [2], "preview")
    bridge.cancel("preview")
    with pytest.raises(BridgeError, match="cancelled"):
        bridge.apply("preview")
    prepare_deletion(bridge, str(work), digest, [1], "stale-on-apply")
    changed = bytearray(work.read_bytes())
    changed[-1] ^= 1
    work.write_bytes(changed)
    with pytest.raises(BridgeError, match="STALE_SOURCE"):
        bridge.apply("stale-on-apply")


def test_native_saved_tombstone_can_retain_attribute_payload(case):
    """Observed BAS60 deletion preserves the inactive slot's attribute payload."""
    import io
    from pymapgis import reader as native
    bridge,_,work=case
    prepare_deletion(bridge,str(work),sha(work.read_bytes()),[1],'native-shape')
    output=Path(bridge.apply('native-shape')['output'])
    blob,reader,entries=read_native(output)
    data=bytearray(blob)
    stream=io.BytesIO(blob)
    _,length,_=native._read_attribute_header(stream,entries[native._ATTR_HEADER_INDEX[reader.shapeType]][0])
    data[stream.tell()+length]=1
    output.write_bytes(data)
    actual=bridge.inspect(str(output))
    assert actual['count']==3
    assert [row['record_id'] for row in actual['rows']]==[2,3,4]
    # The native active-count header remains an independent consistency check.
    struct.pack_into('<I',data,260,4)
    output.write_bytes(data)
    with pytest.raises(BridgeError,match='INCONSISTENT_ACTIVE_FLAGS'):
        bridge.inspect(str(output))
