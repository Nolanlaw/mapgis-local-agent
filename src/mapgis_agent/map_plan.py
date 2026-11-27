"""Typed map plans, ordered input fingerprints and resumable CAS transactions."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, JsonValue, StrictBool, StrictInt, model_validator

from .core import Bridge, BridgeError, json_text, sha
from .drc import check_frame_values, check_frames, frame_unit, normalize_unit, parse_crs, read_layer
from .object_store import ContentAddressedStore, file_sha256


VERSION = 'mapgis-plan/v1'
BACKEND_VERSION = 'mapgis-v3-sprint2.1'
BUILTIN_OUTPUTS = {'drc.audit': {'report'}, 'labels.place': {'report'}, 'preview.render': {'preview', 'report'},
                   'work.copy': {'file'}, 'file.patch': {'file', 'report'}, 'native.inspect': {'report'}}
BUILTINS = BUILTIN_OUTPUTS.keys()
VECTORS = {'.wt', '.wl', '.wp'}
Identity = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$')]
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Bounds = tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class PlanInput(_Model):
    role: Identity
    source_file: Annotated[str, Field(min_length=1)] | None = None
    from_node: Identity | None = None
    output_key: Identity | None = None
    expected_sha256: Digest | None = None

    @model_validator(mode='after')
    def check_binding(self):
        if bool(self.source_file) == bool(self.from_node):
            raise ValueError('Specify exactly one of source_file and from_node')
        if self.source_file is not None and (self.output_key is not None or self.expected_sha256 is None):
            raise ValueError('A source_file requires expected_sha256 and has no output_key')
        if self.from_node is not None and self.output_key is None:
            raise ValueError('A from_node binding requires output_key')
        return self


class PlanEffect(_Model):
    read_files: list[str] = Field(default_factory=list)
    spatial_bounds: Bounds | None = None
    exclusive_session: Identity | StrictBool | None = None
    output_role: Identity | None = None
    refresh_region: Bounds | StrictBool | None = None

    @model_validator(mode='after')
    def check_bounds(self):
        for bounds in (self.spatial_bounds, self.refresh_region):
            if isinstance(bounds, tuple) and (bounds[0] > bounds[2] or bounds[1] > bounds[3]):
                raise ValueError('Bounds must be xmin, ymin, xmax, ymax')
        return self


class PlanNode(_Model):
    id: Identity
    op: Annotated[str, Field(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,99}$')]
    inputs: list[PlanInput] = Field(default_factory=list)
    params: dict[str, JsonValue] = Field(default_factory=dict)
    effect: PlanEffect = Field(default_factory=PlanEffect)
    cost_hint: Annotated[FiniteFloat, Field(ge=0)] | Literal['low', 'medium', 'high'] | None = None


class MapPlan(_Model):
    schema_version: Literal['mapgis-plan/v1'] = VERSION
    plan_id: Identity
    description: str = ''
    crs: Annotated[str, Field(min_length=1)]
    unit: Annotated[str, Field(min_length=1)]
    scale: Annotated[FiniteFloat, Field(gt=0)]
    seed: StrictInt = 0
    slib_sha256: Digest | None = None
    nodes: Annotated[list[PlanNode], Field(min_length=1)]


def parse_spec(spec) -> MapPlan:
    if isinstance(spec, MapPlan):
        return spec
    if isinstance(spec, dict):
        return MapPlan.model_validate(spec)
    if isinstance(spec, Path) or (isinstance(spec, str) and not spec.lstrip().startswith(('{', '['))
                                 and '\n' not in spec and Path(spec).suffix.lower() in {'.json', '.yaml', '.yml'}):
        spec = Path(spec).read_text(encoding='utf-8-sig')
    try:
        return MapPlan.model_validate_json(spec)
    except ValueError:
        if not isinstance(spec, (str, bytes)) or (spec.decode() if isinstance(spec, bytes) else spec).lstrip().startswith(('{', '[')):
            raise
        if importlib.util.find_spec('yaml') is None:
            raise BridgeError('PLAN_FORMAT: YAML is optional. Submit mapgis-plan/v1 JSON.')
        import yaml
        return MapPlan.model_validate(yaml.safe_load(spec))


@dataclass(frozen=True)
class ResolvedInput:
    role: str
    sha256: str
    output_key: str | None = None
    path: Path | None = None


@dataclass(frozen=True)
class NodeContext:
    plan: MapPlan
    directory: Path
    root: Path
    cache_key: str
    publisher_epoch: int


def _digest(data):
    return sha(json_text(data).encode('utf-8'))


def compute_cache_key(node, resolved_inputs, plan_ctx) -> str:
    if not isinstance(node, PlanNode):
        node = PlanNode.model_validate(node)
    context = plan_ctx if isinstance(plan_ctx, dict) else plan_ctx.model_dump(mode='json')
    ordered = []
    for index, item in enumerate(resolved_inputs):
        if isinstance(item, ResolvedInput):
            role, digest, output_key = item.role, item.sha256, item.output_key
        else:
            role = item.get('role', node.inputs[index].role if index < len(node.inputs) else None)
            digest = item['sha256']
            output_key = item.get('output_key', node.inputs[index].output_key if index < len(node.inputs) else None)
        ordered.append((index, role, digest, output_key))
    return _digest({'inputs': ordered, 'op': node.op, 'params': node.params,
                    **{k: context.get(k) for k in ('crs', 'unit', 'scale', 'slib_sha256', 'seed')},
                    'backend_version': context.get('backend_version', BACKEND_VERSION)})


def _ordered(spec):
    nodes = {node.id: node for node in spec.nodes}
    if len(nodes) != len(spec.nodes):
        raise BridgeError('PLAN_DUPLICATE_NODE')
    pending, complete, dependencies = {}, {}, {}
    for node in spec.nodes:
        references = {item.from_node for item in node.inputs if item.from_node is not None}
        if references - nodes.keys():
            raise BridgeError(f'PLAN_UNKNOWN_NODE: {node.id} references a missing producer')
        pending[node.id] = references
    result = []
    while pending:
        ready = [key for key, deps in pending.items() if deps <= complete.keys()]
        if not ready:
            raise BridgeError('PLAN_CYCLE: input references contain a cycle')
        for key in ready:
            ancestry = set().union(*(dependencies[ref] | {ref} for ref in pending.pop(key)))
            node = nodes[key]
            session = node.effect.exclusive_session
            if session:
                for other in result:
                    other_session = other.effect.exclusive_session
                    if other_session and (session is True or other_session is True or other_session == session):
                        if other.id not in ancestry:
                            raise BridgeError(f'PLAN_EXCLUSIVE_SESSION: add a dependency between {other.id} and {node.id}')
            dependencies[key] = ancestry
            complete[key] = node
            result.append(node)
    return result


def _write_json(path, value):
    content = json_text(value).encode('utf-8')
    fd, raw = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.stem}-', suffix='.tmp')
    temporary = Path(raw)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


@contextmanager
def _plan_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / 'plan.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BridgeError('PLAN_BUSY: an active controller owns this plan') from exc
        yield  # Closing the descriptor releases the lease even if the controller fails.


def _basename(name):
    if (not isinstance(name, str) or Path(name).name != name or any(c in name for c in '\\/:')
            or name in ('', '.', '..') or name.rstrip(' .') != name):
        raise BridgeError('PLAN_OUTPUT_NAME: use a single portable basename')
    return name


class MapPlanEngine:
    """Use a project's epoch and a separate per-plan journal and immutable object store."""

    def __init__(self, root, *, backend_version=BACKEND_VERSION, operation_runner=None):
        self.root = Path(root).resolve()
        self.directory = self.root / 'work' / 'plans'
        self.store = ContentAddressedStore(self.directory / 'objects')
        self.backend_version = backend_version
        self.operation_runner = operation_runner

    def _folder(self, plan_id):
        Bridge.request_id(plan_id.replace('.', '_'))
        if plan_id.upper() == 'OBJECTS':
            raise BridgeError('PLAN_ID: objects is reserved')
        folder = self.directory / plan_id
        if folder.resolve() != folder:
            raise BridgeError('PLAN_PATH: state must remain within the project')
        return folder

    def _context(self, spec):
        return {**spec.model_dump(mode='json', exclude={'nodes', 'description'}),
                'backend_version': self.backend_version}

    @contextmanager
    def _fence(self, expected):
        db_path = self.root / 'evidence' / 'commands.sqlite3'
        if not db_path.is_file():
            if expected is not None and (type(expected) is not int or expected != 0):
                raise BridgeError('STALE_PUBLISHER_EPOCH: active epoch is 0')
            yield 0
            return
        with closing(sqlite3.connect(db_path, timeout=15, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            # Hold the same write lock as advance_epoch through the atomic publication.
            db.execute('BEGIN IMMEDIATE')
            try:
                epoch = Bridge._epoch(db)
                if expected is not None and (type(expected) is not int or expected != epoch):
                    raise BridgeError(f'STALE_PUBLISHER_EPOCH: active epoch is {epoch}')
                yield epoch
            finally:
                db.rollback()

    def _validate(self, spec, *, check_sources):
        from .operations import _BY_ID
        from .server import registered_input_schemas
        ordered = _ordered(spec)
        crs = parse_crs(spec.crs)
        if not normalize_unit(spec.unit):
            raise BridgeError('AUDIT_CRS_MISMATCH: empty unit')
        check_frame_values(crs, unit=spec.unit)
        sources = {}
        nodes = {node.id: node for node in ordered}
        for node in ordered:
            if node.op not in BUILTINS and node.op not in _BY_ID and node.op not in registered_input_schemas():
                raise BridgeError(f'UNKNOWN_OPERATION: {node.op}')
            if node.op in {'mapgis_private_control', 'native.private_control', 'mapgis_get_live_state', 'native.live_state'}:
                raise BridgeError('PLAN_EFFECT: use an explicit native session outside the saved-file plan')
            for bounds in (node.effect.spatial_bounds, node.effect.refresh_region):
                if isinstance(bounds, tuple):
                    check_frame_values(crs, bounds)
            files = {item.source_file for item in node.inputs if item.source_file is not None}
            if set(node.effect.read_files) - files:
                raise BridgeError('PLAN_UNBOUND_READ: bind each read_file as a hashed input')
            for item in node.inputs:
                if item.from_node and (declared := BUILTIN_OUTPUTS.get(nodes[item.from_node].op)):
                    if item.output_key not in declared:
                        raise BridgeError(f'PLAN_OUTPUT_KEY: {item.from_node}.{item.output_key} is not declared')
            if not check_sources:
                continue
            for item in node.inputs:
                if not item.source_file:
                    continue
                source = (self.root / item.source_file).resolve(strict=True)
                prior = sources.get(item.source_file)
                if prior is not None and prior['sha256'] != item.expected_sha256:
                    raise BridgeError('STALE_SOURCE: one path has inconsistent plan bindings')
                if prior is not None:
                    continue
                digest = file_sha256(source)
                if digest != item.expected_sha256:
                    raise BridgeError('STALE_SOURCE: a source does not match its plan fingerprint')
                if source.suffix.lower() in VECTORS:
                    layer = read_layer({'file': str(source), 'expected_sha256': digest})
                    check_frames(crs, layer.crs)
                    check_frame_values(crs, layer.reader.bbox if layer.geometries else None, spec.unit)
                    if layer.scale is not None and not math.isclose(layer.scale, spec.scale):
                        raise BridgeError('AUDIT_CRS_MISMATCH: plan scale differs from the saved source')
                sources[item.source_file] = {'sha256': digest, 'bytes': source.stat().st_size,
                                             'name': _basename(source.name), 'path': str(source)}
        return ordered, sources

    def _objects_valid(self, outputs, checked=None):
        if not isinstance(outputs, dict) or not outputs:
            return False
        checked = {} if checked is None else checked
        for obj in outputs.values():
            try:
                identity = (obj['sha256'], obj['bytes'])
                path = self.store.path_for(obj['sha256'])
                if identity not in checked:
                    checked[identity] = (path.resolve() == path and path.is_file()
                                         and path.stat().st_size == obj['bytes'] and file_sha256(path) == obj['sha256'])
                if not checked[identity]:
                    return False
            except (KeyError, OSError, TypeError, ValueError):
                return False
        return True

    def _preview(self, spec):
        ordered, source = self._validate(spec, check_sources=True)
        folder = self._folder(spec.plan_id)
        cache = _read_json(folder / 'cache_index.json', {})
        predicted, keyed, checked = {}, {}, {}
        for node in ordered:
            resolved, deferred = [], []
            for item in node.inputs:
                if item.source_file:
                    obj = source[item.source_file]
                else:
                    outputs = predicted[item.from_node]
                    obj = outputs.get(item.output_key) if outputs is not None else None
                    if outputs is not None and obj is None:
                        raise BridgeError(f'PLAN_OUTPUT_KEY: {item.from_node}.{item.output_key} is missing')
                if obj is not None and item.expected_sha256 is not None and obj['sha256'] != item.expected_sha256:
                    raise BridgeError('STALE_SOURCE: a planned input digest differs from its producer')
                digest = (obj or {}).get('sha256') or item.expected_sha256
                if digest is None:
                    deferred.append(item.from_node)
                    digest = _digest((keyed[item.from_node]['cache_key'], item.output_key))
                resolved.append(ResolvedInput(item.role, digest, item.output_key))
            key = compute_cache_key(node, resolved, self._context(spec))
            outputs = cache.get(key)
            hit = not deferred and self._objects_valid(outputs, checked)
            predicted[node.id] = outputs if hit else None
            keyed[node.id] = {'id': node.id, 'op': node.op, 'cache_key': key, 'cache_hit': hit,
                              'deferred_inputs': list(dict.fromkeys(deferred))}
        return list(keyed.values()), source

    def plan(self, spec):
        spec = parse_spec(spec)
        nodes, _ = self._preview(spec)
        return {'plan_id': spec.plan_id, 'schema_version': VERSION, 'nodes': nodes,
                'topological_order': [node['id'] for node in nodes]}

    def diff(self, current_spec, previous_state_or_spec):
        spec = parse_spec(current_spec)
        prediction = self.plan(spec)['nodes']
        current = {node.id: node for node in spec.nodes}
        previous = previous_state_or_spec
        if isinstance(previous, dict) and 'completed' in previous:
            old_spec = parse_spec(previous['spec'])
            old_keys = {key: row['cache_key'] for key, row in previous['completed'].items()}
        else:
            old_spec = parse_spec(previous)
            old_keys = {}  # The earlier spec can outlive its original source bytes.
        old_nodes = {node.id: node for node in old_spec.nodes}
        context_fields = ('crs', 'unit', 'scale', 'seed', 'slib_sha256')
        context_changes = [f'plan.{field} changed' for field in context_fields
                           if getattr(spec, field) != getattr(old_spec, field)]
        reasons, cached, recompute, added = {}, [], [], []
        for row in prediction:
            key = row['id']
            new_node, old_node = current[key], old_nodes.get(key)
            why = list(context_changes)
            if old_node is None:
                added.append(key)
                reasons[key] = ['node added']
                continue
            for field in ('op', 'inputs', 'params'):
                if getattr(new_node, field) != getattr(old_node, field):
                    why.append(f'{field} changed')
            for item in new_node.inputs:
                if item.from_node in reasons:
                    why.extend(f'{item.from_node}: {reason}' for reason in reasons[item.from_node])
            if key in old_keys and old_keys[key] != row['cache_key'] and not why:
                why.append('resolved input, backend version or cache key changed')
            if row['cache_hit'] and not why:
                cached.append(key)
            else:
                recompute.append(key)
                reasons[key] = list(dict.fromkeys(why or ['verified cache object unavailable']))
        removed = [node.id for node in old_spec.nodes if node.id not in current]
        return {'unchanged_cached': cached, 'recompute_required': recompute,
                'added': added, 'removed': removed, 'reasons': reasons | {key: ['node removed'] for key in removed}}

    def _materialize(self, obj, folder):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / _basename(obj['name'])
        if target.resolve() != target:
            raise BridgeError('PLAN_PATH: materialized data must be project-local')
        if target.exists():
            if file_sha256(target) != obj['sha256']:
                raise BridgeError('PLAN_OUTPUT_CHANGED: a previously materialized copy changed')
        else:
            self.store.copy_to(obj['sha256'], target, expected_size=obj['bytes'])
        return target

    def _resolve(self, node, state, directory):
        resolved = []
        for index, item in enumerate(node.inputs):
            outputs = state['completed'][item.from_node]['outputs'] if item.from_node else None
            obj = state['sources'][item.source_file] if outputs is None else outputs.get(item.output_key)
            if obj is None:
                raise BridgeError(f'PLAN_OUTPUT_KEY: {item.from_node}.{item.output_key} is missing')
            if item.expected_sha256 is not None and obj['sha256'] != item.expected_sha256:
                raise BridgeError('STALE_SOURCE: resolved input differs from its bound fingerprint')
            directory_key = obj['sha256'][:16]
            path = self._materialize(obj, directory / 'inputs' / node.id / str(index) / directory_key)
            resolved.append(ResolvedInput(item.role, obj['sha256'], item.output_key, path))
        return resolved

    def _install(self, node, data, context):
        if not isinstance(data, dict):
            raise BridgeError('PLAN_RESULT: executor must return a mapping of outputs')
        outputs = {}
        for role, value in data.items():
            _basename(role)
            if isinstance(value, Path):
                path = value.resolve(strict=True)
                if not path.is_relative_to(context.root):
                    raise BridgeError('PLAN_OUTPUT_PATH: executor outputs must be project-owned')
            else:
                path = context.directory / (_basename(role) + '.json')
                _write_json(path, value)
            obj = self.store.put(path)
            outputs[role] = {'name': _basename(path.name), 'sha256': obj.digest, 'bytes': obj.size}
        if not outputs:
            raise BridgeError(f'PLAN_RESULT: no outputs from {node.id}')
        return outputs

    def _publish(self, state, cache, folder, epoch):
        with self._fence(epoch):
            _write_json(folder / 'cache_index.json', cache)
            _write_json(folder / 'state.json', state)

    def _run(self, spec, ordered, state, cache, folder, epoch, executor, fault, *, resuming):
        reused = [node.id for node in ordered if node.id in state['completed']]
        executed = []
        checked = {}
        for node_id in reused:
            checkpoint = state['completed'][node_id]
            if not self._objects_valid(checkpoint['outputs'], checked):
                raise BridgeError('PLAN_CHECKPOINT_CORRUPT: restore a completed CAS object before resuming')
            key = checkpoint['cache_key']
            checkpoint['paths'] = {name: str(self._materialize(obj, folder / 'outputs' / node_id / key / name))
                                   for name, obj in checkpoint['outputs'].items()}
        state.update(status='RUNNING', interruption=None, publisher_epoch=epoch)
        self._publish(state, cache, folder, epoch)
        for node in ordered:
            if node.id in state['completed']:
                continue
            try:
                if node.id == fault:
                    state.update(status='INTERRUPTED', interruption={'node': node.id, 'code': 'FAULT_INJECTED'})
                    self._publish(state, cache, folder, epoch)
                    break
                inputs = self._resolve(node, state, folder)
                key = compute_cache_key(node, inputs, self._context(spec))
                outputs = cache.get(key)
                hit = self._objects_valid(outputs, checked)
                if hit:
                    reused.append(node.id)
                else:
                    context = NodeContext(spec, folder / 'runs' / node.id / key, self.root, key, epoch)
                    context.directory.mkdir(parents=True, exist_ok=True)
                    with self._fence(epoch):
                        pass
                    data = executor(node, inputs, context) if executor else None
                    if data is None:
                        data = self._execute(node, inputs, context)
                    outputs = self._install(node, data, context)
                    cache[key] = outputs
                    executed.append(node.id)

                visible = {name: str(self._materialize(obj, folder / 'outputs' / node.id / key / name))
                           for name, obj in outputs.items()}
                state['completed'][node.id] = {'cache_key': key, 'outputs': outputs, 'paths': visible}
            except Exception as exc:
                state.update(status='INTERRUPTED', interruption={'node': node.id, 'code': type(exc).__name__})
                self._publish(state, cache, folder, epoch)
                raise
            self._publish(state, cache, folder, epoch)
        else:
            state['status'] = 'COMPLETE'
            self._publish(state, cache, folder, epoch)
        return state | {'executed_nodes': executed, 'reused_nodes': reused, 'resumed': resuming}

    def apply(self, spec, *, executor=None, fault_inject_node=None, expected_epoch=None):
        spec = parse_spec(spec)
        ordered, sources = self._validate(spec, check_sources=True)
        if fault_inject_node is not None and fault_inject_node not in {node.id for node in ordered}:
            raise BridgeError('PLAN_UNKNOWN_NODE: fault injection requires an existing node')
        folder = self._folder(spec.plan_id)
        with _plan_lock(folder):
            with self._fence(expected_epoch) as epoch:
                pass
            for obj in sources.values():
                self.store.put(obj['path'], expected_digest=obj['sha256'])
            data = spec.model_dump(mode='json')
            state = {'schema_version': VERSION, 'plan_id': spec.plan_id, 'spec': data, 'spec_sha256': _digest(data),
                     'topological_order': [node.id for node in ordered], 'completed': {}, 'sources': sources,
                     'backend_version': self.backend_version, 'publisher_epoch': epoch}
            cache = _read_json(folder / 'cache_index.json', {})
            return self._run(spec, ordered, state, cache, folder, epoch, executor, fault_inject_node, resuming=False)

    def resume(self, plan_id, *, executor=None, expected_epoch=None):
        folder = self._folder(plan_id)
        if not (folder / 'state.json').is_file():
            raise BridgeError('PLAN_MISSING')
        with _plan_lock(folder):
            state = _read_json(folder / 'state.json')
            if (state['plan_id'] != plan_id or _digest(state['spec']) != state['spec_sha256']
                    or state['backend_version'] != self.backend_version):
                raise BridgeError('PLAN_CHECKPOINT_CORRUPT: state identity, specification or backend changed')
            spec = parse_spec(state['spec'])
            ordered, _ = self._validate(spec, check_sources=False)
            keys = [node.id for node in ordered]
            completed = state['completed']
            if state['topological_order'] != keys or set(completed) != set(keys[:len(completed)]):
                raise BridgeError('PLAN_CHECKPOINT_CORRUPT: completed nodes must be a topological prefix')
            with self._fence(expected_epoch if expected_epoch is not None else state['publisher_epoch']) as epoch:
                pass
            cache = _read_json(folder / 'cache_index.json', {})
            return self._run(spec, ordered, state, cache, folder, epoch, executor, None, resuming=True)

    def _execute(self, node, inputs, ctx):
        params = node.params.copy()
        paths = [item.path for item in inputs]
        bound = [{'file': str(item.path), 'expected_sha256': item.sha256} for item in inputs]
        if node.op == 'work.copy' and len(paths) == 1:
            path = ctx.directory / _basename(params.pop('name', paths[0].name))
            if params:
                raise BridgeError('PLAN_PARAMETERS: work.copy accepts only a basename')
            shutil.copyfile(paths[0], path)
            return {'file': path}
        if node.op == 'drc.audit':
            from .drc import run_drc_audit
            unbound = {'before_files', 'label_files', 'slib_path', 'rules_path', 'root'}.intersection(params)
            if unbound:
                raise BridgeError('PLAN_UNBOUND_READ: DRC files must be bound through input roles')
            layers = [value for value, item in zip(bound, inputs) if item.role not in {'before', 'slib', 'rules'}]
            before = [value for value, item in zip(bound, inputs) if item.role == 'before']
            if before:
                params.setdefault('before_files', before)
            slib = next((item for item in inputs if item.role == 'slib'), None)
            if slib is not None:
                if ctx.plan.slib_sha256 is not None and slib.sha256 != ctx.plan.slib_sha256:
                    raise BridgeError('STALE_SOURCE: Slib fingerprint differs from the plan')
                params.setdefault('slib_path', str(slib.path))
            if rules := next((item for item in inputs if item.role == 'rules'), None):
                params['rules_path'] = str(rules.path)
            for value, item in zip(bound, inputs):
                value['role'] = item.role
            params.setdefault('reference_crs', ctx.plan.crs)
            params.setdefault('unit', ctx.plan.unit)
            params.setdefault('scale', ctx.plan.scale)
            params.setdefault('root', self.root)
            return {'report': run_drc_audit(layers, **params)}
        if node.op == 'labels.place' and len(paths) == 1:
            from .label_placement import place_labels
            params.setdefault('seed', ctx.plan.seed)
            report = place_labels(paths[0], inputs[0].sha256, **params)
            report.pop('runtime_ms')  # Timing is not part of the editable result.
            return {'report': report}
        if node.op == 'preview.render':
            from .visual_preview import render_overview
            data = [row for row, item in zip(bound, inputs) if item.role != 'drc_report']
            reports = [item.path for item in inputs if item.role == 'drc_report']
            if reports:
                params.setdefault('drc_report', _read_json(reports[0]))
            image = ctx.directory / _basename(params.pop('name', 'preview.png'))
            receipt = render_overview(data, image, **params)
            receipt['path'] = image.name
            return {'preview': image, 'report': receipt}
        if node.op == 'native.inspect' and len(paths) == 1:
            from .native_backend import native_file_info
            return {'report': native_file_info(self.root, str(paths[0]), inputs[0].sha256)}
        if node.op == 'file.patch':
            from .native_backend import prepare_native_operation
            report = next((item for item in inputs if item.role == 'patch_plan'), None)
            vector = next((item for item in inputs if item.role != 'patch_plan'), None)
            if vector is None:
                raise BridgeError('PLAN_INPUTS: file.patch requires a bound native vector')
            bridge = Bridge(self.root)
            identity = 'mp-' + _digest((ctx.plan.plan_id, node.id, ctx.cache_key))[:45]
            if report is not None:
                patch = _read_json(report.path)
                if patch['expected_sha256'] != vector.sha256:
                    raise BridgeError('STALE_SOURCE: label report is bound to a different WT')
                if not patch['patch_plan']:
                    return {'file': vector.path, 'report': {'source_sha256': vector.sha256, 'updates': 0}}
                if len(patch['patch_plan']) != 1 or patch['patch_plan'][0]['operation'] != 'point.move' or params:
                    raise BridgeError('PLAN_PARAMETERS: unsupported native label patch')
                prepare_native_operation(bridge, 'point.move', str(vector.path), vector.sha256,
                                         patch['patch_plan'][0]['parameters'], identity)
            else:
                bridge.prepare(str(vector.path), vector.sha256, request_id=identity, **params)
            receipt = bridge.apply(identity, ctx.publisher_epoch)
            return {'file': Path(receipt['output']), 'report': receipt}
        if node.op in BUILTINS:
            raise BridgeError(f'PLAN_INPUTS: invalid bindings for {node.op}')
        runner = self.operation_runner
        if runner is None:
            from .mcp_surface import _Operations
            from .server import build_server
            runner = _Operations(build_server(self.root, surface='legacy'), self.root)
            self.operation_runner = runner
        return runner.run_plan_node(node, inputs, ctx)

    def import_mpj(self, mpj_path):
        source = (self.root / mpj_path).resolve(strict=True)
        if source.suffix.lower() != '.mpj':
            raise BridgeError('PLAN_MPJ: expected a saved MPJ')
        digest = file_sha256(source)
        raw = source.read_bytes()
        native = None
        # A text interchange project is one ordered layer filename per line,
        # optionally preceded by its layer count.
        lines = []
        if b'\0' not in raw:
            try:
                contents = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                contents = raw.decode('gbk', errors='replace')
            lines = [line.strip().strip('"') for line in contents.splitlines() if line.strip() and not line.startswith('#')]
            if lines and lines[0].isdigit():
                count = int(lines.pop(0))
                if count != len(lines):
                    raise BridgeError('PLAN_MPJ: inconsistent text project layer count')
        if lines and all(Path(line).suffix.lower() in VECTORS | {'.msi'} for line in lines):
            layers = [{'file': str((source.parent / line).resolve(strict=True))} for line in lines]
        else:
            from .native_project import native_project_info
            native = native_project_info(self.root, str(source), digest)
            layers = native['layers']
        if file_sha256(source) != digest:
            raise BridgeError('STALE_DURING_PROJECT_READ')
        nodes, bindings, layer_frames = [], [], []
        for index, layer in enumerate(layers, 1):
            path = Path(layer['file'])
            fingerprint = layer['sha256'] if 'sha256' in layer else file_sha256(path)
            identity = f'layer-{index:03}'
            nodes.append({'id': identity, 'op': 'work.copy', 'inputs': [
                {'role': 'layer', 'source_file': str(path), 'expected_sha256': fingerprint}]})
            if path.suffix.lower() in VECTORS:
                bindings.append({'role': 'layer', 'from_node': identity, 'output_key': 'file'})
                layer_frames.append(read_layer({'file': str(path), 'expected_sha256': fingerprint}))
        if not layer_frames:
            raise BridgeError('PLAN_MPJ: no WT, WL or WP layers found')
        frame = layer_frames[0]
        crs = frame.crs.to_string() if frame.crs is not None else 'LOCAL'
        nodes.append({'id': 'audit', 'op': 'drc.audit', 'inputs': bindings})
        nodes.append({'id': 'preview', 'op': 'preview.render',
                      'inputs': [*bindings, {'role': 'drc_report', 'from_node': 'audit', 'output_key': 'report'}]})
        library = (native or {}).get('system_library') or {}
        return MapPlan.model_validate({'plan_id': 'mpj-' + digest[:20], 'description': source.name,
                                       'crs': crs, 'unit': frame_unit(frame.crs) or 'local',
                                       'scale': frame.scale or 1, 'slib_sha256': library.get('expected_manifest_sha256'),
                                       'nodes': nodes}).model_dump(mode='json')


def plan(spec, *, root='.'):
    return MapPlanEngine(root).plan(spec)


def diff(current_spec, previous_state_or_spec, *, root='.'):
    return MapPlanEngine(root).diff(current_spec, previous_state_or_spec)


def apply(spec, *, root='.', executor=None, fault_inject_node=None, expected_epoch=None):
    return MapPlanEngine(root).apply(spec, executor=executor, fault_inject_node=fault_inject_node,
                                    expected_epoch=expected_epoch)


def resume(plan_id, *, root='.', executor=None, expected_epoch=None):
    return MapPlanEngine(root).resume(plan_id, executor=executor, expected_epoch=expected_epoch)


def import_mpj(mpj_path, *, root='.'):
    return MapPlanEngine(root).import_mpj(mpj_path)
