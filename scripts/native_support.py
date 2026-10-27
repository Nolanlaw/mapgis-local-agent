"""Read-only support for the Sky desktop adapter; never sends native UI input."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mapgis_agent.core import BridgeError, native_ids, read_native, scalar, sha
from mapgis_agent.native_state import NativeStateError, live_state


def identity(root: Path, pid: int) -> dict:
    config = json.loads((root / "config.local.json").read_text(encoding="utf-8"))
    executable = (Path(config["mapgis_installation"]) / "program/mdiedit6x.exe").resolve()
    candidates = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            if process.info["exe"] and os.path.normcase(process.info["exe"]) == os.path.normcase(str(executable)):
                candidates.append(process.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Sky's window id is opaque; do not assume it is a Win32 HWND or guess its PID.
    if len(candidates) != 1 or candidates[0]["pid"] != pid:
        raise BridgeError("AMBIGUOUS_PROCESS: require one editor with the configured executable and exact PID")
    return {**candidates[0], "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}


def record(file: Path, record_id: int) -> dict:
    blob, reader, _ = read_native(file)
    ids = native_ids(reader)
    if record_id not in ids:
        raise BridgeError("Native physical record does not exist")
    row = ids.index(record_id)
    params = {"POLYGON": reader.polygon_params, "LINE": reader.line_params, "POINT": reader.point_params}[reader.shapeType]
    return {"path": str(file.resolve()), "sha256": sha(blob), "record_id": record_id,
            "type": reader.shapeType, "geometry_sha256": sha(reader.geom[row].wkb),
            "attributes": {str(k): scalar(v) for k, v in reader.data.iloc[row].items()},
            "style": {str(k): scalar(v) for k, v in params.iloc[row].items()}}


def verify(before: Path, after: Path, expected: list[dict]) -> dict:
    old_blob, old, _ = read_native(before)
    new_blob, new, _ = read_native(after)
    old_ids, new_ids = native_ids(old), native_ids(new)
    ids_equal = old.shapeType == new.shapeType and old_ids == new_ids
    geometry_equal = ids_equal and [g.wkb for g in old.geom] == [g.wkb for g in new.geom]
    attributes_equal = old.data.equals(new.data)
    changes = []
    if ids_equal:
        old_params = {"POLYGON": old.polygon_params, "LINE": old.line_params, "POINT": old.point_params}[old.shapeType]
        new_params = {"POLYGON": new.polygon_params, "LINE": new.line_params, "POINT": new.point_params}[new.shapeType]
        if list(old_params.columns) != list(new_params.columns):
            raise BridgeError("Native style schema changed")
        for index, physical_id in enumerate(old_ids):
            for field in old_params.columns:
                left, right = scalar(old_params.iloc[index][field]), scalar(new_params.iloc[index][field])
                if left != right:
                    changes.append({"record_id": physical_id, "field": str(field), "before": left, "after": right})
    actual_delta = [{k: v for k, v in change.items() if k != "before"} for change in changes]
    normalize = lambda rows: sorted(rows, key=lambda row: (row["record_id"], row["field"]))
    expected_matches = normalize(actual_delta) == normalize(expected)
    return {"verified": bool(ids_equal and geometry_equal and attributes_equal and expected_matches),
            "before_sha256": sha(old_blob), "after_sha256": sha(new_blob),
            "before_bytes": len(old_blob), "after_bytes": len(new_blob), "records": len(new),
            "ids_equal": ids_equal, "geometry_wkb_equal": geometry_equal,
            "attributes_equal": attributes_equal, "style_changes": changes,
            "expected_changes_match": expected_matches}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    proc = sub.add_parser("identity")
    proc.add_argument("root", type=Path)
    proc.add_argument("pid", type=int)
    live = sub.add_parser("live")
    live.add_argument("root", type=Path)
    live.add_argument("pid", type=int)
    rec = sub.add_parser("record")
    rec.add_argument("file", type=Path)
    rec.add_argument("record_id", type=int)
    check = sub.add_parser("verify")
    check.add_argument("before", type=Path)
    check.add_argument("after", type=Path)
    check.add_argument("expected_json")
    args = parser.parse_args()
    try:
        if args.action == "identity":
            result = identity(args.root, args.pid)
        elif args.action == "live":
            result = live_state(args.root, args.pid)
        elif args.action == "record":
            result = record(args.file, args.record_id)
        else:
            result = verify(args.before, args.after, json.loads(args.expected_json))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except (ValueError, OSError, psutil.Error, NativeStateError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
