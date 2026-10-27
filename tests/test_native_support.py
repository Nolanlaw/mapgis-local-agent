from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_support import record, verify
from mapgis_agent.core import BridgeError, sha
from test_bridge import case


def test_native_readback_uses_physical_record_number(case):
    _, _, work = case
    result = record(work, 1)
    assert result["record_id"] == 1
    assert result["attributes"]["ID"] == 9
    with pytest.raises(BridgeError, match="does not exist"):
        record(work, 9)


def test_native_save_verifier_accepts_exact_delta_and_rejects_extra_records(case):
    bridge, original, work = case
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "one", style={"color": 2})
    one = Path(bridge.apply("one")["output"])
    expected = [{"record_id": 1, "field": "color", "after": 2}]
    assert verify(original, one, expected)["verified"]
    bridge.prepare(str(work), sha(work.read_bytes()), [1, 2], "two", style={"color": 2})
    two = Path(bridge.apply("two")["output"])
    result = verify(original, two, expected)
    assert not result["verified"] and not result["expected_changes_match"]


def test_native_save_verifier_rejects_unrequested_attribute_change(case):
    bridge, original, work = case
    bridge.prepare(str(work), sha(work.read_bytes()), [1], "extra", style={"color": 2}, attributes={"类别": "滑坡"})
    changed = Path(bridge.apply("extra")["output"])
    result = verify(original, changed, [{"record_id": 1, "field": "color", "after": 2}])
    assert result["geometry_wkb_equal"] and not result["attributes_equal"] and not result["verified"]
