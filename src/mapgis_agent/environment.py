"""Read-only local discovery. Presence is not a licence or SDK readiness test."""
from __future__ import annotations

import platform
from pathlib import Path
import struct

import psutil


def capabilities(root: Path) -> dict:
    config_path = root / "config.local.json"
    import json
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    install = Path(config["mapgis_installation"]) if config.get("mapgis_installation") else None
    binaries = []
    if install:
        for name in ("Face67.exe", "mdiedit6x.exe", "W60_Conv.exe", "section.exe"):
            path = install / "program" / name
            if path.exists():
                data = path.read_bytes()
                pe = struct.unpack_from("<I", data, 60)[0]
                binaries.append({"path": str(path), "bytes": len(data),
                                 "machine": hex(struct.unpack_from("<H", data, pe + 4)[0])})
    processes = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            if (process.info["name"] or "").lower() in ("mdiedit6x.exe", "face67.exe", "dogserver67.exe"):
                processes.append(process.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"platform": platform.platform(), "installation": str(install) if install else None,
            "binaries": binaries, "processes": processes,
            "implemented": ["native_file_inspection", "copy_without_overwriting_original", "explicit_record_attribute_style_preview",
                         "new_branch_apply", "request_reconciliation", "guarded_structural_recovery",
                         "exact_build_readonly_project_selection_dirty_state"],
            "native_profile": "mapgis-6.7-051118-readonly-v1; verified module hashes and loaded-code guards required",
            "native_execution": "Host-provided Sky performs native edits, save/reopen and output. Python does not send UI commands.",
            "native_output": "Prepare-NativeOutput.ps1 stages explicit sibling dependencies through a short ASCII junction; native EPS/TIFF require live path binding and file validation.",
            "requires_native_validation": ["other_builds", "unobserved_modules_or_edit_types", "new_machine_symbols_and_licence",
                                           "new_project_native_output", "general_native_save_reopen"],
            "licence": "A running helper is not proof that all modules are licensed.",
            "sdk": "No matching developer headers or supported current-document API found in inspected installation.",
            "transport": "stdio; no listening port or arbitrary code execution tool",
            "source": "Pinned mapgis2shp 2.6.0 plus byte-preserving edits and exact-build read-only Win32 state",
            "native_save_limit": "File tools create a separate branch; they do not write into live unsaved MapGIS memory."}
