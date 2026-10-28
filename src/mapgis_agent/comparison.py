"""Bounded read-only native result comparison tool for MapGIS Agent.

Compares two saved native MapGIS 6.x files (WT/WL/WP) by physical record IDs,
reporting added, removed, changed, and unchanged records.

This comparison is a read-only diff of serialized records and physical slots;
it is NOT proof of live UI presentation or desktop reopen acceptance.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path
from typing import Any

import pandas as pd
from pymapgis import Reader

from .core import BridgeError, annotation_text, native_ids, read_native, scalar, sha


def _safe_scalar(value: Any) -> Any:
    """Normalize pandas / numpy / float values to JSON-safe primitives or None."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    val = scalar(value)
    if isinstance(val, float) and (math.isnan(val) or not math.isfinite(val)):
        return None
    return val


def _values_equal(v1: Any, v2: Any) -> bool:
    """Determine equality safely without pandas NA ambiguity or bool-int coercion."""
    if v1 is None and v2 is None:
        return True
    if v1 is None or v2 is None:
        return False
    if isinstance(v1, bool) != isinstance(v2, bool):
        return False
    return bool(v1 == v2)


def _extract_attributes(reader: Reader, row_idx: int) -> dict[str, Any]:
    """Extract field-level attributes for a specific row index."""
    if reader.data is None or reader.data.empty or row_idx >= len(reader.data):
        return {}
    row_series = reader.data.iloc[row_idx]
    return {str(k): _safe_scalar(v) for k, v in row_series.items()}


def _extract_style(reader: Reader, row_idx: int) -> dict[str, Any]:
    """Extract graphic parameters for a specific row index."""
    for field_name in ("point_params", "line_params", "polygon_params"):
        frame = getattr(reader, field_name, None)
        if frame is not None and not frame.empty and row_idx < len(frame):
            row_series = frame.iloc[row_idx]
            return {str(k): _safe_scalar(v) for k, v in row_series.items()}
    return {}


def _diff_dicts(d_before: dict[str, Any], d_after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compute deterministic field-level differences between two dictionaries."""
    diff: dict[str, dict[str, Any]] = {}
    all_keys = sorted(set(d_before.keys()) | set(d_after.keys()))
    for k in all_keys:
        in_before = k in d_before
        in_after = k in d_after
        val_b = d_before.get(k)
        val_a = d_after.get(k)
        if in_before and in_after:
            if not _values_equal(val_b, val_a):
                diff[k] = {"before": val_b, "after": val_a}
        elif in_before and not in_after:
            diff[k] = {"before": val_b, "after": None}
        elif not in_before and in_after:
            diff[k] = {"before": None, "after": val_a}
    return diff


def compare_native_files(before: str, after: str, limit: int = 100) -> dict[str, Any]:
    """Compare two saved native MapGIS files by physical record slots.

    Args:
        before: Path to baseline native file (WT, WL, WP).
        after: Path to candidate native file.
        limit: Maximum number of row changes to return (0 to 1000).

    Returns:
        Dictionary containing source fingerprints, physical-ID counts,
        bounded row changes, and truncation status.
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise BridgeError("limit must be an integer")
    if limit < 0:
        raise BridgeError("limit must be non-negative")
    if limit > 1000:
        raise BridgeError("limit exceeds maximum allowed limit (1000)")

    before_path = Path(before)
    if not before_path.exists():
        raise BridgeError(f"File not found: {before}")
    after_path = Path(after)
    if not after_path.exists():
        raise BridgeError(f"File not found: {after}")

    blob_before, reader_before, sections_before = read_native(before_path)
    blob_after, reader_after, sections_after = read_native(after_path)

    if reader_before.shapeType != reader_after.shapeType:
        raise BridgeError(
            f"Incompatible native types: {reader_before.shapeType} vs {reader_after.shapeType}"
        )

    before_ids = native_ids(reader_before)
    after_ids = native_ids(reader_after)

    before_id_set = set(before_ids)
    after_id_set = set(after_ids)

    added_ids = [rid for rid in after_ids if rid not in before_id_set]
    removed_ids = [rid for rid in before_ids if rid not in after_id_set]
    common_ids = [rid for rid in before_ids if rid in after_id_set]

    before_row_map = {rid: idx for idx, rid in enumerate(before_ids)}
    after_row_map = {rid: idx for idx, rid in enumerate(after_ids)}

    changed_ids: list[int] = []
    unchanged_ids: list[int] = []
    common_changes_map: dict[int, dict[str, Any]] = {}

    for rid in common_ids:
        r_before = before_row_map[rid]
        r_after = after_row_map[rid]

        geom_b = reader_before.geom[r_before]
        geom_a = reader_after.geom[r_after]
        wkb_b = geom_b.wkb
        wkb_a = geom_a.wkb
        geom_changed = (wkb_b != wkb_a)

        attrs_b = _extract_attributes(reader_before, r_before)
        attrs_a = _extract_attributes(reader_after, r_after)
        attr_diff = _diff_dicts(attrs_b, attrs_a)

        style_b = _extract_style(reader_before, r_before)
        style_a = _extract_style(reader_after, r_after)
        style_diff = _diff_dicts(style_b, style_a)
        text_b = annotation_text(blob_before, sections_before, rid) if reader_before.shapeType == "POINT" else None
        text_a = annotation_text(blob_after, sections_after, rid) if reader_after.shapeType == "POINT" else None
        text_changed = text_b != text_a

        if geom_changed or attr_diff or style_diff or text_changed:
            changed_ids.append(rid)
            if len(common_changes_map) >= limit:
                continue
            geom_diff = {
                "before_wkb_sha256": sha(wkb_b),
                "after_wkb_sha256": sha(wkb_a),
            } if geom_changed else None

            changed_fields: list[str] = []
            if geom_changed:
                changed_fields.append("geometry")
            changed_fields.extend(sorted(attr_diff.keys()))
            changed_fields.extend(sorted(style_diff.keys()))
            if text_changed:
                changed_fields.append("text")

            common_changes_map[rid] = {
                "record_id": rid,
                "status": "changed",
                "geometry": geom_diff,
                "attributes": attr_diff,
                "style": style_diff,
                "text": {"before": text_b, "after": text_a} if text_changed else None,
                "changed_fields": changed_fields,
            }
        else:
            unchanged_ids.append(rid)

    all_changes: list[dict[str, Any]] = []
    all_changed_or_diff_ids = sorted(set(added_ids) | set(removed_ids) | set(changed_ids))

    for rid in all_changed_or_diff_ids[:limit]:
        if rid in common_changes_map:
            all_changes.append(common_changes_map[rid])
        elif rid in after_row_map:
            r_after = after_row_map[rid]
            geom_a = reader_after.geom[r_after]
            attrs_a = _extract_attributes(reader_after, r_after)
            style_a = _extract_style(reader_after, r_after)

            all_changes.append({
                "record_id": rid,
                "status": "added",
                "geometry": {
                    "before_wkb_sha256": None,
                    "after_wkb_sha256": sha(geom_a.wkb),
                },
                "attributes": {
                    col: {"before": None, "after": val}
                    for col, val in sorted(attrs_a.items())
                },
                "style": {
                    k: {"before": None, "after": val}
                    for k, val in sorted(style_a.items())
                },
                "changed_fields": ["attributes", "geometry", "style"],
                "text": {"before": None, "after": annotation_text(blob_after, sections_after, rid)}
                        if reader_after.shapeType == "POINT" else None,
            })
        elif rid in before_row_map:
            r_before = before_row_map[rid]
            geom_b = reader_before.geom[r_before]
            attrs_b = _extract_attributes(reader_before, r_before)
            style_b = _extract_style(reader_before, r_before)

            all_changes.append({
                "record_id": rid,
                "status": "removed",
                "geometry": {
                    "before_wkb_sha256": sha(geom_b.wkb),
                    "after_wkb_sha256": None,
                },
                "attributes": {
                    col: {"before": val, "after": None}
                    for col, val in sorted(attrs_b.items())
                },
                "style": {
                    k: {"before": val, "after": None}
                    for k, val in sorted(style_b.items())
                },
                "changed_fields": ["attributes", "geometry", "style"],
                "text": {"before": annotation_text(blob_before, sections_before, rid), "after": None}
                        if reader_before.shapeType == "POINT" else None,
            })

    total_changes = len(all_changed_or_diff_ids)
    def coordinate_metadata(blob):
        return {"coordinate_type": blob[91], "coordinate_unit": blob[92],
                "projection_index": blob[109], "ellipsoid_index": blob[110],
                "stored_map_scale": _safe_scalar(struct.unpack_from("<d", blob, 143)[0]),
                "header_record_count": struct.unpack_from("<I", blob, 260)[0]}
    metadata_changes = _diff_dicts(coordinate_metadata(blob_before), coordinate_metadata(blob_after))

    return {
        "scope": "PHYSICAL_IDS_ONLY",
        "native_reopen": "NOT_VERIFIED",
        "type": reader_before.shapeType,
        "before": {
            "path": str(before_path.resolve()),
            "sha256": sha(blob_before),
            "bytes": len(blob_before),
            "count": len(before_ids),
        },
        "after": {
            "path": str(after_path.resolve()),
            "sha256": sha(blob_after),
            "bytes": len(blob_after),
            "count": len(after_ids),
        },
        "counts": {
            "added": len(added_ids),
            "removed": len(removed_ids),
            "changed": len(changed_ids),
            "unchanged": len(unchanged_ids),
        },
        "added_ids": added_ids[:limit],
        "removed_ids": removed_ids[:limit],
        "changed_ids": changed_ids[:limit],
        "unchanged_ids": unchanged_ids[:limit],
        "ids_truncated": {key: len(ids) > limit for key, ids in
                          (("added", added_ids), ("removed", removed_ids),
                           ("changed", changed_ids), ("unchanged", unchanged_ids))},
        "metadata_changes": metadata_changes,
        "records_equal": total_changes == 0,
        "bytes_equal": blob_before == blob_after,
        "total": total_changes,
        "returned": len(all_changes),
        "limit": limit,
        "truncated": total_changes > len(all_changes),
        "changes": all_changes,
    }
