from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import Bridge
from .deletion import prepare_deletion
from .environment import capabilities
from .native_state import live_state
from .operations import find_operations, describe_operation
from .native_help import search_native_help
from .raster import RasterWorkspace, write_georeferenced_window
from .creation import prepare_creation
from .vector import prepare_vector_operation
from .candidates import extract_candidates, merge_candidate_tiles
from .features import read_features, prepare_file_operation
from .comparison import compare_native_files
from .native_backend import native_file_info, prepare_native_operation, reconcile_native_request
from .native_plot import prepare_native_plot
from .workspace import NativeWorkspaces
from .exchange import query_attributes,prepare_export,prepare_import
from .terrain import analyze_terrain,prepare_terrain
from .network import analyze_network,prepare_network
from .native_raster import prepare_native_raster,prepare_raster_warp
from .artifacts import reconcile_bundle
from .native_project import native_project_info,prepare_native_project,prepare_project_plot
from .cartography import projection_points,prepare_map_frame
from .derived import prepare_annotations,prepare_seams,prepare_attribute_report


def build_server(root: str | Path) -> FastMCP:
    bridge = Bridge(root)
    raster = RasterWorkspace(root)
    workspaces = NativeWorkspaces(bridge)
    server = FastMCP("MapGIS 6.7 本机原生文件工具")

    @server.tool()
    def mapgis_prepare_project_plot(file:str,expected_sha256:str,view:dict,request_id:str)->dict:
        """Render a portable saved MPJ's ordered vector and MSI background layers through native MapGIS into EPS, with an independent PNG preview. view={native_bounds:[xmin,ymin,xmax,ymax],paper_mm:[width,height],coordinate_frame:'native'}. Project dependencies must be siblings (prepare_native_project makes such a bundle). Freezes project/member hashes, checks actual drawn layer counts and EPS page/visible output, then prepares an atomic bundle for apply/get/cancel. No mouse or external editor mutation."""
        return prepare_project_plot(bridge,file,expected_sha256,view,request_id)

    @server.tool()
    def mapgis_projection_points(points:list[list[float]],source_crs:dict,target_crs:dict,allow_unknown_accuracy:bool=False)->dict:
        """Transform explicit XY pairs using PROJ with no ballpark operation. Require Earth CRS definitions and xy axes. Missing best grids fail; unknown accuracy across datums needs explicit opt-in. Returns actual operation/accuracy metadata, independently of native file CRS encoding limits."""
        return projection_points(points,source_crs,target_crs,allow_unknown_accuracy)

    @server.tool()
    def mapgis_prepare_map_frame(part:str,parameters:dict,template:dict,request_id:str)->dict:
        """Create frame border/grid WL or tick-label WT. parameters={geographic_bounds:[west,south,east,north],grid_step_degrees,subdivisions?:32,geographic_crs?:EPSG4326 explicit dict}. Densifies before projection, checks native CRS encoding, and writes actual GBK DMS annotations. template requires name, explicit target crs and map_scale. No national sheet-number convention inferred; apply/get/cancel use the ledger."""
        return prepare_map_frame(bridge,part,parameters,template,request_id)

    @server.tool()
    def mapgis_prepare_annotations(file:str,expected_sha256:str,field:str,template:dict,request_id:str)->dict:
        """Make a new native annotation WT from a saved attribute field. Points retain position, lines use half-length, polygons use an interior representative point (avoids holes). Keeps source attributes/physical mapping, explicit CRS/scale and native text style. apply/get/cancel publish a new branch; no source mutation."""
        return prepare_annotations(bridge,file,expected_sha256,field,template,request_id)

    @server.tool()
    def mapgis_prepare_seams(files:list[dict],parameters:dict,template:dict,request_id:str)->dict:
        """Join 2..100 saved native files by an explicit class field: files=[{file,expected_sha256}], parameters={field,grid_size?}. Optional positive precision grid snaps to a shared zero-origin lattice, then GEOS dissolves each class. Reports movement and source mappings; only the group field is retained in the derived attributes. Requires a common declared CRS and template name/scale; inputs are preserved."""
        return prepare_seams(bridge,files,parameters,template,request_id)

    @server.tool()
    def mapgis_prepare_attribute_report(file:str,expected_sha256:str,sql:str,request_id:str,limit:int=100000)->dict:
        """Persist a real read-only native attribute query/aggregate as JSON plus UTF-8 CSV. Same SQL limits and authorizer as query_attributes. Whole result set/member hashes publish atomically through apply_patch; truncation is explicit and source bytes stay unchanged."""
        return prepare_attribute_report(bridge,file,expected_sha256,sql,request_id,limit)

    @server.tool()
    def mapgis_native_project_info(file:str,expected_sha256:str)->dict:
        """Inspect a saved MPJ through the installed exact-build project library in an owned process. Reopens vector layers and reads actual native active counts; MSI backgrounds return native extents. No project edit/save call. Does not bind or change an external editor. Native paths must fit the verified CP936 interface."""
        return native_project_info(bridge.root,file,expected_sha256)

    @server.tool()
    def mapgis_prepare_native_project(layers:list[dict],parameters:dict,request_id:str)->dict:
        """Prepare a portable native MPJ with copied WT/WL/WP and MSI layers. layers=[{file,expected_sha256}], parameters={name:'map.mpj',coordinate_frame:'native'}. Uses mapprj60 Create/Append/Update/Save; relative references, no original rename. Reopens after relocation with the old candidate directory hidden and compares native counts/extents. apply publishes MPJ and dependencies atomically; sources stay unchanged. A common physical coordinate frame remains the caller's responsibility."""
        return prepare_native_project(bridge,layers,parameters,request_id)

    @server.tool()
    def mapgis_query_attributes(file:str,expected_sha256:str,sql:str,limit:int=1000)->dict:
        """Read-only SQLite over saved native attributes in table features. __record_id is physical identity; ID may repeat. SELECT/CTE/aggregates allowed, writes/ATTACH/PRAGMA/extensions denied; bounded query work and row limit. Source remains unchanged."""
        return query_attributes(file,expected_sha256,sql,limit)

    @server.tool()
    def mapgis_prepare_export(file:str,expected_sha256:str,driver:str,request_id:str)->dict:
        """Prepare a standard vector bundle: GeoJSON, GPKG or ESRI Shapefile. Full geometry/attribute readback; sidecar binds physical IDs, native symbols/text/styles and member hashes. GeoJSON uses the 2008 named-CRS extension for custom projected data. Shapefile truncation/collisions rejected. apply_patch atomically publishes all members; status verifies the complete set."""
        return prepare_export(bridge,file,expected_sha256,driver,request_id)

    @server.tool()
    def mapgis_prepare_import(file:str,expected_sha256:str,template:dict,request_id:str,layer:str|None=None)->dict:
        """Prepare NEW WT/WL/WP from GeoJSON/GPKG. template requires name, explicit crs and map_scale. Rejects invalid/mixed/3D geometry, duplicate identities, wrong CRS and ambiguous GPKG layers. A project-local, input-hashed sidecar restores native annotation/styles and attributes. apply/get/cancel use the existing ledger."""
        return prepare_import(bridge,file,expected_sha256,template,request_id,layer)

    @server.tool()
    def mapgis_analyze_terrain(file:str,expected_sha256:str,height_field:str,base_height:float|None=None)->dict:
        """Analyze saved WT elevations using SciPy Delaunay. Returns planar hull area, triangulated 3D area and exact piecewise-linear cut/fill against an optional horizontal base. XY and elevations must share a linear unit; no extrapolation outside the convex hull. Does not mutate the source."""
        return analyze_terrain(file,expected_sha256,height_field,base_height)

    @server.tool()
    def mapgis_prepare_terrain(file:str,expected_sha256:str,height_field:str,parameters:dict,template:dict,request_id:str)->dict:
        """Prepare terrain native vectors: parameters={operation:'tin'} for WP; {operation:'grid',bounds:[xmin,ymin,xmax,ymax],step} for WT; {operation:'contours',grid:{bounds,step},levels:[...]} for WL. Separate height_field names the WT elevation attribute. SciPy/ContourPy, hull masking, all requested levels checked and empty levels reported. Output template requires the explicit source CRS, map_scale and matching suffix. apply/get/cancel use the ledger."""
        if 'height_field' in parameters and parameters['height_field']!=height_field:
            from .core import BridgeError
            raise BridgeError('TERRAIN_HEIGHT_FIELD_CONFLICT')
        return prepare_terrain(bridge,file,expected_sha256,parameters|{'height_field':height_field},template,request_id)

    @server.tool()
    def mapgis_analyze_network(file:str,expected_sha256:str,parameters:dict)->dict:
        """NetworkX/GEOS over saved WL: operation=route, service_area or components. Explicit directed boolean, optional cost_field, source/target XY or cutoff; snap_tolerance and node_intersections are opt-in. Physical IDs and edge direction preserved; negative costs refused. Service area returns reachable network segments including partial edges, not a filled isochrone."""
        return analyze_network(file,expected_sha256,parameters)

    @server.tool()
    def mapgis_prepare_network(file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str)->dict:
        """Prepare native WL for route, reachable segments or connected components using analyze_network parameters. Keeps source edge IDs/costs/attributes and explicit source CRS. New branch only; apply/get/cancel use the existing ledger."""
        return prepare_network(bridge,file,expected_sha256,parameters,template,request_id)

    @server.tool()
    def mapgis_prepare_native_raster(operation:str,file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
        """Native TIFF/MSI conversion with no mouse: operation=to_msi or to_tiff, parameters={name?,gcps?:[{pixel:[col,row],world:[x,y]}],max_error?}. Verified byte gray/RGB without palette/nodata. to_msi writes native GCPs, closes and reopens in a fresh process, and proves exact pixels. GCP saving is not rectification. A hashed sidecar preserves CRS identifiers the native converter drops; to_tiff restores it only when bound to the exact MSI. apply publishes the complete file set."""
        return prepare_native_raster(bridge,operation,file,expected_sha256,parameters,request_id)

    @server.tool()
    def mapgis_prepare_raster_warp(file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
        """Rectify affine GCP scans/reproject rasters with GDAL, without a GUI. parameters={name,target_crs,bounds:[xmin,ymin,xmax,ymax],resolution:[dx,dy],resampling:'nearest'|'bilinear'|'cubic',max_gcp_error?}. Explicit target grid; 64MB warp memory, no full-image array. Original pixels remain in the source. New GeoTIFF is read back before preparation; apply/get/cancel use the ledger. Independent checkpoints determine real ground accuracy."""
        return prepare_raster_warp(bridge,file,expected_sha256,parameters,request_id)

    @server.tool()
    def mapgis_reconcile_artifact(request_id:str)->dict:
        """Reconcile a completed multi-file export/raster result after parent interruption. Validates frozen member hashes and source fingerprints, then prepares the same result without rerunning its algorithm or native call. Incomplete producers are not replayed. Use apply_patch afterward."""
        return reconcile_bundle(bridge,request_id)

    @server.tool()
    def mapgis_prepare_plot_export(file:str,expected_sha256:str,parameters:dict,request_id:str)->dict:
        """Export an actual native EPS to PDF and/or RGB TIFF without a desktop. parameters={formats:['pdf','tiff'],dpi:300}; dpi 72..600, at most 100M pixels. Ghostscript retains the physical page, TIFF is read back, and PDF is independently rendered and compared with the EPS. No georeferencing is inferred from a printed page. apply_patch publishes all members atomically; original EPS is preserved."""
        from .plot_export import prepare_plot_export
        return prepare_plot_export(bridge,file,expected_sha256,parameters,request_id)

    @server.tool()
    def mapgis_native_workspace(action: str, workspace_id: str, parameters: dict,
                                expected_revision: int | None = None, request_id: str | None = None) -> dict:
        """Own a persistent native buffer without mouse/UI. open: parameters={source,expected_sha256}; status: {}. edit: parameters={operation:'point.move'|'feature.delete',parameters:{updates:[{record_id,x,y}]}|{record_ids:[1,3]}}; save: {}; edit/save require expected_revision and stable request_id. close: {} refuses dirty work, {discard:true} explicitly discards back to last save. recover: {} restores completed checkpoints and reconciles an interrupted edit/save without replay. close/recover require expected_revision. Saved outputs are new deliverables; the source stays unchanged. Dirty checkpoints survive parent/native failure. This buffer belongs to the agent, independently of any external MapGIS editor and its unsaved memory. Requires the explicitly enabled exact-build x86 backend."""
        return workspaces.execute(action,workspace_id,parameters,expected_revision,request_id)

    @server.tool()
    def mapgis_reconcile_native_request(request_id: str) -> dict:
        """Reconcile RUNNING/FAILED native file or plot requests after interruption without repeating the edit. Verifies the recorded child PID/start/executable, durable completion receipt, source snapshots and actual candidate. File candidates get an independent comparison and fresh native reopen; plots get page/layer checks and independent rendering. Incomplete candidates remain FAILED. Then use apply_patch for PREPARED results."""
        return reconcile_native_request(bridge, request_id)

    @server.tool()
    def mapgis_prepare_native_plot(layers: list[dict], view: dict, request_id: str) -> dict:
        """Compose actual native EPS without desktop input. layers ordered bottom to top: [{file,expected_sha256}], 1..100 saved WT/WL/WP. view={native_bounds:[xmin,ymin,xmax,ymax],paper_mm:[width,height],coordinate_frame:'native'}; obtain raw native bounds with native_file_info, ensure all layers share the intended coordinate frame. Preserves aspect ratio, centers on the physical page, uses installed MapGIS symbols/fonts and verified winout60/mapdsp60. Independent Ghostscript preview rejects blank/truncated output. PREPARED candidates use existing apply_patch/get_request/cancel_request; originals stay unchanged. Geological content still needs source review."""
        return prepare_native_plot(bridge, layers, view, request_id)

    @server.tool()
    def mapgis_native_file_info(file: str, expected_sha256: str) -> dict:
        """Open a saved WT/WL/WP through BAS60 in a fresh isolated x86 process, without desktop input. Requires exact build, installed licence and explicit isolated legacy DEP compatibility opt-in. Cross-checks native active count against the independent Reader. Never reads live unsaved editor memory."""
        return native_file_info(bridge.root, file, expected_sha256)

    @server.tool()
    def mapgis_prepare_native_operation(operation: str, file: str, expected_sha256: str,
                                       parameters: dict, request_id: str) -> dict:
        """Prepare a new branch through actual BAS60 calls without mouse/UI. operation=resave with {}; point.move with {updates:[{record_id:1,x:12.5,y:13.75}]}; or feature.delete with {record_ids:[1,3]} for WT/WL/WP. WP deletion checks shared arc ownership, holes and active regions before and after native save/reopen. Requires an owned work copy and explicit legacy x86 compatibility opt-in. Verifies saved geometry/attributes/style and opens it in another native process before PREPARED. apply_patch/get_request/cancel_request use the existing ledger. RUNNING or FAILED is never blindly replayed."""
        return prepare_native_operation(bridge, operation, file, expected_sha256, parameters, request_id)

    @server.tool()
    def mapgis_search_native_help(module: str, query: str = '', topic_id: str | None = None, limit: int = 10) -> dict:
        """Search the recipient's indexed HLP/CHM by module/words; pass returned topic_id to read the full topic. Source hash checked every time. Instructions are documentation, not permission or execution proof; native UI stays with host Sky."""
        return search_native_help(bridge.root, module, query, topic_id, limit)

    @server.tool()
    def mapgis_find_operations(query: str = "", module: str | None = None) -> dict:
        """Find local operations by task words/module. DISCOVERED is not executable or tested; describe before choosing an execution path."""
        return find_operations(bridge.root, query, module)

    @server.tool()
    async def mapgis_describe_operation(operation_id: str) -> dict:
        """Read parameters, preconditions, installed executable, native help and acceptance checks. Native UI requires the host's Sky and fresh observations."""
        result = describe_operation(bridge.root, operation_id)
        if result["executable_by_mcp"]:
            tools = {tool.name: tool for tool in await server.list_tools()}
            result["input_schema"] = tools[result["execution"]["tool"]].inputSchema
            result["execution_status"] = "MCP_TOOL_AVAILABLE"
        else:
            result["execution_status"] = "HOST_OBSERVATION_REQUIRED"
        return result

    @server.tool()
    def mapgis_register_scan(source: str, request_id: str) -> dict:
        """Register original raster bytes/dimensions without full-image decode. Source/preview/tile coordinates remain distinct."""
        return raster.register_scan(source, request_id)

    @server.tool()
    def mapgis_scan_window(scan_id: str, window: list[int], request_id: str, max_dimension: int = 2048) -> dict:
        """Render a bounded source-pixel window using libvips. Returns exact preview-to-source mapping; never disables Pillow large-image protections."""
        return raster.render_window(scan_id, window, request_id, max_dimension)

    @server.tool()
    def mapgis_prepare_georeference(scan_id: str, crs: dict, gcps: list[dict], request_id: str,
                                   checkpoints: list[dict] | None = None, max_error: float = 1.0,
                                   pixel_frame: str = "source", pixel_to_source: list[float] | None = None) -> dict:
        """Fit an affine with explicit datum/axis order and independent checkpoints. Small fitting residual alone cannot pass acceptance. Errors use declared CRS units."""
        return raster.prepare_georeference(scan_id, crs, gcps, request_id, checkpoints, max_error, pixel_frame, pixel_to_source)

    @server.tool()
    def mapgis_write_georeferenced_window(scan_id: str, window_id: str, georeference_id: str, request_id: str) -> dict:
        """Write bounded RGB GeoTIFF plus a MapGIS .gcp file from an accepted affine. Native TIFF-to-MSI drops the GeoTIFF GCP metadata: explicitly import .gcp, save and reopen, then check overlay. Local grids stay local; source pixels and fingerprints are preserved."""
        return write_georeferenced_window(raster,scan_id,window_id,georeference_id,request_id)

    @server.tool()
    def mapgis_extract_candidates(scan_id: str, window_id: str, config: dict, request_id: str) -> dict:
        """Extract reviewable line/area/symbol candidates from a fingerprinted bounded window. GeoScan centerlines/GDAL cell-edge polygons preserve source offsets; map coordinates require an accepted georeference. Explicit roles/RGB mask/palette, no inferred geological age, all candidates UNREVIEWED. Returns GeoJSON path and QA."""
        return extract_candidates(raster,scan_id,window_id,config,request_id)

    @server.tool()
    def mapgis_merge_candidate_tiles(scan_id: str, tiles: list[dict], request_id: str,
                                      georeference_id: str | None = None, simplify_px: float = 0) -> dict:
        """Merge full-resolution area candidates using explicit disjoint core rectangles in original pixels. tiles: [{request_id, core:[left,top,width,height]}]. Compare overlapping windows, clip duplicate halos, union each explicit color class, validate matched coverage, optionally simplify jointly and georeference once. Requires same source and palette; all geology remains UNREVIEWED. Returns immutable GeoJSON path and measured seam disagreement."""
        return merge_candidate_tiles(raster,scan_id,tiles,request_id,georeference_id,simplify_px)

    @server.tool()
    def mapgis_prepare_creation(features: dict, template: dict, request_id: str) -> dict:
        """Prepare NEW WT/WL/WP from map-coordinate GeoJSON with unique feature IDs, explicit CRS/scale and native style. Shared arcs/holes use pinned mapgis2shp. Apply/get/cancel use the existing request ledger; native UI reopen is a separate gate."""
        return prepare_creation(bridge, features, template, request_id)

    @server.tool()
    def mapgis_prepare_vector_operation(operation_id: str, features: dict, template: dict,
                                        parameters: dict, request_id: str) -> dict:
        """Prepare a NEW native branch using GEOS/PROJ: topology.polygonize/node/snap_grid, correction.affine, overlay.clip/difference, geometry.buffer/simplify/dissolve, projection.transform, style.classify. Explicit CRS/units and stable IDs; preserves open nonboundary features; reports QA. Existing request apply/get/cancel commits the result."""
        return prepare_vector_operation(bridge,operation_id,features,template,parameters,request_id)

    @server.tool()
    def mapgis_get_capabilities() -> dict:
        """Report local scope, installation, processes and exact-build live-state prerequisites; discovery is not a live acceptance test."""
        return capabilities(bridge.root)

    @server.tool()
    def mapgis_get_live_state(pid: int) -> dict:
        """Read exact-build 6.7 project/layer paths, selection pools and unsaved flags. Read-only. Active MDI identity requires independent Sky observation; never guesses it from view count."""
        return live_state(bridge.root, pid)

    @server.tool()
    def mapgis_inspect_file(file: str, offset: int = 0, limit: int = 10) -> dict:
        """Read native WT/WL/WP geometry, attributes and style. record_id is physical 1-based, not attribute ID; no GUI selection read."""
        return bridge.inspect(file, offset, limit)

    @server.tool()
    def mapgis_read_features(file: str, expected_sha256: str, record_ids: list[int] | None = None,
                             offset: int = 0, limit: int = 100) -> dict:
        """Read saved WT/WL/WP as paged GeoJSON with separate native styles/text. IDs are physical slots, attribute ID may repeat. Require the current file fingerprint; coordinates need explicit CRS/unit confirmation. For whole-file processing use prepare_file_operation without sending every vertex through the model."""
        return read_features(file, expected_sha256, record_ids, offset, limit)

    @server.tool()
    def mapgis_prepare_file_operation(operation_id: str, file: str, expected_sha256: str,
                                      template: dict, parameters: dict, request_id: str,
                                      record_ids: list[int] | None = None) -> dict:
        """Run existing GEOS/PROJ operations directly on a saved native file or explicit physical IDs. Same operations as prepare_vector_operation. Declare CRS/units and new WT/WL/WP name in template. Optional role_by_record maps string physical IDs to boundary/fault/contour/annotation/protected for polygonize. Produces a new derived branch with provenance and mapping; auxiliary native sections are not copied. Apply/get/cancel use the existing ledger; does not edit live unsaved memory."""
        return prepare_file_operation(bridge, operation_id, file, expected_sha256,
                                      template, parameters, request_id, record_ids)

    @server.tool()
    def mapgis_make_work_copy(source: str, request_id: str) -> dict:
        """Copy a native vector into a new owned workspace, preserving original bytes. Repeated ID reconciles the same copy."""
        return bridge.copy(source, request_id)

    @server.tool()
    def mapgis_prepare_patch(file: str, expected_sha256: str, record_ids: list[int], request_id: str,
                             attributes: dict | None = None, style: dict | None = None) -> dict:
        """Preview fixed-field edits to explicit records on an owned copy. Preserves geometry, annotation auxiliary sections and every untargeted byte. No source write."""
        return bridge.prepare(file, expected_sha256, record_ids, request_id, attributes, style)

    @server.tool()
    def mapgis_prepare_delete(file: str, expected_sha256: str, record_ids: list[int], request_id: str) -> dict:
        """Prepare deletion of explicit active physical IDs in an owned WT/WL copy. Attribute ID is not identity. Keeps other bytes, native pools/styles and slot numbering; produces a NEW branch through apply_patch. No live unsaved-memory edits. WP requires native topology editing. Same-layout recovery can restore this request's flags; native reopen remains a separate check."""
        return prepare_deletion(bridge, file, expected_sha256, record_ids, request_id)

    @server.tool()
    def mapgis_apply_patch(request_id: str) -> dict:
        """Apply a prepared PATCH, CREATE or DELETE to a NEW output branch after source hash recheck. Idempotent; does not overwrite originals or live documents. Reconcile get_request after interruption before retrying."""
        return bridge.apply(request_id)

    @server.tool()
    def mapgis_get_request(request_id: str) -> dict:
        """Inspect the persisted request and output hash. Reconcile this state after interruption before retrying."""
        return bridge.status(request_id)

    @server.tool()
    def mapgis_compare_files(before: str, after: str, limit: int = 100) -> dict:
        """Read-only saved WT/WL/WP comparison by physical slots: geometry, attributes, graphic parameters, annotation text and coordinate header. Returns complete counts and bounded details; limit=0 gives counts. Renumbering is not semantic identity. Does not certify native UI or reopen."""
        return compare_native_files(before, after, limit)

    @server.tool()
    def mapgis_verify_request_output(request_id: str, file: str) -> dict:
        """Compare a saved file with an applied request's frozen candidate. Accepts native bookkeeping changes when records/text/styles/coordinate header match. Reports matches_candidate_records; never rewrites request status or labels an unobserved native reopen as verified. Use after native Save with independent live project/dirty checks."""
        return bridge.verify_output(request_id, file)

    @server.tool()
    def mapgis_cancel_request(request_id: str) -> dict:
        """Cancel an unapplied preview. Refuses to cancel an applied or uncertain output."""
        return bridge.cancel(request_id)

    @server.tool()
    def mapgis_prepare_recovery(applied_request: str, later_file: str, expected_sha256: str, request_id: str) -> dict:
        """Prepare a NEW recovery branch reverting only this request's fields. Uses compatible spans or a guarded mapping of unchanged physical records after native layout changes. Preserves later independent edits; same-field, deleted, renumbered or changed-geometry conflicts fail. Not universal native Undo."""
        return bridge.recover(applied_request, later_file, expected_sha256, request_id)

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    build_server(args.root).run(transport="stdio")


if __name__ == "__main__":
    main()
