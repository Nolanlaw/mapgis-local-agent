"""Tombstone explicit WT/WL records without rebuilding the native file.

This file-side path clears physical 93-byte point / 57-byte line slots and
the corresponding attribute flags. BAS60 saved files may retain the deleted
slot's attribute payload; core.read_native validates the native active count. Coordinate pools, styles and native
auxiliary sections are retained. WP deletion requires topology-aware handling.
"""
from __future__ import annotations

import io
import struct
import tempfile
from pathlib import Path

from pymapgis import reader as native

from .core import Bridge, BridgeError, json_text, native_ids, read_native, sha


def prepare_deletion(bridge: Bridge, file: str, expected_sha256: str,
                     record_ids: list[int], request_id: str) -> dict:
    path = bridge.owned_path(file)
    request_id = bridge.request_id(request_id)
    payload = {"kind": "DELETE", "file": str(path), "source_sha256": expected_sha256,
               "record_ids": record_ids, "format": "tombstone-with-native-count-v1"}
    payload_hash = sha(json_text(payload).encode())
    with bridge.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
        if old:
            if old["payload_hash"] != payload_hash:
                raise BridgeError("REQUEST_ID_CONFLICT")
            return bridge._receipt(old)
        before, reader, sections = read_native(path)
        if sha(before) != expected_sha256:
            raise BridgeError("STALE_SOURCE: inspect the current file before deletion")
        if reader.shapeType not in ("POINT", "LINE"):
            raise BridgeError("DELETE_TYPE: only WT points and WL lines; WP needs native topology editing")
        active = native_ids(reader)
        # BAS60 loads this value independently from the slot flags. Leaving it
        # unchanged makes the native workspace report deleted records as active.
        if struct.unpack_from("<I", before, 260)[0] != len(active):
            raise BridgeError("DELETE_LAYOUT: native active count disagrees with record flags")
        if (not record_ids or any(type(i) is not int for i in record_ids)
                or len(record_ids) != len(set(record_ids))
                or not set(record_ids).issubset(active)):
            raise BridgeError("DELETE_IDS: provide unique active physical record IDs")
        attr_start, attr_size = sections[native._ATTR_HEADER_INDEX[reader.shapeType]]
        stream = io.BytesIO(before)
        count, length, _ = native._read_attribute_header(stream, attr_start)
        table = stream.tell()
        size = 93 if reader.shapeType == "POINT" else 57
        if (length < 1 or table + count * length > attr_start + attr_size
                or (max(record_ids) + 1) * size > sections[0][1]):
            raise BridgeError("DELETE_LAYOUT: missing geometry or attribute slots")
        mutable = bytearray(before)
        changes = []
        for rid in record_ids:
            for group, offset in (("geometry", sections[0][0] + rid * size),
                                  ("attributes", table + rid * length)):
                if before[offset] != 1:
                    raise BridgeError("DELETE_LAYOUT: inconsistent active flags")
                mutable[offset] = 0
                changes.append({"record_id": rid, "group": group, "field": "active",
                                "offset": offset, "before": "01", "after": "00"})
        count_after = struct.pack("<I", len(active) - len(record_ids))
        mutable[260:264] = count_after
        changes.append({"record_id": None, "group": "header", "field": "active_count",
                        "offset": 260, "before": before[260:264].hex(), "after": count_after.hex()})
        after = bytes(mutable)
        deleted = set(record_ids)
        keep = [i for i, rid in enumerate(active) if rid not in deleted]
        with tempfile.TemporaryDirectory(dir=bridge.root / "work") as temp:
            candidate = Path(temp) / path.name
            candidate.write_bytes(after)
            _, check, _ = read_native(candidate)
            if (native_ids(check) != [active[i] for i in keep]
                    or [g.wkb for g in check.geom] != [reader.geom[i].wkb for i in keep]
                    or not check.data.equals(reader.data.iloc[keep])):
                raise BridgeError("DELETE_READBACK: surviving records changed")
        receipt = {"id": request_id, "kind": "DELETE", "source": str(path),
                   "source_sha256": sha(before), "candidate_sha256": sha(after),
                   "output": str(bridge.root / "deliverables/deletions" / request_id / path.name),
                   "record_ids": record_ids, "count_before": len(active), "count_after": len(keep),
                   "changes": changes, "changed_bytes": sum(a != b for a, b in zip(before, after)),
                   "geometry_unchanged": False, "surviving_records_unchanged": True,
                   "untargeted_bytes_unchanged": True, "compacted": False,
                   "desktop_selection": "NOT_USED", "native_reopen": "NOT_VERIFIED"}
        db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",
                   (request_id, payload_hash, "PREPARED", str(path), before, after,
                    json_text(changes), json_text(receipt)))
    return {**receipt, "state": "PREPARED"}
