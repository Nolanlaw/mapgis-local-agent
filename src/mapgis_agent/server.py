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


def build_server(root: str | Path) -> FastMCP:
    bridge = Bridge(root)
    raster = RasterWorkspace(root)
    server = FastMCP("MapGIS 6.7 本机原生文件工具")

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
        """Prepare a new branch through actual BAS60 calls without mouse/UI. operation=resave with {}; point.move with {updates:[{record_id:1,x:12.5,y:13.75}]}; or feature.delete with {record_ids:[1,3]} for WT/WL. Requires an owned work copy and explicit legacy x86 compatibility opt-in. Verifies saved geometry/attributes/style and opens it in another native process before PREPARED. apply_patch/get_request/cancel_request use the existing ledger. RUNNING or FAILED is never blindly replayed. Other native APIs are not exposed."""
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
        """Prepare a NEW native branch using GEOS/PROJ: topology.polygonize, overlay.clip/difference, geometry.buffer/simplify/dissolve, projection.transform, style.classify. Explicit CRS/units and stable IDs; preserves open nonboundary features; reports QA. Existing request apply/get/cancel commits the result."""
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
