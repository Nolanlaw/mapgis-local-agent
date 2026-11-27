"""MCP presentation and validated routing for the three supported surfaces."""
from __future__ import annotations

import asyncio
from functools import wraps
import os
from pathlib import Path
from typing import Annotated, Literal

from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .core import Bridge, BridgeError, json_text, sha
from .errors import classify_error
from .jobs import JobContext, JobManager
from .native_help import search_native_help
from .operations import add_operation_schema, describe_operation, find_operations, operation_defaults


SURFACES = ('legacy', 'compact', 'hybrid')
_UNANNOTATED_READS = {
    'spatial_query', 'polygon_overlay', 'system_library_info', 'projection_points',
    'native_project_info', 'query_attributes', 'vector_source_info', 'analyze_terrain',
    'native_grid_info', 'table_info', 'analyze_network', 'raster_source_info', 'native_file_info',
    'search_native_help', 'find_operations', 'describe_operation', 'get_capabilities',
    'get_live_state', 'inspect_file', 'read_features', 'describe_sdk', 'read_wp_arcs',
    'get_request', 'compare_files', 'verify_request_output',
}
_LIFECYCLE_TOOLS = {'native_workspace', 'project_workspace', 'private_control'}
_DIRECT_TOOLS = {
    'mapgis_read_features': 'Page saved WT/WL/WP geometry, attributes and styles with an input SHA-256. Physical IDs are required for selection. Keep unknown CRS explicit.',
    'mapgis_make_work_copy': 'Make a project-owned WT/WL/WP copy using a stable request ID. Original bytes remain unchanged.',
    'mapgis_prepare_patch': 'Preview attribute or style changes on explicit physical IDs in an owned copy. Requires a current SHA-256. Leaves geometry, source and live buffers intact.',
    'mapgis_prepare_file_operation': 'Process saved WT/WL/WP using the chosen operation, explicit coordinate frame and input hash. Stages a new artifact without editing original or live data.',
    'mapgis_prepare_native_project': 'Stage an editable MPJ with copied dependencies. Bind each layer SHA-256 and the native coordinate frame. Requires the configured native runtime.',
    'mapgis_prepare_project_plot': 'Stage a native EPS and preview from a saved MPJ. Supply native bounds, paper dimensions and the MPJ hash. This call does not publish output.',
}


def select_surface(surface: str | None) -> str:
    selected = os.environ.get('MAPGIS_MCP_SURFACE', 'legacy') if surface is None else surface
    if selected not in SURFACES:
        raise ValueError('INVALID_SURFACE: choose legacy, compact or hybrid')
    return selected


def _annotations(*, read: bool = False, destructive: bool = False, idempotent: bool = True) -> ToolAnnotations:
    return ToolAnnotations(readOnlyHint=read, destructiveHint=destructive, idempotentHint=idempotent,
                           openWorldHint=False)


_READ = _annotations(read=True)
_PREPARE = _annotations()
_LIFECYCLE = _annotations(destructive=True, idempotent=False)
_CANCEL = _annotations(destructive=True)


def complete_annotations(legacy: FastMCP) -> None:
    for tool in legacy._tool_manager.list_tools():
        name = tool.name.removeprefix('mapgis_')
        hints = tool.annotations
        if hints is None:
            tool.annotations = (_READ if name in _UNANNOTATED_READS else
                                _LIFECYCLE if name in _LIFECYCLE_TOOLS else
                                _CANCEL if name == 'cancel_request' else _PREPARE)
        else:
            tool.annotations = _PREPARE.model_copy(update=hints.model_dump(exclude_none=True))


def _validate(tool: Tool, arguments: dict) -> None:
    schema = tool.parameters
    if (not isinstance(arguments, dict) or set(arguments) - schema.get('properties', {}).keys()
            or not Draft202012Validator(schema).is_valid(arguments)):
        raise BridgeError('INVALID_TOOL_ARGUMENTS')


class StructuredFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict):
        # FastMCP 1.x binds this method to the protocol with its own coercive
        # pre-validation disabled. Emit the same envelope on direct and wire calls.
        try:
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                raise BridgeError('UNKNOWN_TOOL')
            _validate(tool, arguments)
            return await tool.run(arguments, context=self.get_context())
        except Exception as exc:
            if isinstance(exc, ToolError) and exc.__cause__ is not None:
                exc = exc.__cause__
            error = classify_error(exc)
            return CallToolResult(content=[TextContent(type='text', text=json_text(error))],
                                  structuredContent=error, isError=True)


class _Operations:
    def __init__(self, legacy: FastMCP, root: Path):
        self.root = root
        self.tools = {tool.name: tool for tool in legacy._tool_manager.list_tools()}
        self.tools.update(_sprint2_tools(root))
        self.tools.update(_sprint3_tools(root))
        self.tools.update(_sprint4_tools(root))

    def describe(self, op_id: str, surface: str) -> dict:
        detail = describe_operation(self.root, op_id)
        if not detail['executable_by_mcp']:
            return detail | {'execution_status': 'HOST_OBSERVATION_REQUIRED'}
        tool = self.tools.get(detail['execution']['tool'])
        if tool is None:
            return detail | {'executable_by_mcp': False, 'execution_status': 'MCP_TOOL_NOT_LOADED'}
        detail = add_operation_schema(detail, tool.parameters)
        if surface != 'legacy':
            detail['execute_via'] = {'tool': 'mapgis_execute', 'op_id': op_id}
        return detail | {'execution_status': 'MCP_TOOL_AVAILABLE'}

    def plan(self, op_id: str, arguments: dict, request_id: str | None = None,
             *, read_only: bool = False) -> tuple[Tool, dict]:
        bound = dict(arguments)
        if op_id.startswith('mapgis_'):
            name = op_id
        else:
            detail = describe_operation(self.root, op_id)
            if not detail['executable_by_mcp']:
                raise BridgeError('HOST_OBSERVATION_REQUIRED')
            name = detail['execution']['tool']
            for key, value in operation_defaults(detail).items():
                if bound.setdefault(key, value) != value:
                    raise BridgeError('INVALID_TOOL_ARGUMENTS: operation selector conflicts with catalog ID')
        tool = self.tools.get(name)
        if tool is None:
            raise BridgeError('UNKNOWN_TOOL')
        if read_only and not tool.annotations.readOnlyHint:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: use execute for a state-changing operation')
        if request_id is not None and 'request_id' in tool.parameters.get('properties', {}):
            if bound.setdefault('request_id', request_id) != request_id:
                raise BridgeError('REQUEST_ID_CONFLICT')
        if name == 'mapgis_apply_patch' and bound.get('expected_epoch') is None:
            raise BridgeError('PUBLISHER_EPOCH_REQUIRED')
        _validate(tool, bound)
        return tool, bound

    @staticmethod
    def run(tool: Tool, arguments: dict) -> dict:
        try:
            return asyncio.run(tool.run(arguments))
        except ToolError as exc:
            if exc.__cause__ is None:
                raise
            raise exc.__cause__ from None

    async def invoke(self, name: str, arguments: dict) -> dict:
        def run():
            return self.run(*self.plan(name, arguments))
        return await asyncio.to_thread(run)

    def run_plan_node(self, node, inputs, context):
        name = (node.op if node.op.startswith('mapgis_') else
                describe_operation(self.root, node.op)['execution'].get('tool'))
        tool = self.tools.get(name)
        if tool is None:
            raise BridgeError('UNKNOWN_TOOL')
        schema = tool.parameters.get('properties', {})
        args = node.params.copy()
        groups = {}
        for item in inputs:
            groups.setdefault(item.role, []).append(item)
        for role, values in groups.items():
            if role in args:
                raise BridgeError('PLAN_INPUTS: a parameter would override a bound input role')
            field = schema.get(role, {})
            if field.get('type') == 'array':
                item_schema = field.get('items', {})
                args[role] = ([{'file': str(v.path), 'expected_sha256': v.sha256} for v in values]
                              if '$ref' in item_schema or item_schema.get('type') == 'object'
                              else [str(v.path) for v in values])
            elif len(values) == 1:
                args[role] = str(values[0].path)
            else:
                raise BridgeError('PLAN_INPUTS: only array arguments accept repeated roles')
        for input_name, hash_name in (('file', 'expected_sha256'), ('source', 'expected_sha256'),
                                      ('before', 'before_sha256'), ('after', 'after_sha256')):
            if input_name in groups and hash_name in schema:
                digest = groups[input_name][0].sha256
                if args.setdefault(hash_name, digest) != digest:
                    raise BridgeError('STALE_SOURCE')
        if 'expected_epoch' in schema:
            if args.setdefault('expected_epoch', context.publisher_epoch) != context.publisher_epoch:
                raise BridgeError('STALE_PUBLISHER_EPOCH')
        identity = 'mp-' + sha(json_text((context.plan.plan_id, node.id, context.cache_key)).encode('utf8'))[:45]
        result = self.run(*self.plan(node.op, args, identity))
        if result.get('state') == 'PREPARED' and (request_id := result.get('request_id')):
            result = Bridge(self.root).apply(request_id, context.publisher_epoch)
        outputs = {'report': result}
        if isinstance(result.get('output'), str):
            output = Path(result['output'])
            if output.is_file() and output.resolve().is_relative_to(self.root):
                outputs['file'] = output
        return outputs


class AuditSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file: str
    expected_sha256: Annotated[str, Field(pattern='^[a-f0-9]{64}$')]
    role: str | None = None


def _audit(root, files, reference_crs, before_files, legend_items, slib_path, options):
    from .drc import run_drc_audit
    values = dict(options or {})
    for name, value in (('before_files', before_files), ('legend_items', legend_items), ('slib_path', slib_path)):
        if value is not None:
            if name in values:
                raise BridgeError('INVALID_TOOL_ARGUMENTS: audit option was supplied twice')
            values[name] = value
    if {'root', 'enforce_frames', 'files', 'reference_crs'}.intersection(values):
        raise BridgeError('INVALID_TOOL_ARGUMENTS: reserved audit options')
    return run_drc_audit(files, reference_crs, root=root, enforce_frames=True, **values)


def _audit_tool(root):
    def mapgis_audit_check(files: Annotated[list[AuditSource], Field(min_length=1, max_length=32)],
                           reference_crs: str | None = None, before_files: list[AuditSource] | None = None,
                           legend_items: list[dict] | None = None, slib_path: str | None = None,
                           options: dict | None = None) -> dict:
        """Check saved topology, CRS, legend agreement and change accounting. Options bind label WT and rule tolerances."""
        return _audit(root, files, reference_crs, before_files, legend_items, slib_path, options)
    return mapgis_audit_check


def _sprint2_tools(root):
    registry = FastMCP('MapGIS saved-file operations')
    registry.add_tool(_audit_tool(root), annotations=_READ)

    def output_path(output):
        path = (root / output).resolve()
        if not any(path.is_relative_to(root / member) for member in ('work', 'deliverables')):
            raise BridgeError('Writes require a project-owned destination')
        return path

    @registry.tool(annotations=_READ)
    def mapgis_place_labels(file: str, expected_sha256: str,
                            solver: Literal['greedy_local', 'simulated_annealing'] = 'greedy_local',
                            options: dict | None = None) -> dict:
        """Propose an editable WT move patch and report overlap statistics."""
        from .label_placement import place_labels
        return place_labels(file, expected_sha256, solver=solver, **(options or {}))

    @registry.tool(annotations=_PREPARE)
    def mapgis_render_preview(files: list[AuditSource], output_png: str, options: dict | None = None) -> dict:
        """Render ordered saved vectors, or compare two owned PNGs with options.compare_png and files=[]."""
        from .visual_preview import render_diff_heatmap, render_overview
        values = dict(options or {})
        destination = output_path(output_png)
        if 'compare_png' in values:
            pair = values.pop('compare_png')
            if (files or values or not isinstance(pair, list) or len(pair) != 2
                    or any(not isinstance(path, str) for path in pair)):
                raise BridgeError('INVALID_TOOL_ARGUMENTS: pass two PNG names and no vector inputs for comparison')
            before, after = map(output_path, pair)
            absolute = output_path(destination.with_name(destination.stem + '-absolute.png'))
            if destination.exists() or absolute.exists():
                raise BridgeError('OUTPUT_CONFLICT')
            return render_diff_heatmap(before, after, destination)
        return render_overview([item.model_dump() for item in files], destination, **values)

    @registry.tool(annotations=_PREPARE)
    def mapgis_export_fdr(output_zip: str, options: dict | None = None) -> dict:
        """Export redacted checkpoint or recomputation evidence."""
        from .fdr import export_fdr_bundle
        return export_fdr_bundle(root, output_path(output_zip), **(options or {}))

    @registry.tool(annotations=_READ)
    def mapgis_probe_abi(install_dir: str | None = None) -> dict:
        """Probe exact core x86 modules and return the permitted ABI route."""
        import json
        from .abi_profiles import probe_installation
        if install_dir is None and (config := root / 'config.local.json').is_file():
            install_dir = json.loads(config.read_text(encoding='utf8')).get('mapgis_installation')
        return probe_installation(install_dir)

    return {tool.name: tool for tool in registry._tool_manager.list_tools()}


def _sprint3_tools(root):
    registry = FastMCP('MapGIS Sprint 3 operations')

    @registry.tool(annotations=_PREPARE)
    def mapgis_prepare_modern_crs(features: dict, template: dict, request_id: str) -> dict:
        """Prepare a native vector and SHA-256-bound WKT2 CRS sidecar for joint publication."""
        from .creation import prepare_modern_crs_creation
        return prepare_modern_crs_creation(Bridge(root), features, template, request_id) | {'request_id': request_id}

    @registry.tool(annotations=_PREPARE)
    def mapgis_prepare_drillhole_workflow(sources: dict, coordinate_frame: dict, parameters: dict, request_id: str) -> dict:
        """Prepare editable section and drillhole columns from four hash-bound survey tables."""
        from .geology_section import prepare_drillhole_workflow
        return prepare_drillhole_workflow(Bridge(root), sources, coordinate_frame, parameters, request_id) | {'request_id': request_id}

    @registry.tool(annotations=_LIFECYCLE)
    def mapgis_run_ui_macro(session_id: str, steps: list[dict], request_id: str,
                            artifact_checks: list[dict] | None = None, timeout_seconds: float = 4.) -> dict:
        """Run observed, verified state transitions in an existing authorized private session."""
        from .private_control import private_control
        return private_control(Bridge(root), 'macro', session_id=session_id, request_id=request_id,
            parameters={'steps': steps, 'artifact_checks': artifact_checks or [], 'timeout_seconds': timeout_seconds})

    @registry.tool(annotations=_READ)
    def mapgis_dbs_multisheet_validate(sheets: list[dict]) -> dict:
        """Validate standard sheet IDs, CRS and native field dictionaries. Each sheet has sheet_id and layers wt, wl, wp. Bind every layer with file and expected_sha256."""
        from .dbs_multisheet import validate_multisheet_schema
        return validate_multisheet_schema(sheets)

    @registry.tool(annotations=_PREPARE)
    def mapgis_dbs_multisheet_edge_match(left_sheet: dict, right_sheet: dict, tolerance: float,
            boundary_x_or_y: Literal['x', 'y'] | dict[str, float] | None = None,
            policy: Literal['strict_report', 'midpoint_snap_matching_attrs'] = 'strict_report',
            key_fields: list[str] | dict[str, list[str]] | None = None, output_dir: str | None = None) -> dict:
        """Report gaps, conflicting keys and unmatched WL/WP boundaries in metres. Midpoint policy branches only unique equal-attribute pairs. Sheet bindings follow dbs.multisheet_validate."""
        from .dbs_multisheet import edge_match_sheets
        return edge_match_sheets(left_sheet, right_sheet, boundary_x_or_y=boundary_x_or_y, tolerance=tolerance,
                                 policy=policy, key_fields=key_fields or (), root=root, output_dir=output_dir)

    @registry.tool(annotations=_PREPARE)
    def mapgis_dbs_multisheet_build(library_id: str, sheets: list[dict], tolerance: float = 0.,
                                    key_fields: list[str] | dict[str, list[str]] | None = None) -> dict:
        """Persist an immutable DBS sheet index with CRS sidecars and per-edge findings. Sheet bindings follow dbs.multisheet_validate."""
        from .dbs_multisheet import build_multisheet_library
        return build_multisheet_library(root, library_id, sheets, tolerance=tolerance, key_fields=key_fields or ())

    @registry.tool(annotations=_PREPARE)
    def mapgis_dbs_multisheet_window(library_id: str, bbox: list[float], output_dir: str) -> dict:
        """Clip the indexed sheets to xmin,ymin,xmax,ymax in their projected CRS. Publish editable nonempty native layers and record lineage to a new work or deliverables directory."""
        from .dbs_multisheet import export_multisheet_window
        return export_multisheet_window(root, library_id, bbox, output_dir)

    @registry.tool(annotations=_READ)
    def mapgis_probe_native_l2(format: Literal['4x', '5x'],
            parameter_block_hex: Annotated[str, Field(pattern='^(?:[0-9A-Fa-f]{66}|[0-9A-Fa-f]{112})$',
                                                       examples=['010000803f'+'00'*28])]) -> dict:
        """Read a caller-extracted packed projection block using the matching DLL. Supply 33 bytes for 4x, or 56 for 5x. Returns native slot indices and values."""
        from .native_l2 import probe_native_l2
        return probe_native_l2(root, format, parameter_block_hex)

    @registry.tool(annotations=_PREPARE)
    def mapgis_normalize_survey_wt(source_path: str, output_path: str, expected_sha256: str | None = None,
                                   map_scale: float | None = None) -> dict:
        """Write a fresh normalized WT under work or deliverables. Provide the source scale for a nonlocal WT with an unusable scale. The receipt records byte changes and native reopen."""
        from .native_l2 import normalize_survey_wt_header
        output = Bridge(root).destination_path(root/output_path)
        return normalize_survey_wt_header(root/source_path, output, expected_sha256=expected_sha256,
                                          map_scale=map_scale, root=root)

    return {tool.name: tool for tool in registry._tool_manager.list_tools()}


class InterchangeSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    file: str
    expected_sha256: Annotated[str, Field(pattern='^[a-f0-9]{64}$')]
    layer: str | None = None


def _sprint4_tools(root):
    registry = FastMCP('MapGIS interchange and provider operations')

    @registry.tool(annotations=_PREPARE)
    def mapgis_interchange_export(
            sources: Annotated[list[InterchangeSource], Field(min_length=1, max_length=100)],
            target: Literal['arcgis_pro', 'surpac', 'coreldraw', 'qgis_gpkg', 'geosciml_lite'],
            output_dir: str, crs: str | dict | None = None, metadata: dict | None = None) -> dict:
        """Publish a verified new package under work or deliverables. Match the input CRS and bind drillhole tables and default_z for Surpac."""
        from .interchange import export_interchange_package
        return export_interchange_package(root, [source.model_dump(exclude_none=True) for source in sources],
                                          target, output_dir, crs=crs, metadata=metadata)

    @registry.tool(annotations=_READ)
    def mapgis_interchange_verify(package_dir: str) -> dict:
        """Reopen the handoff and check the manifest, format-specific contents, counts and bounds."""
        from .interchange import verify_interchange_package
        return verify_interchange_package(root / package_dir)

    @registry.tool(annotations=_READ)
    def mapgis_provider_inspect(operation_id: str | None = None) -> dict:
        """Report installed and executable provider routes, including optional operation support."""
        from .providers import detect_providers
        return {'providers': detect_providers(root, operation_id=operation_id)}

    return {tool.name: tool for tool in registry._tool_manager.list_tools()}


def register_modern_tools(server: StructuredFastMCP, legacy: FastMCP, bridge: Bridge, surface: str) -> None:
    operations = _Operations(legacy, bridge.root)
    jobs = JobManager(bridge.root)
    jobs.reconcile_startup()

    @server.tool(annotations=_READ)
    async def mapgis_discover(query: str = '', module: str | None = None,
                              limit: Annotated[int, Field(ge=0)] = 20,
                              operation_id: str | None = None) -> dict:
        """Find ranked geological operations or describe an operation_id with its schema and example."""
        if operation_id is not None:
            return await asyncio.to_thread(operations.describe, operation_id, surface)
        return await asyncio.to_thread(find_operations, bridge.root, query, module, limit)

    @server.tool(annotations=_READ)
    async def mapgis_environment() -> dict:
        """Check the local MapGIS runtime, configured native routes and the current MCP surface."""
        result = await operations.invoke('mapgis_get_capabilities', {})
        result['loaded_runtime'].update(surface=surface, tool_count=len(await server.list_tools()))
        return result

    @server.tool(annotations=_READ)
    async def mapgis_inspect(op_id: str, arguments: dict) -> dict:
        """Run a registered read-only operation or inspection tool. Use discover for its argument schema."""
        def inspect():
            return operations.run(*operations.plan(op_id, arguments, read_only=True))
        return await asyncio.to_thread(inspect)

    @server.tool(annotations=_LIFECYCLE)
    async def mapgis_execute(op_id: str, arguments: dict, async_job: StrictBool = False,
                             request_id: str | None = None) -> dict:
        """Validate and call a registered operation ID or tool. Set async_job for a persistent job. Bind a stable request_id for retries."""
        tool, bound = await asyncio.to_thread(operations.plan, op_id, arguments, request_id)
        if not async_job:
            return await asyncio.to_thread(operations.run, tool, bound)

        def run(context: JobContext):
            context.update(10, 'validated')
            context.checkpoint()
            result = operations.run(tool, bound)
            context.update(95, 'recording_result')
            return result

        identity = request_id or bound.get('request_id') or None
        fingerprint = sha(json_text({'tool': tool.name, 'arguments': bound}).encode('utf-8'))
        return await asyncio.to_thread(jobs.submit, op_id, run, request_id=identity, fingerprint=fingerprint)

    @server.tool(annotations=_PREPARE)
    async def mapgis_sdk_flow(action: Literal['describe', 'prepare'], arguments: dict) -> dict:
        """Describe exact local SDK types or prepare a typed flow on owned copies. No raw addresses or unrestricted calls."""
        tool = 'mapgis_describe_sdk' if action == 'describe' else 'mapgis_prepare_sdk_flow'
        return await operations.invoke(tool, arguments)

    @server.tool(annotations=_LIFECYCLE)
    async def mapgis_private_ui(action: Literal['policy', 'open', 'status', 'observe', 'act', 'close', 'macro'],
                                module: str = '', session_id: str = '', parameters: dict | None = None,
                                request_id: str = '', token: str | None = None) -> dict:
        """Use an authorized exact-build NON-SERVER module in a newly owned private process. Observe before acting. Host input and existing windows are excluded."""
        if action == 'macro':
            return await operations.invoke('mapgis_run_ui_macro', {
                **(parameters or {}), 'session_id': session_id, 'request_id': request_id,
            })
        return await operations.invoke('mapgis_private_control', {
            'action': action, 'module': module, 'session_id': session_id, 'parameters': parameters,
            'request_id': request_id, 'token': token,
        })

    @server.tool(annotations=_LIFECYCLE)
    async def mapgis_workspace(kind: Literal['native', 'project'], action: str, workspace_id: str,
                               parameters: dict, expected_revision: StrictInt | None = None,
                               request_id: str | None = None) -> dict:
        """Manage a native or project-owned edit buffer with revision and request guards. External GUI buffers stay separate."""
        tool = 'mapgis_native_workspace' if kind == 'native' else 'mapgis_project_workspace'
        return await operations.invoke(tool, {'action': action, 'workspace_id': workspace_id, 'parameters': parameters,
                                              'expected_revision': expected_revision, 'request_id': request_id})

    @server.tool(annotations=_CANCEL)
    async def mapgis_job_manage(action: Literal['status', 'list', 'cancel'], job_id: str | None = None,
                                limit: Annotated[int, Field(ge=0, le=100)] = 20) -> dict:
        """Poll, list or request cancellation of persistent jobs. A running operation stops only at a safe checkpoint."""
        if action == 'list':
            return {'jobs': await asyncio.to_thread(jobs.list_jobs, limit)}
        if job_id is None:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: job_id is required')
        method = jobs.status if action == 'status' else jobs.cancel
        return await asyncio.to_thread(method, job_id)

    @server.tool(annotations=_PREPARE)
    async def mapgis_tx_commit(request_id: str, expected_epoch: Annotated[StrictInt, Field(ge=0)]) -> dict:
        """Publish a prepared request to a new branch with the active publisher epoch. Reconcile after interruption."""
        return await asyncio.to_thread(bridge.apply, request_id, expected_epoch)

    @server.tool(annotations=_LIFECYCLE)
    async def mapgis_tx_manage(action: Literal['status', 'cancel', 'current_epoch', 'advance_epoch'],
                               request_id: str | None = None) -> dict:
        """Inspect or cancel a request, read the current epoch or acquire the next publication epoch."""
        if action in {'current_epoch', 'advance_epoch'}:
            method = bridge.current_epoch if action == 'current_epoch' else bridge.advance_epoch
            return {'epoch': await asyncio.to_thread(method)}
        if request_id is None:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: request_id is required')
        return await asyncio.to_thread(bridge.status if action == 'status' else bridge.cancel, request_id)

    @server.tool(annotations=_PREPARE)
    async def mapgis_tx_recovery(action: Literal['artifact', 'native', 'prepare', 'verify'], arguments: dict) -> dict:
        """Reconcile artifact or native evidence, prepare a recovery branch, or verify saved records. Avoid blind replay."""
        tool = {'artifact': 'mapgis_reconcile_artifact', 'native': 'mapgis_reconcile_native_request',
                'prepare': 'mapgis_prepare_recovery', 'verify': 'mapgis_verify_request_output'}[action]
        return await operations.invoke(tool, arguments)

    @server.tool(annotations=_PREPARE)
    async def mapgis_export_plot(action: Literal['native', 'project', 'convert', 'series'], arguments: dict) -> dict:
        """Prepare native EPS, MPJ plot, PDF/RGB TIFF conversion or a map series. Use saved inputs and explicit native bounds."""
        tool = {'native': 'mapgis_prepare_native_plot', 'project': 'mapgis_prepare_project_plot',
                'convert': 'mapgis_prepare_plot_export', 'series': 'mapgis_prepare_map_series'}[action]
        return await operations.invoke(tool, arguments)

    server.add_tool(_threaded(_audit_tool(bridge.root)), annotations=_READ)

    @server.tool(annotations=_PREPARE)
    async def mapgis_run_plan(action: Literal['plan', 'diff', 'apply', 'resume', 'import_mpj'], arguments: dict) -> dict:
        """Plan, compare, execute or resume mapgis-plan/v1, or import ordered MPJ layers."""
        from .map_plan import MapPlanEngine
        engine = MapPlanEngine(bridge.root, operation_runner=operations)
        if 'executor' in arguments:
            raise BridgeError('INVALID_TOOL_ARGUMENTS: executor is local-only')
        return await asyncio.to_thread(getattr(engine, action), **arguments)

    @server.tool(annotations=_READ)
    async def mapgis_compare(before: str, after: str, limit: Annotated[int, Field(ge=0, le=1000)] = 100) -> dict:
        """Compare saved native vectors by physical records, geometry, attributes, style and coordinate headers."""
        return await operations.invoke('mapgis_compare_files', {'before': before, 'after': after, 'limit': limit})

    if surface == 'hybrid':
        for name, description in _DIRECT_TOOLS.items():
            tool = operations.tools[name]
            server.add_tool(_threaded(tool.fn), description=description, annotations=tool.annotations)


def _threaded(fn):
    @wraps(fn)
    async def call(**arguments):
        return await asyncio.to_thread(fn, **arguments)
    return call


_CARTOGRAPHY = """# 地质制图参考

- GB/T 958-2015《区域地质图图例》现行，2025-11-03 复审结论为继续有效。[国家标准信息公共服务平台](https://std.samr.gov.cn/gb/search/gbDetailed?id=71F772D80B51D3A7E05397BE0A0AB82A)
- DZ/T 0179-2025《地质图用色标准及用色原则（1∶50 000～1∶250 000）》现行，自 2025-12-31 实施。[国家数字标准馆](https://www.ndls.org.cn/standard/detail/5f981a8c4c23427569015cfc43372e7a)
- CGCS2000 三度带中央经线 120°E。EPSG:4528 带号 40 的假东距为 40,500,000 m，EPSG:4549 假东距为 500,000 m，差值为 40,000,000 m。[EPSG:4528](https://epsg.org/crs_4528/CGCS2000-3-degree-Gauss-Kruger-zone-40.html) [EPSG:4549](https://epsg.org/crs_4549/CGCS2000-3-degree-Gauss-Kruger-CM-120E.html)

出图前核对坐标轴顺序、长度单位、带号和范围。对投影信息缺失的图层，先查证来源并核验独立控制点。
成果验收包括图例与用色、原生文件读回和 PDF/TIFF 输出检查。
"""

_PROMPTS = {
    'geological-map-production': ('Produce an editable geological map',
        'Confirm map extent, the declared CRS and scale, layer order, symbol library and geological classifications. '
        'Read mapgis://standards/cartography. Inspect source fingerprints and build a portable MPJ with copied '
        'dependencies. Use native bounds for plotting and check saved EPS and PDF/TIFF against the scale and legend.'),
    'topology-clean-polygonize': ('Build topologically consistent geological areas',
        'Identify boundary lines by physical ID. Preserve faults and contours as separate roles. Confirm the '
        'coordinate unit and tolerance, node real intersections, inspect dangles and cuts, and verify holes and shared '
        'arcs in the new WP. Compare the result against the original interpretation before publication.'),
    'geochemistry-anomaly-workflow': ('Map geochemical anomalies with traceable thresholds',
        'Bind the assay table and sample coordinates. Audit units, detection limits and invalid samples. Choose a '
        'geological or sampling domain, record the estimator and test threshold sensitivity. Preserve separate '
        'background, anomaly and eligibility fields and verify the mapped attributes against the table.'),
    'drillhole-section-column': ('Prepare editable drillhole sections and columns',
        'Verify collar, survey and interval keys, depths, azimuth and dip conventions. Confirm the CRS, section '
        'direction, offset and scale. Preserve measured depths and distinguish trajectory interpolation from observations. '
        'Check interval alignment and verify the editable map artifacts.'),
    'scan-to-vector-geology': ('Trace and review geology from a georeferenced scan',
        'Register scan bytes and verify georeferencing against independent control points. Use bounded windows for '
        'candidate extraction, classify boundaries and fault or contour traces with source review, and reconcile '
        'tile boundaries. Publish only reviewed vector branches with uncertainty and input fingerprints recorded.'),
}


def register_resources_and_prompts(server: FastMCP, legacy: FastMCP, root: Path, surface: str) -> None:
    operations = _Operations(legacy, root)

    @server.resource('mapgis://operations/catalog', mime_type='application/json')
    async def operations_catalog() -> str:
        return json_text(await asyncio.to_thread(find_operations, root))

    @server.resource('mapgis://operations/{operation_id}', mime_type='application/json')
    async def operation_details(operation_id: str) -> str:
        return json_text(await asyncio.to_thread(operations.describe, operation_id, surface))

    @server.resource('mapgis://help/{module}/{topic_id}', mime_type='application/json')
    async def help_topic(module: str, topic_id: str) -> str:
        return json_text(await asyncio.to_thread(search_native_help, root, module, topic_id=topic_id))

    @server.resource('mapgis://standards/cartography', mime_type='text/markdown')
    def cartography_standards() -> str:
        return _CARTOGRAPHY

    def prompt_for(steps: str):
        def render(task: str = '') -> str:
            return steps + (f'\n\nTask context\n{task}' if task else '')
        return render

    for name, (title, steps) in _PROMPTS.items():
        server.prompt(name=name, description=title)(prompt_for(steps))
