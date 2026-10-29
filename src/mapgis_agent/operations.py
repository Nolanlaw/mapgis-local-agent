"""Versioned operation discovery. A native menu entry is not an executable API."""
from __future__ import annotations

import json
from pathlib import Path


def _catalog() -> list[dict]:
    return json.loads(Path(__file__).with_name("operations.json").read_text(encoding="utf-8"))


def _workflow(relative: str) -> dict:
    name = Path(relative).name
    # Wheels carry the same canonical JSON via Hatch's force-include rule.
    folder = Path(__file__).with_name("workflows")
    if not folder.is_dir():
        folder = Path(__file__).resolve().parents[2] / "workflows"
    return json.loads((folder / name).read_text(encoding="utf-8"))


def describe_operation(root: str | Path, operation_id: str) -> dict:
    item = next((item for item in _catalog() if item["id"] == operation_id), None)
    if item is None:
        raise ValueError(f"UNKNOWN_OPERATION: {operation_id}")
    config_path = Path(root) / "config.local.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    program = Path(config["mapgis_installation"]) / "program" if config.get("mapgis_installation") else None
    native = item["execution"]["kind"] == "host_sky"
    exe = program / item["executable"] if native and program else None
    availability = ("INSTALLED_LICENCE_UNCHECKED" if exe and exe.is_file() else
                    "NOT_INSTALLED" if program else "NOT_CONFIGURED") if native else "AVAILABLE"
    if operation_id.startswith("native.") or item["execution"].get("backend")=="mapgis_native":
        availability = ("NOT_CONFIGURED" if not program else
                        "LEGACY_HOST_DISABLED" if config.get("native_backend_legacy_dep_compatibility") is not True else
                        "EXACT_BUILD_AND_LICENCE_CHECKED_PER_CALL")
    result = {**item, "availability": availability,
            "executable_path": str(exe) if exe and exe.is_file() else None,
            "executable_by_mcp": not native,
            "coverage_scope": item.get("validation_scope",item.get("scope","See output_checks and parameters")),
            "version": "6.7; live editor requires exact supported build",
            "licence": "Native module access must be checked in the actual session" if native else None}
    if item.get("workflow"):
        result["workflow_knowledge"] = _workflow(item["workflow"])
    return result


def find_operations(root: str | Path, query: str = "", module: str | None = None) -> dict:
    terms = query.casefold().split()
    result = []
    for item in _catalog():
        if module and item["module"] != module:
            continue
        haystack = json.dumps(item, ensure_ascii=False).casefold()
        if not all(term in haystack for term in terms):
            continue
        detail = describe_operation(root, item["id"])
        result.append({key: detail[key] for key in
                       ("id", "name", "module", "availability", "validation", "executable_by_mcp")})
    return {"operations": result, "count": len(result),
            "status_note": "DISCOVERED describes a task and its native help; it does not claim execution or licence availability."}
