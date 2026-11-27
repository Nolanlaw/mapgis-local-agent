"""In-process Python entry points for the same operations used by MapGIS MCP."""
from __future__ import annotations

import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from uuid import uuid4


SURFACES = ('legacy', 'compact', 'hybrid')


@dataclass(frozen=True, slots=True)
class MapGISConfig:
    """The local task root and the MCP surface advertised by discovery."""

    root: Path | str
    surface: str = 'hybrid'

    def __post_init__(self):
        object.__setattr__(self, 'root', Path(self.root).resolve())
        if self.surface not in SURFACES:
            raise ValueError('INVALID_SURFACE: choose legacy, compact or hybrid')


class MapPlan:
    """Build a mapgis-plan/v1 specification with ordered, repeatable input roles."""

    def __init__(self, plan_id: str, crs: str, unit: str, scale: float, *,
                 description: str = '', seed: int = 0, slib_sha256: str | None = None):
        self._spec = dict(plan_id=plan_id, crs=crs, unit=unit, scale=scale,
                          description=description, seed=seed, slib_sha256=slib_sha256, nodes=[])

    def add_node(self, id: str, op: str, *, inputs=(), params=None, effect=None, cost_hint=None) -> MapPlan:
        """Append a node. Supply inputs as a sequence, including repeated roles."""
        from .map_plan import PlanNode

        node = PlanNode(id=id, op=op, inputs=list(inputs), params={} if params is None else params,
                        effect={} if effect is None else effect, cost_hint=cost_hint)
        self._spec['nodes'].append(node)
        return self

    def to_spec(self) -> dict:
        """Return an independent, JSON-ready spec accepted by MapPlanEngine."""
        from .map_plan import parse_spec

        return parse_spec(self._spec).model_dump(mode='json')


class Workspace:
    """Own one native edit buffer. Save explicitly; context exit discards unsaved edits."""

    def __init__(self, client: MapGISClient, source: str | Path, expected_sha256: str, workspace_id: str):
        self.id = workspace_id
        self._client = client
        self._source = {'source': str(client.root / source), 'expected_sha256': expected_sha256}
        self._manager = None
        self._state = None

    @property
    def revision(self) -> int:
        if self._state is None:
            raise RuntimeError('Enter the workspace context before using its revision')
        return self._state['revision']

    def __enter__(self) -> Workspace:
        from .workspace import NativeWorkspaces

        if self._manager is not None or self._state is not None:
            raise RuntimeError('This workspace context has already been entered')
        self._manager = NativeWorkspaces(self._client._bridge)
        try:
            result = self._manager.execute('open', self.id, self._source)
            if result['state'] != 'OPEN' or not result['running']:
                raise RuntimeError(f'Workspace {self.id} is {result["state"]}; recover it using the workspace operation')
            self._state = result
        except BaseException:
            self._release()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._manager is not None:
            try:
                self.close(discard=True)
            except Exception as close_error:
                self._release()
                if exc is None:
                    raise
                exc.add_note(f'Workspace {self.id} close failed: {close_error}. Inspect its journal before replay.')

    def _release(self):
        manager, self._manager = self._manager, None
        if manager is not None:
            atexit.unregister(manager.close_all)
            manager.close_all()

    def _action(self, action, parameters, *, expected_revision=None, request_id=None):
        if self._manager is None:
            raise RuntimeError('The workspace context is inactive')
        if action != 'status' and expected_revision is None:
            expected_revision = self.revision
        result = self._manager.execute(action, self.id, parameters, expected_revision, request_id)
        self._state = result.get('current_workspace', result)
        return result

    def status(self) -> dict:
        return self._action('status', {})

    def edit(self, operation: str, parameters: dict, *, request_id: str, expected_revision: int | None = None) -> dict:
        return self._action('edit', {'operation': operation, 'parameters': parameters},
                            expected_revision=expected_revision, request_id=request_id)

    def save(self, *, request_id: str, expected_revision: int | None = None) -> dict:
        return self._action('save', {}, expected_revision=expected_revision, request_id=request_id)

    def recover(self) -> dict:
        self.status()
        return self._action('recover', {})

    def close(self, *, discard: bool = False) -> dict:
        if self._manager is None and self._state is not None and self._state['state'] == 'CLOSED':
            return self._state.copy()
        result = self._action('close', {'discard': discard})
        self._release()
        return result


class MapGISClient:
    """Synchronous local SDK. Use the task root and the same bound inputs as MCP."""

    def __init__(self, root: str | Path | MapGISConfig, surface: str = 'hybrid'):
        self.config = root if isinstance(root, MapGISConfig) else MapGISConfig(root, surface)
        self.root = self.config.root
        self.surface = self.config.surface

    @cached_property
    def _bridge(self):
        from .core import Bridge

        return Bridge(self.root)

    @cached_property
    def _legacy(self):
        from .server import _legacy_registry
        from .workspace import NativeWorkspaces

        return _legacy_registry(self._bridge, NativeWorkspaces(self._bridge))

    @cached_property
    def _operations(self):
        from .mcp_surface import _Operations

        return _Operations(self._legacy, self.root)

    @cached_property
    def _job_dispatch(self):
        from .mcp_surface import StructuredFastMCP, register_modern_tools

        server = StructuredFastMCP(self._legacy.name)
        register_modern_tools(server, self._legacy, self._bridge, 'compact')
        return server._tool_manager.get_tool('mapgis_execute')

    @staticmethod
    def _run(tool, arguments) -> dict:
        from .mcp_surface import _Operations

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _Operations.run(tool, arguments)
        # Synchronous calls also work in notebook kernels with a running loop.
        with ThreadPoolExecutor(max_workers=1) as worker:
            return worker.submit(_Operations.run, tool, arguments).result()

    def discover(self, query: str = '', module: str | None = None, limit: int = 20) -> dict:
        from .operations import find_operations

        return find_operations(self.root, query, module, limit)

    def describe(self, op_id: str) -> dict:
        return self._operations.describe(op_id, self.surface)

    def inspect_file(self, file: str | Path) -> dict:
        return self._bridge.inspect(str(self.root / file))

    def execute(self, op_id: str, arguments: dict, *, async_job: bool = False, request_id: str | None = None) -> dict:
        if type(async_job) is not bool or not isinstance(arguments, dict):
            raise ValueError('INVALID_TOOL_ARGUMENTS: expected a Boolean async_job and an arguments object')
        if async_job:
            from .mcp_surface import _validate

            values = dict(op_id=op_id, arguments=arguments, async_job=True, request_id=request_id)
            _validate(self._job_dispatch, values)
            return self._run(self._job_dispatch, values)
        return self._run(*self._operations.plan(op_id, arguments, request_id))

    def run_plan(self, action: str, spec_or_id, **kwargs) -> dict:
        from .map_plan import MapPlanEngine

        if action not in {'plan', 'diff', 'apply', 'resume', 'import_mpj'}:
            raise ValueError('Invalid plan action: use plan, diff, apply, resume or import_mpj')
        engine = MapPlanEngine(self.root, operation_runner=self._operations)
        if isinstance(spec_or_id, MapPlan):
            spec_or_id = spec_or_id.to_spec()
        if isinstance(kwargs.get('previous_state_or_spec'), MapPlan):
            kwargs['previous_state_or_spec'] = kwargs['previous_state_or_spec'].to_spec()
        return getattr(engine, action)(spec_or_id, **kwargs)

    def _sources(self, files) -> list[dict]:
        return [dict(source, file=str(self.root / source['file'])) for source in files]

    def audit_drc(self, files, **kwargs) -> dict:
        if kwargs.get('before_files') is not None:
            kwargs['before_files'] = self._sources(kwargs['before_files'])
        if kwargs.get('slib_path') is not None:
            kwargs['slib_path'] = str(self.root / kwargs['slib_path'])
        return self.execute('mapgis_audit_check', dict(files=self._sources(files), **kwargs))

    def place_labels(self, file, expected_sha256, *, solver='greedy_local', options=None, **kwargs) -> dict:
        return self.execute('mapgis_place_labels', dict(file=str(self.root / file), expected_sha256=expected_sha256,
                                                       solver=solver, options=dict(options or {}, **kwargs)))

    def render_preview(self, files, output_png, *, options=None, **kwargs) -> dict:
        return self.execute('mapgis_render_preview', dict(files=self._sources(files), output_png=str(output_png),
                                                         options=dict(options or {}, **kwargs)))

    def export_fdr(self, output_zip, *, options=None, **kwargs) -> dict:
        return self.execute('mapgis_export_fdr', dict(output_zip=str(output_zip), options=dict(options or {}, **kwargs)))

    def export_interchange(self, sources, target, output_dir, **kwargs) -> dict:
        return self.execute('mapgis_interchange_export', dict(sources=self._sources(sources), target=target,
                                                             output_dir=str(output_dir), **kwargs))

    def verify_interchange(self, package_dir) -> dict:
        return self.execute('mapgis_interchange_verify', {'package_dir': str(package_dir)})

    def probe_abi(self, install_dir: str | Path | None = None) -> dict:
        import json

        from .abi_profiles import probe_installation

        config = self.root / 'config.local.json'
        if install_dir is None and config.is_file():
            install_dir = json.loads(config.read_text(encoding='utf-8-sig')).get('mapgis_installation')
        return probe_installation(install_dir)

    def detect_providers(self, *, operation_id: str | None = None) -> list[dict]:
        from .providers import detect_providers

        return detect_providers(self.root, operation_id=operation_id)

    def current_epoch(self) -> int:
        return self._bridge.current_epoch()

    def advance_epoch(self) -> int:
        return self._bridge.advance_epoch()

    def commit(self, request_id: str, expected_epoch: int) -> dict:
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise ValueError('PUBLISHER_EPOCH_REQUIRED: supply the active nonnegative integer epoch')
        return self._bridge.apply(request_id, expected_epoch)

    def open_workspace(self, source: str | Path, expected_sha256: str, *, workspace_id: str | None = None) -> Workspace:
        return Workspace(self, source, expected_sha256, workspace_id if workspace_id is not None else 'ws-' + uuid4().hex)
