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
    # Symbols are edited by the editor's system-library commands. w60_bb is
    # the report editor, not a symbol-library executable.
    "symbols":("mdiedit6x.exe","Mpedit60.hlp"),
    "reports":("w60_bb.exe","Gisbb.hlp"),
    "library":("W60_Dbs.exe","W60_dbs.hlp"),
    "analysis":("W60_Anly.exe","W60_Anly.hlp"),
    "terrain":("W60_DTM.exe","W60_dtm.hlp"),
    "terrain_3d":("DTM6x.exe","DTM.chm"),
    "network":("Netanaly.exe","Netanaly.chm"),
    "conversion":("W60_Conv.exe","w60_conv.hlp"),
    "output":("mapout60.exe","Mapout60.hlp"),
    "section":("section.exe","section.CHM"),
    "image_library":("MsiDb60.exe","Msimgdb.hlp"),
    "attributes_legacy":("ATT.exe","Gis_att.hlp"),
    "clip":("w60_clip.exe","Gis_clip.hlp"),
    "network_legacy":("n60_edit.exe","Gis_net.hlp"),
    "image_view":("MapSee6x.exe","Msifly.hlp"),
    "database_setup":("Sql_Init.exe","Sql_init.hlp"),
    "survey":("MapSuvL67.exe","SuvHelp.chm"),
    "dem_library":("W60_DemDb.exe","DEMDBS.chm"),
    "terrain_flight":("DTM3DFly.exe","FLY.chm"),
    "multiscale":("CreateMSD.exe","MsdHlp.chm"),
    "utilities":("Face67.exe","Allone.hlp"),
}

# Implemented subsets, not a claim that an installed module is fully automated.
BACKEND_MODULES = {
    'editor':'native persistent WT/WL/WP buffers, point/line/shared-arc/shared-node editing, deletion, MPJ layer/state/display-limit/layout-environment editing and durable multi-layer project sessions',
    'raster':'native integer/float multiband tiled MSI, finite NoData, pyramid/GCP persistence; GDAL affine/polynomial2/polynomial3/TPS correction with explicit masks/checkpoints; SciPy/NumPy filters, band arithmetic, stretch, threshold/morphology, seeded kmeans and explicit RGB class colors; original proprietary palette/NaN profiles remain outside the supported native import',
    'projection':'PROJ points/vectors, densified frame/grid/GBK labels',
    'correction':'explicit affine vector transform/grid snapping; GDAL polynomial2/polynomial3/TPS raster GCP correction with separate fit/inverse/withheld checks',
    'attributes':'bounded SQL, per-record values/calculations and schema edits; guarded XLSX left joins; spatial left joins/statistics retaining native geometry/styles; CSV/JSON reports',
    'symbols':'stable-ID symbol/pattern polyline-polygon records and simple solid/dashed line records in copied libraries; isolated native plot binding',
    'library':'class-based vector seam merging; original DBS management uses the authorized private-process route',
    'multiscale':'native MSD6.0 MSI image collections: create, append, positional deletion, portable save/reopen and envelope queries; not all scale switching or other database types',
    'analysis':'GEOS topology/editing/overlay, paged spatial queries and explicit-ownership joins/grouped summaries; native non-attribute bytes retained',
    'terrain':'native IDW/GRD; CDT2.0.1 constrained topology plus real MapGIS TIN storage/readback, dense-profile node/constraint/edge edits and saved-face sections; six GDAL derivatives; ContourPy saved-grid contours/bands and unit-aware area/cut-fill',
    'network':'NetworkX route/components/reachable partial edges',
    'conversion':'GeoJSON/GPKG/SHP multi-file import/export, explicit multipart/reprojection and bound native sidecars',
    'output':'native vector/MSI project EPS; lossless embedded-image EPS/PDF encoding, TIFF/JPEG and independent pixel checks',
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
                  "licence":"UNCHECKED","backend_scope":BACKEND_MODULES.get(name),
                  "native_execution":"headless subset listed in backend_scope; see per-operation scope" if name in BACKEND_MODULES else "NOT_CONNECTED; installed executable is discovery only"}
             for name,pair in MODULES.items()}
    from .private_control import _profile
    private_profiles=_profile()['modules']
    private_enabled=config.get('native_general_private_ui') is True
    for name,record in modules.items():
        route=name if name in private_profiles else next((key for key,value in private_profiles.items()
            if value['executable'].casefold()==record['executable'].casefold()),None)
        if route:
            record['private_process_route']={'tool':'mapgis_private_control','module':route,
                'enabled':private_enabled,'requires_observation':True}
            if private_enabled:
                record['native_execution']='Dedicated SDK/file tools plus original menus/controls in a newly owned private process; see LOCAL67-ROUTES.md for runtime evidence and limits.'
    if 'terrain_3d' in modules:
        modules['terrain_3d']['installation_note']='DTM6x.exe is an archive extractor; its _dtmsfx.exe payload is absent in this installation. Original DTM3DFly.exe and native scene/flight SDK routes are available separately.'
    dependencies={}
    for name in ("mapgis2shp","mcp","psutil","pyvips","pyvips-binary","opencv-python-headless","rasterio","shapely","pyproj","scipy","contourpy","networkx"):
        try:
            dependencies[name]=metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name]=None
    return {"platform": platform.platform(), "installation": str(install) if install else None,
            "binaries": binaries, "processes": processes,
            "modules":modules,"dependencies":dependencies,
            "private_process_calling":{"enabled":private_enabled,'tool':'mapgis_private_control',
                'modules':private_profiles,'attach_existing_process':False,'desktop_input_used':False,
                'scope':'Installed6.7 non-server modules only. Observe original controls; bind task copies, execute, save and read back. DTM3DFly is an installed submodule, not an additional product.'},
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
                         "isolated_native_x86_read_resave_point_move", "native_eps_composition_without_desktop_input",
                         "native_wp_shared_arc_hole_deletion", "persistent_native_dirty_save_discard_recovery",
                         "atomic_multifile_outputs_and_reconciliation", "native_msi_gcp_and_exact_pixels",
                         "gdal_affine_raster_warp", "portable_native_mpj_and_raster_eps",
                         "scipy_terrain_and_contourpy", "networkx_routes_and_service_segments",
                         "bounded_attribute_sql_and_reports", "vector_exchange_with_bound_sidecars",
                         "densified_projected_frames", "annotation_placement_and_class_seams",
                         "physical_page_pdf_and_rgb_tiff", "native_line_vertex_replacement_in_map_units",
                         "geos_line_polygon_editing", "native_attribute_schema_preserving_geometry",
                         "per_record_attribute_updates", "per_record_graphic_updates", "native_circle_arc_radius_bounds",
                         "bounded_cubic_native_vertex_smoothing", "native_endpoint_resize_and_explicit_boundary_extension",
                         "copied_system_library_authoring_and_native_binding", "planar_area_weighted_spatial_summary",
                         "lossless_raster_pixel_window_crop", "mixed_mpj_window_clip_workflow",
                         "distinct_coincident_native_regions", "native_symbol_pen_width", "native_annotation_symbol_style_and_bounds", "native_annotation_text_and_bounds", "native_selection_append_preserving_target_identity", "dirty_workspace_selection_append_and_native_attribute_origin_correction", "cartesian_control_fits_and_full_affine_native_chain", "aligned_raster_mosaic_with_independent_coverage", "polygon_hole_aoi_mask_and_explicit_byte_rgb_display", "domestic_standard_sheet_index_and_native_scaled_line_bounds"],
            "native_profile": "mapgis-6.7-051118-readonly-v1; verified module hashes and loaded-code guards required",
            "native_execution": "Owned x86 native processes handle WT/WL/WP buffers, native WP deletion, MSI, MPJ and EPS. Open geospatial libraries implement the other listed subsets. No mouse input; unconnected modules remain explicit.",
            "native_backend": {"transport": "owned x86 child process; no listening port", "operations": ["inspect", "resave", "point.move", "point.radius", "point.style", "point.text", "line.replace", "line.smooth", "line.resize", "line.extend_to", "arc.replace", "arc.smooth", "node.move", "feature.delete", "plot", "workspace", "image.to_msi", "image.to_tiff", "project.create", "project.inspect", "project.edit", "project.plot", "com.subset", "com.append"],
                               "legacy_dep_compatibility_enabled": config.get("native_backend_legacy_dep_compatibility") is True,
                               "system_dep_changed": False, "live_editor_mutation": False},
            "native_output": "Native vector/project EPS on a private desktop, independently rendered; PDF and RGB TIFF derive from that EPS. Persistent WP buffers use process-owned short junctions to avoid the old DLL's long-path corruption.",
            "requires_native_validation": ["other_builds", "unobserved_modules_or_edit_types", "new_machine_symbols_and_licence",
                                           "new_project_native_output", "general_native_save_reopen"],
            "licence": "A running helper is not proof that all modules are licensed.",
            "sdk": "Exact-build adapter from native exports and local type information; no complete developer SDK is included. Live unsaved-editor mutation is not provided by the isolated backend.",
            "transport": "stdio; no listening port or arbitrary code execution tool",
            "source": "Pinned mapgis2shp 2.6.0, GeoScan and Stratigraphic Amenity; GEOS/PROJ/GDAL/libvips/OpenCV; new-file region/text compatibility supplements and byte-preserving edits",
            "coverage_limit": "Discovery and installed modules do not establish full MapGIS automation. Each native workflow, licence and output needs actual acceptance evidence.",
            "not_connected": (["external_user_editor_attachment", "server_dependent_features", "unknown_binary_builds"] if private_enabled else
                              ["external_editor_unsaved_memory_mutation", "native_font_color_and_complex_curve_library_authoring",
                              "proprietary_map_and_image_databases", "full_3d_terrain_and_geological_section_modules",
                              "arbitrary_mapgis_menu_or_unobserved_binary_build"]),
            "native_save_limit": "File tools create a separate branch; they do not write into live unsaved MapGIS memory."}
