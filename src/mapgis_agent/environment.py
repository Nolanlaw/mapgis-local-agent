"""Read-only local discovery. Presence is not a licence or SDK readiness test."""
from __future__ import annotations

import platform
from importlib import metadata
from pathlib import Path
import struct

import psutil


MODULES = {
    "editor":("mdiedit6x.exe","Mpedit60.hlp"),
    "raster":("msiProc60.exe","MsiProc.chm"),
    "projection":("w60_proj.exe","W60_proj.hlp"),
    "correction":("w60_chg.exe","W60_chg.hlp"),
    "attributes":("W60_Att.exe","W60_att.hlp"),
    "symbols":("w60_bb.exe","Gisbb.hlp"),
    "library":("W60_Dbs.exe","W60_dbs.hlp"),
    "analysis":("W60_Anly.exe","W60_Anly.hlp"),
    "terrain":("W60_DTM.exe","W60_dtm.hlp"),
    "terrain_3d":("DTM6x.exe","DTM.chm"),
    "network":("Netanaly.exe","Netanaly.chm"),
    "conversion":("W60_Conv.exe","w60_conv.hlp"),
    "output":("mapout60.exe","Mapout60.hlp"),
    "section":("section.exe","section.CHM"),
    "image_library":("MsiDb60.exe","Msimgdb.hlp"),
}


def capabilities(root: Path) -> dict:
    config_path = root / "config.local.json"
    import json
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    install = Path(config["mapgis_installation"]) if config.get("mapgis_installation") else None
    binaries = []
    if install:
        for name in ("Face67.exe",*(pair[0] for pair in MODULES.values())):
            path = install / "program" / name
            if path.exists():
                data = path.read_bytes()
                pe = struct.unpack_from("<I", data, 60)[0]
                binaries.append({"path": str(path), "bytes": len(data),
                                 "machine": hex(struct.unpack_from("<H", data, pe + 4)[0])})
    processes = []
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            if (process.info["name"] or "").lower() in {"face67.exe","dogserver67.exe",*(pair[0].lower() for pair in MODULES.values())}:
                processes.append(process.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    modules={name:{"executable":pair[0],"help_file":pair[1],
                  "installed":bool(install and (install/'program'/pair[0]).is_file()),
                  "help_present":bool(install and (install/'program'/pair[1]).is_file()),
                  "licence":"UNCHECKED","native_execution":"host Sky; inspect the actual module before input"}
             for name,pair in MODULES.items()}
    dependencies={}
    for name in ("mapgis2shp","mcp","psutil","pyvips","pyvips-binary","opencv-python-headless","rasterio","shapely","pyproj"):
        try:
            dependencies[name]=metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name]=None
    return {"platform": platform.platform(), "installation": str(install) if install else None,
            "binaries": binaries, "processes": processes,
            "modules":modules,"dependencies":dependencies,
            "implemented": ["native_file_inspection", "copy_without_overwriting_original", "explicit_record_attribute_style_preview",
                         "new_branch_apply", "request_reconciliation", "guarded_structural_recovery",
                         "exact_build_readonly_project_selection_dirty_state",
                         "operation_discovery_with_validation_scope","source_bound_local_help_search","bounded_scan_windows",
                         "explicit_affine_and_independent_checks","geotiff_and_native_gcp_export",
                         "reviewable_scan_candidates","new_native_vectors_and_gbk_annotation",
                         "geos_vector_operations_and_proj_transforms",
                         "paged_native_geometry_with_physical_ids", "saved_native_file_vector_processing",
                         "point_line_delete_with_native_active_count", "record_text_style_and_metadata_comparison",
                         "persisted_request_saved_output_verification", "typed_mcp_schemas_and_cli_call",
                         "isolated_native_x86_read_resave_point_move", "native_eps_composition_without_desktop_input"],
            "native_profile": "mapgis-6.7-051118-readonly-v1; verified module hashes and loaded-code guards required",
            "native_execution": "Saved WT/WL/WP can use the exact-build isolated x86 backend for native read/resave and point.move after explicit legacy DEP compatibility opt-in. Other desktop operations use host Sky. No Python UI input.",
            "native_backend": {"transport": "owned x86 child process; no listening port", "operations": ["inspect", "resave", "point.move", "feature.delete", "plot"],
                               "legacy_dep_compatibility_enabled": config.get("native_backend_legacy_dep_compatibility") is True,
                               "system_dep_changed": False, "live_editor_mutation": False},
            "native_output": "Prepare-NativeOutput.ps1 stages explicit sibling dependencies through a short ASCII junction; native EPS/TIFF require live path binding and file validation.",
            "requires_native_validation": ["other_builds", "unobserved_modules_or_edit_types", "new_machine_symbols_and_licence",
                                           "new_project_native_output", "general_native_save_reopen"],
            "licence": "A running helper is not proof that all modules are licensed.",
            "sdk": "Exact-build adapter from native exports and local type information; no complete developer SDK is included. Live unsaved-editor mutation is not provided by the isolated backend.",
            "transport": "stdio; no listening port or arbitrary code execution tool",
            "source": "Pinned mapgis2shp 2.6.0, GeoScan and Stratigraphic Amenity; GEOS/PROJ/GDAL/libvips/OpenCV; new-file region/text compatibility supplements and byte-preserving edits",
            "coverage_limit": "Discovery and installed modules do not establish full MapGIS automation. Each native workflow, licence and output needs actual acceptance evidence.",
            "native_save_limit": "File tools create a separate branch; they do not write into live unsaved MapGIS memory."}
