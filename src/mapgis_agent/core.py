"""Preserve native bytes; use the pinned upstream reader, not a second parser.

All mutations produce new project-local branches. No live document or original
file is overwritten. Physical record numbers differ from an attribute named ID.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import tempfile
from typing import Any

from pymapgis import Reader
from pymapgis import reader as native


class BridgeError(ValueError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_text(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def read_native(path: Path) -> tuple[bytes, Reader, list[tuple[int, int]]]:
    blob = path.read_bytes()
    if len(blob) < 817 or blob[:8] not in (b"WMAP`D21", b"WMAP`D22", b"WMAP`D23"):
        raise BridgeError("Unsupported or truncated MapGIS 6.x vector file")
    start = struct.unpack_from("<i", blob, 12)[0]
    if start < 16 or start + 160 > len(blob):
        raise BridgeError("Invalid native section table")
    sections = native._read_header_entries(io.BytesIO(blob), start, 16)
    for offset, length in sections:
        if offset < 0 or length < 0 or offset + length > len(blob):
            raise BridgeError("Native section is outside the file")
    reader = Reader(path, make_valid=False, read_graphic_params=True)
    _filter_deleted_rows(blob, reader, sections)
    if path.read_bytes() != blob:
        raise BridgeError("STALE_DURING_READ: native file changed while it was inspected")
    if len(reader.geom) != len(reader.data):
        raise BridgeError("Geometry/attribute row mapping is inconsistent")
    return blob, reader, sections


def _filter_deleted_rows(blob: bytes, reader: Reader, sections: list[tuple[int, int]]) -> None:
    """Keep native physical slot identity when native deletion leaves tombstones.

    The pinned upstream reader only filters polygons through topology. Point and
    line records still include inactive slots, so filtering must precede every
    inspection, patch or snapshot. Attribute ID values do not identify slots.
    """
    stream = io.BytesIO(blob)
    attr_start, attr_size = sections[native._ATTR_HEADER_INDEX[reader.shapeType]]
    count, length, _ = native._read_attribute_header(stream, attr_start)
    table_start = stream.tell()
    if length < 1 or table_start + count * length > attr_start + attr_size:
        raise BridgeError("Invalid native active-record table")
    physical = list(reader._polygon_active_ids) if reader.shapeType == "POLYGON" else list(range(1, len(reader.geom) + 1))
    geometry_flags = None
    native_count_matches = False
    if reader.shapeType != "POLYGON":
        size = 93 if reader.shapeType == "POINT" else 57
        geometry_flags = [blob[sections[0][0] + rid * size] for rid in physical]
        native_count_matches = (all(flag in (0, 1) for flag in geometry_flags)
                                and sum(geometry_flags) == struct.unpack_from("<I", blob, 260)[0])
    keep = []
    for row, record_id in enumerate(physical):
        if record_id >= count:
            raise BridgeError("Native physical record has no attribute slot")
        flag = blob[table_start + record_id * length]
        if flag not in (0, 1):
            raise BridgeError("Unknown native attribute active flag")
        if reader.shapeType != "POLYGON":
            geometry_flag = geometry_flags[row]
            if geometry_flag != flag:
                # BAS60 may retain the attribute payload of a deleted physical
                # slot. Native GetPnt returns -1 and the active-count header
                # excludes it. An active geometry with missing attributes, an
                # unknown flag or a contradictory count is still rejected.
                if not (geometry_flag == 0 and flag == 1 and native_count_matches):
                    raise BridgeError("INCONSISTENT_ACTIVE_FLAGS: geometry and attributes disagree")
            flag = geometry_flag
        if flag:
            keep.append(row)
    reader._bridge_physical_ids = [physical[row] for row in keep]
    if len(keep) != len(physical):
        reader.geom = [reader.geom[row] for row in keep]
        reader.data = reader.data.iloc[keep]
        for field in ("point_params", "line_params", "polygon_params"):
            frame = getattr(reader, field)
            if len(frame):
                setattr(reader, field, frame.iloc[keep])
        if reader.shapeType == "POLYGON":
            reader._polygon_active_ids = reader._bridge_physical_ids
        reader._build_geodataframe()


def native_ids(reader: Reader) -> list[int]:
    if hasattr(reader, "_bridge_physical_ids"):
        return list(reader._bridge_physical_ids)
    if reader.shapeType == "POLYGON":
        return list(reader._polygon_active_ids)
    return list(range(1, len(reader) + 1))


def annotation_text(blob: bytes, entries: list, record_id: int) -> str | None:
    position=entries[0][0]+record_id*93
    if blob[position+31]!=0:
        return None
    length,offset=struct.unpack_from("<Hi",blob,position+1)
    pool,size=entries[1]
    if offset<0 or offset+length>size:
        raise BridgeError("ANNOTATION_POOL_OUT_OF_BOUNDS")
    try:
        return blob[pool+offset:pool+offset+length].decode("gbk",errors="strict")
    except UnicodeDecodeError as exc:
        raise BridgeError("INVALID_GBK_ANNOTATION") from exc


def layout_signature(blob: bytes) -> str:
    """Record locations and attribute descriptors for the byte-offset fast path."""
    start = struct.unpack_from("<i", blob, 12)[0]
    entries = native._read_header_entries(io.BytesIO(blob), start, 16)
    attr_index = native._ATTR_HEADER_INDEX[{b"WMAP`D23": "POLYGON", b"WMAP`D22": "POINT", b"WMAP`D21": "LINE"}[blob[:8]]]
    attr_start, attr_size = entries[attr_index]
    if attr_start + 348 > len(blob):
        raise BridgeError("Attribute table is missing")
    fields = struct.unpack_from("<h", blob, attr_start + 322)[0]
    if fields < 0 or 348 + fields * 39 > attr_size:
        raise BridgeError("Attribute descriptors are outside their section")
    fixed = blob[:16] + blob[start:start + 160]
    fixed += blob[attr_start + 322:attr_start + 348 + fields * 39]
    return sha(struct.pack("<Q", len(blob)) + fixed)


class Bridge:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        for directory in ("work", "deliverables", "evidence"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "evidence" / "commands.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL, source TEXT NOT NULL,
                before_blob BLOB NOT NULL, after_blob BLOB NOT NULL,
                changes TEXT NOT NULL, receipt TEXT NOT NULL)""")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def owned_path(self, value: str | Path) -> Path:
        path = Path(value).resolve(strict=True)
        if not any(path.is_relative_to((self.root / d).resolve()) for d in ("work", "deliverables")):
            raise BridgeError("Writes require a project-owned work copy")
        if not path.is_file():
            raise BridgeError("Expected a native vector file")
        return path

    def destination_path(self, value: str | Path) -> Path:
        """Resolve a new task artifact without following a junction out of root."""
        path = Path(value).resolve()
        if not any(path.is_relative_to(self.root / d) for d in ("work", "deliverables")):
            raise BridgeError("Writes require a project-owned destination")
        return path

    @staticmethod
    def request_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
            raise BridgeError("request_id must be 1-80 ASCII letters, digits, _ or -")
        return value

    def inspect(self, file: str, offset: int = 0, limit: int = 10) -> dict:
        if not 0 <= offset or not 1 <= limit <= 100:
            raise BridgeError("offset must be nonnegative; limit must be 1-100")
        path = Path(file).resolve(strict=True)
        blob, reader, entries = read_native(path)
        ids = native_ids(reader)
        rows = []
        for row in range(offset, min(offset + limit, len(ids))):
            attrs = {str(k): scalar(v) for k, v in reader.data.iloc[row].items()}
            params = next((p for p in (reader.point_params, reader.line_params, reader.polygon_params)
                           if not p.empty), None)
            style = {} if params is None else {str(k): scalar(v) for k, v in params.iloc[row].items()}
            geometry = reader.geom[row]
            rows.append({"record_id": ids[row], "attributes": attrs, "style": style,
                         "geometry_type": geometry.geom_type,
                         "valid": bool(geometry.is_valid), "bounds": list(geometry.bounds)})
            if reader.shapeType=="POINT":
                rows[-1]["text"]=annotation_text(blob,entries,ids[row])
        return {"path": str(path), "sha256": sha(blob), "bytes": len(blob),
                "type": reader.shapeType, "count": len(reader),
                "crs": reader.crs.to_wkt() if reader.crs else None,
                "coordinate_context": {"projection_index": blob[109], "ellipsoid_index": blob[110],
                                       "stored_map_scale": struct.unpack_from("<d", blob, 143)[0],
                                       "unit": "NOT_VERIFIED"},
                "bounds": reader.bbox.tolist(), "fields": reader.fields,
                "rows": rows, "next_offset": offset + len(rows) if offset + len(rows) < len(ids) else None,
                "selection_source": "EXPLICIT_PHYSICAL_RECORDS_NOT_DESKTOP_SELECTION",
                "warnings": ["CRS and units require the originating MapGIS index tables."] if not reader.crs else [],
                "native_rendering": "NOT_VERIFIED_BY_FILE_READ"}

    def copy(self, source: str, request_id: str) -> dict:
        request_id = self.request_id(request_id)
        path = Path(source).resolve(strict=True)
        blob, reader, _ = read_native(path)
        directory = self.root / "deliverables" / "workspaces" / request_id
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            receipt_path = directory / "source.json"
            if not receipt_path.exists():
                raise BridgeError("Copy outcome is incomplete; inspect the existing directory")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt["source"] != str(path) or receipt["source_sha256"] != sha(blob):
                raise BridgeError("request_id was already used for a different source state")
            if sha(Path(receipt["path"]).read_bytes()) != sha(blob):
                raise BridgeError("Existing work copy changed; refusing to replace it")
            return receipt
        target = directory / path.name
        with target.open("xb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        receipt = {"source": str(path), "source_sha256": sha(blob), "path": str(target),
                   "sha256": sha(target.read_bytes()), "count": len(reader), "type": reader.shapeType}
        (directory / "source.json").write_text(json_text(receipt), encoding="utf-8")
        return receipt

    def _build_patch(self, path: Path, expected_sha256: str, record_ids: list[int],
                     attributes: dict, style: dict) -> tuple[bytes, bytes, list[dict]]:
        before, reader, entries = read_native(path)
        if sha(before) != expected_sha256:
            raise BridgeError("STALE_SOURCE: read this file again before preparing a change")
        if not record_ids or len(record_ids) != len(set(record_ids)):
            raise BridgeError("Provide a nonempty, unique set of physical record IDs")
        active = native_ids(reader)
        if any(type(i) is not int or i not in active for i in record_ids):
            raise BridgeError("A requested physical record ID does not exist")
        if not attributes and not style:
            raise BridgeError("Provide an attribute or style change")
        mutable = bytearray(before)
        changes = []

        def change(offset: int, value: bytes, rid: int, group: str, field: str):
            end = offset + len(value)
            if offset < 817 or end > len(mutable):
                raise BridgeError("Patch span is outside a native data record")
            original = before[offset:end]
            if original == value:
                return
            if any(offset < c["offset"] + len(bytes.fromhex(c["after"])) and end > c["offset"] for c in changes):
                raise BridgeError("Overlapping patch fields are not supported")
            mutable[offset:end] = value
            changes.append({"record_id": rid, "group": group, "field": field,
                            "offset": offset, "before": original.hex(), "after": value.hex()})

        attr_index = native._ATTR_HEADER_INDEX[reader.shapeType]
        attr_start, attr_size = entries[attr_index]
        stream = io.BytesIO(before)
        count, record_length, fields = native._read_attribute_header(stream, attr_start)
        table_start = stream.tell()
        if count < 1 or record_length <= 0 or table_start + count * record_length > attr_start + attr_size:
            raise BridgeError("Unsupported attribute storage layout")
        field_map = {f.name: f for f in fields}
        if len(field_map) != len(fields):
            raise BridgeError("Duplicate attribute field names require a native SDK")
        for name, value in attributes.items():
            if name not in field_map:
                raise BridgeError(f"Unknown existing attribute: {name}")
            field = field_map[name]
            next_offsets = [f.offset for f in fields if f.offset > field.offset]
            storage = min(next_offsets, default=record_length) - field.offset
            if field.offset < 0 or storage <= 0 or field.offset + storage > record_length:
                raise BridgeError("Invalid attribute field span")
            if field.type_code == 0:
                if not isinstance(value, str) or "\x00" in value:
                    raise BridgeError("String attributes require text without NUL")
                try:
                    encoded = value.encode("gbk", errors="strict")
                except UnicodeEncodeError as error:
                    raise BridgeError("Attribute contains characters unavailable in native GBK") from error
                if len(encoded) >= storage:
                    raise BridgeError("Attribute exceeds fixed native field capacity; no truncation performed")
                encoded = encoded.ljust(storage, b"\x00")
            else:
                fmt = {1: "B", 2: "h", 3: "i", 4: "f", 5: "d"}.get(field.type_code)
                if fmt is None:
                    raise BridgeError("Date/time field editing is not yet verified")
                encoded = pack_number(fmt, value)
                if len(encoded) > storage:
                    raise BridgeError("Numeric field storage is too small")
            for rid in record_ids:
                if rid >= count:
                    raise BridgeError("Attribute row for this record is missing")
                change(table_start + rid * record_length + field.offset, encoded, rid, "attributes", name)

        for rid in record_ids:
            for name, value in style.items():
                if name != "angle" and isinstance(value, (int, float)) and value < 0:
                    raise BridgeError("Style indexes and widths must be nonnegative")
                section, size, offset, fmt = style_location(reader.shapeType, name, before, entries, rid)
                if (rid + 1) * size > entries[section][1]:
                    raise BridgeError("Graphic parameter record is missing")
                encoded = pack_number(fmt, value)
                change(entries[section][0] + rid * size + offset, encoded, rid, "style", name)
        after = bytes(mutable)
        if len(before) != len(after) or layout_signature(before) != layout_signature(after):
            raise BridgeError("Patch unexpectedly changed the native storage layout")
        with tempfile.TemporaryDirectory(dir=self.root / "work") as temp:
            candidate = Path(temp) / path.name
            candidate.write_bytes(after)
            _, check, _ = read_native(candidate)
            if native_ids(check) != active or len(reader) != len(check):
                raise BridgeError("Physical record identity changed")
            if any(a.wkb != b.wkb for a, b in zip(reader.geom, check.geom)):
                raise BridgeError("Geometry changed during an attribute/style edit")
        return before, after, changes

    def prepare(self, file: str, expected_sha256: str, record_ids: list[int],
                request_id: str, attributes: dict | None = None, style: dict | None = None) -> dict:
        path = self.owned_path(file)
        request_id = self.request_id(request_id)
        payload = {"kind": "PATCH", "file": str(path), "source_sha256": expected_sha256,
                   "record_ids": record_ids, "attributes": attributes or {}, "style": style or {}}
        payload_hash = sha(json_text(payload).encode())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise BridgeError("REQUEST_ID_CONFLICT: use a new request_id for different parameters")
                return self._receipt(existing)
            before, after, changes = self._build_patch(path, expected_sha256, record_ids,
                                                        attributes or {}, style or {})
            receipt = {"id": request_id, "kind": "PATCH", "source": str(path),
                       "source_sha256": sha(before), "candidate_sha256": sha(after),
                       "output": str(self.root / "deliverables" / "patches" / request_id / path.name),
                       "changes": changes, "changed_bytes": sum(a != b for a, b in zip(before, after)),
                       "geometry_unchanged": True, "untargeted_bytes_unchanged": True,
                       "desktop_selection": "NOT_USED", "native_reopen": "NOT_VERIFIED"}
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",
                       (request_id, payload_hash, "PREPARED", str(path), before, after,
                        json_text(changes), json_text(receipt)))
        return {**receipt, "state": "PREPARED"}

    def _receipt(self, row: sqlite3.Row) -> dict:
        return {**json.loads(row["receipt"]), "state": row["state"]}

    def status(self, request_id: str) -> dict:
        self.request_id(request_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise BridgeError("Unknown request_id")
            result = self._receipt(row)
            output = Path(result["output"])
            result["output_exists"] = output.exists()
            result["output_matches"] = output.exists() and sha(output.read_bytes()) == sha(row["after_blob"])
            if result["state"] == "APPLIED" and not result["output_matches"]:
                result["state"] = "OUTPUT_CHANGED"
            return result

    def apply(self, request_id: str) -> dict:
        self.request_id(request_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise BridgeError("Unknown request_id")
            receipt = self._receipt(row)
            if row["state"] == "CANCELLED":
                raise BridgeError("This request was cancelled")
            if row["state"] not in ("PREPARED", "APPLIED"):
                raise BridgeError("Request is not prepared; reconcile its native execution first")
            output = Path(receipt["output"])
            if output.exists():
                if sha(output.read_bytes()) != sha(row["after_blob"]):
                    raise BridgeError("OUTPUT_CONFLICT: existing output changed; not replacing or replaying")
                db.execute("UPDATE commands SET state='APPLIED' WHERE id=?", (request_id,))
                return {**receipt, "state": "APPLIED", "reconciled_existing_output": True}
            if row["state"] == "APPLIED":
                raise BridgeError("OUTPUT_MISSING: previously applied output was removed; use a new request")
            if sha(Path(row["source"]).read_bytes()) != sha(row["before_blob"]):
                raise BridgeError("STALE_SOURCE: source changed after preview")
            if receipt.get("kind") == "NATIVE_PLOT":
                if any(sha(Path(item["file"]).read_bytes()) != item["sha256"] for item in receipt["sources"]):
                    raise BridgeError("STALE_SOURCE: plot layer changed after preview")
            output.parent.parent.mkdir(parents=True, exist_ok=True)
            if output.parent.exists():
                raise BridgeError("INCOMPLETE_OUTPUT: inspect the existing request directory")
            output.parent.mkdir()
            with output.open("xb") as stream:
                stream.write(row["after_blob"])
                stream.flush()
                os.fsync(stream.fileno())
            if receipt.get("kind") == "NATIVE_PLOT":
                from .native_plot import validate_eps
                validate_eps(output, receipt["view"]["paper_mm"])
            else:
                read_native(output)
            if sha(output.read_bytes()) != sha(row["after_blob"]):
                raise BridgeError("Output readback failed")
            db.execute("UPDATE commands SET state='APPLIED' WHERE id=?", (request_id,))
        return {**receipt, "state": "APPLIED", "reconciled_existing_output": False}

    def cancel(self, request_id: str) -> dict:
        self.request_id(request_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if not row:
                raise BridgeError("Unknown request_id")
            if row["state"] != "PREPARED" or Path(json.loads(row["receipt"])["output"]).exists():
                raise BridgeError("An applied or uncertain request must be reconciled, not cancelled")
            db.execute("UPDATE commands SET state='CANCELLED' WHERE id=?", (request_id,))
        return {"id": request_id, "state": "CANCELLED"}

    def verify_output(self, request_id: str, file: str) -> dict:
        """Compare a native-saved file against this request's immutable candidate."""
        from .comparison import compare_native_files

        self.request_id(request_id)
        with self.connect() as db:
            row = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if row is None or row["state"] != "APPLIED":
                raise BridgeError("Verification requires a previously applied request")
            receipt = self._receipt(row)
            candidate = row["after_blob"]
        actual = Path(file).resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="verify-request-", dir=self.root / "work") as folder:
            baseline = Path(folder) / Path(receipt["output"]).name
            baseline.write_bytes(candidate)
            comparison = compare_native_files(str(baseline), str(actual))
        # The baseline is the persisted BLOB; its temporary path is not a resource.
        comparison["before"]["path"] = None
        return {"id": request_id, "kind": receipt.get("kind", "PATCH"), "file": str(actual),
                "candidate_sha256": sha(candidate), "candidate_source": "persisted request candidate",
                "matches_candidate_records": comparison["records_equal"] and not comparison["metadata_changes"],
                "comparison": comparison, "native_reopen": "NOT_VERIFIED",
                "scope": "saved disk records, text, graphic parameters and coordinate header; native session requires separate binding"}

    def recover(self, applied_request: str, later_file: str, expected_sha256: str, request_id: str) -> dict:
        later_path = self.owned_path(later_file)
        request_id = self.request_id(request_id)
        payload_hash = sha(json_text({"kind": "RECOVERY", "applied_request": applied_request,
                                     "later_file": str(later_path), "sha256": expected_sha256}).encode())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM commands WHERE id=?", (request_id,)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise BridgeError("REQUEST_ID_CONFLICT")
                return self._receipt(old)
            original = db.execute("SELECT * FROM commands WHERE id=?", (applied_request,)).fetchone()
            if original is None or original["state"] != "APPLIED":
                raise BridgeError("Recovery requires a previously applied request")
            if json.loads(original["receipt"]).get("kind") == "NATIVE_PLOT":
                raise BridgeError("Plot output is a new artifact; its input files were preserved")
            later, later_reader, later_entries = read_native(later_path)
            if sha(later) != expected_sha256:
                raise BridgeError("STALE_SOURCE: later file changed")
            recovered = bytearray(later)
            changes = json.loads(original["changes"])
            pristine_native_snapshot = (json.loads(original["receipt"]).get("kind") == "NATIVE"
                                        and not changes and original["before_blob"] != original["after_blob"])
            if pristine_native_snapshot:
                if later != original["after_blob"]:
                    raise BridgeError("NATIVE_RECOVERY_CONFLICT: structural rollback requires the unchanged saved candidate")
                changes = [{"offset": 0, "before": original["before_blob"].hex(),
                            "after": original["after_blob"].hex(), "record_id": None,
                            "group": "native", "field": "pristine_saved_snapshot"}]
            structural = layout_signature(later) != layout_signature(original["after_blob"])
            if json.loads(original["receipt"]).get("kind") == "NATIVE" and (
                    structural or (not changes and original["before_blob"] != original["after_blob"])):
                raise BridgeError("NATIVE_RECOVERY_REQUIRES_SAME_LAYOUT: use the preserved source snapshot")
            if structural:
                if json.loads(original["receipt"]).get("kind") == "DELETE":
                    raise BridgeError("DELETE_RECOVERY_REQUIRES_SAME_LAYOUT: native compaction or rebuilding changed the slots")
                changes = self._relocate_recovery(original["after_blob"], later, later_reader,
                                                   later_entries, changes, later_path.suffix)
            inverse = []
            for item in changes:
                start = item["offset"]
                before, after = bytes.fromhex(item["before"]), bytes.fromhex(item["after"])
                if later[start:start + len(after)] != after:
                    raise BridgeError("FIELD_CONFLICT: later work changed the same field; no overwrite")
                recovered[start:start + len(after)] = before
                inverse.append({**item, "before": item["after"], "after": item["before"]})
            receipt = {"id": request_id, "kind": "RECOVERY", "source": str(later_path),
                       "source_sha256": sha(later), "candidate_sha256": sha(recovered),
                       "output": str(self.root / "deliverables" / "recoveries" / request_id / later_path.name),
                       "changes": inverse, "retained_later_independent_bytes": True,
                       "recovery_mapping": ("pristine_native_snapshot" if pristine_native_snapshot else
                                            "unique_unchanged_physical_record" if structural else "same_layout_byte_spans"),
                       "native_reopen": "NOT_VERIFIED"}
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)",
                       (request_id, payload_hash, "PREPARED", str(later_path), later, bytes(recovered),
                        json_text(inverse), json_text(receipt)))
        return {**receipt, "state": "PREPARED"}

    def _relocate_recovery(self, original_blob: bytes, later: bytes, reader: Reader,
                           entries: list, changes: list[dict], suffix: str) -> list[dict]:
        """Handle changed storage without guessing through renumbering or clones.

        The same physical slot must still have its exact geometry and untouched
        attributes, and that semantic identity must be unique in the later file.
        Only field spans are relocated; all newer geometry and bytes stay intact.
        """
        with tempfile.TemporaryDirectory(dir=self.root / 'work') as temp:
            source = Path(temp) / ('recorded-after' + suffix)
            source.write_bytes(original_blob)
            _, original, _ = read_native(source)
        if original.shapeType != reader.shapeType or original.fields != reader.fields:
            raise BridgeError('STRUCTURAL_CONFLICT: native type or attribute schema changed')
        old_ids, new_ids = native_ids(original), native_ids(reader)
        for record in {change['record_id'] for change in changes}:
            if record not in old_ids or record not in new_ids:
                raise BridgeError('STRUCTURAL_CONFLICT: recovery target was deleted or renumbered')
            old_index, new_index = old_ids.index(record), new_ids.index(record)
            changed_attributes = {change['field'] for change in changes
                                  if change['record_id'] == record and change['group'] == 'attributes'}
            fields = [field for field in original.data.columns if field not in changed_attributes]
            old_values = [scalar(original.data.iloc[old_index][field]) for field in fields]
            matches = [new_ids[index] for index, geom in enumerate(reader.geom)
                       if geom.equals(original.geom[old_index])
                       and [scalar(reader.data.iloc[index][field]) for field in fields] == old_values]
            if matches != [record] or reader.geom[new_index].wkb != original.geom[old_index].wkb:
                raise BridgeError('STRUCTURAL_CONFLICT: changed or ambiguous record identity')
        attr_start, attr_size = entries[native._ATTR_HEADER_INDEX[reader.shapeType]]
        stream = io.BytesIO(later)
        count, length, fields = native._read_attribute_header(stream, attr_start)
        table_start = stream.tell()
        if length <= 0 or table_start + count * length > attr_start + attr_size:
            raise BridgeError('STRUCTURAL_CONFLICT: invalid attribute storage')
        field_map = {field.name: field for field in fields}
        if len(field_map) != len(fields):
            raise BridgeError('STRUCTURAL_CONFLICT: duplicate attribute descriptors')
        relocated = []
        for change in changes:
            record, name = change['record_id'], change['field']
            width = len(bytes.fromhex(change['after']))
            if change['group'] == 'style':
                section, size, offset, fmt = style_location(reader.shapeType, name, later, entries, record)
                if struct.calcsize('<' + fmt) != width or (record + 1) * size > entries[section][1]:
                    raise BridgeError('STRUCTURAL_CONFLICT: graphic parameter storage changed')
                start = entries[section][0] + record * size + offset
            elif change['group'] == 'attributes' and name in field_map:
                field = field_map[name]
                following = min((f.offset for f in fields if f.offset > field.offset), default=length)
                if record >= count or field.offset < 0 or width > following - field.offset:
                    raise BridgeError('STRUCTURAL_CONFLICT: attribute storage changed')
                start = table_start + record * length + field.offset
            else:
                raise BridgeError('STRUCTURAL_CONFLICT: unknown recorded field')
            if start < 817 or start + width > len(later):
                raise BridgeError('STRUCTURAL_CONFLICT: relocated field is outside the file')
            relocated.append({**change, 'offset': start})
        return relocated


def pack_number(fmt: str, value: Any) -> bytes:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BridgeError("Numeric values must be finite numbers")
    if fmt in ("B", "h", "H", "i"):
        if int(value) != value:
            raise BridgeError("An integer field cannot store a fractional value")
        value = int(value)
    try:
        return struct.pack("<" + fmt, value)
    except (struct.error, OverflowError) as error:
        raise BridgeError("Value is outside native field storage range") from error


def style_location(kind: str, name: str, blob: bytes, entries: list, rid: int) -> tuple:
    if kind == "LINE":
        mapping = {"color": (native._LINE_COLOR_OFFSET, "h"),
                   "linetype": (native._LINE_LINETYPE_OFFSET, "h"),
                   "width": (native._LINE_WIDTH_OFFSET, "f"),
                   "aux_color": (native._LINE_AUX_COLOR_OFFSET, "h"),
                   "layer": (native._LINE_LAYER_OFFSET, "B")}
        location = (0, 57)
    elif kind == "POINT":
        point_kind = blob[entries[0][0] + rid * 93 + native._POINT_TYPE_OFFSET]
        mapping = {"color": (native._POINT_COLOR_OFFSET, "B"),
                   "angle": (native._POINT_ANGLE_OFFSET, "f"),
                   "layer": (native._POINT_LAYER_OFFSET, "B")}
        if point_kind == 1:
            mapping["symbol_no"] = (native._POINT_SYMBOL_NO_OFFSET, "h")
        elif point_kind != 0:
            raise BridgeError("Unknown native point kind")
        location = (0, 93)
    else:
        mapping = {"fill_color": (native._POLY_FILL_COLOR_OFFSET, "H"),
                   "pattern": (native._POLY_PATTERN_OFFSET, "H"),
                   "pattern_color": (native._POLY_PATTERN_COLOR_OFFSET, "B"),
                   "layer": (native._POLY_LAYER_OFFSET, "B")}
        location = (8, 40)
    if name not in mapping:
        raise BridgeError(f"Unverified or unsupported {kind} style field: {name}")
    offset, fmt = mapping[name]
    return (*location, offset, fmt)
