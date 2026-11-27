"""Three execution policies for the packaged, file-bound MapGIS x86 worker."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil
from threading import Condition, RLock
from time import perf_counter
import tempfile
import uuid

import psutil

from .core import BridgeError, native_ids, read_native, sha
from .native_backend import (_guard_owned_process, _invoke, _native_commands, _native_opcode,
                             _parameters, _validate, backend_configuration)
from .object_store import file_sha256
from .workspace import _NativeBuffer


STRATEGIES = ('oneshot', 'sticky', 'warm_pool')
WORKSPACE_OPERATIONS = {
    'point.move', 'point.radius', 'point.style', 'point.text', 'line.replace',
    'line.smooth', 'line.resize', 'line.extend_to', 'arc.replace', 'arc.smooth', 'node.move', 'feature.delete',
}


@dataclass
class _Slot:
    key: tuple[Path, str]
    path: Path
    native: _NativeBuffer
    resources: ExitStack
    tasks: int = 0
    leased: bool = False

    @property
    def pid(self):
        return self.native.process.pid

    def rss_mb(self):
        return psutil.Process(self.pid).memory_info().rss / (1024 * 1024)


class NativeWorkerPool:
    """Lease immutable warm sessions by full path and hash.

    The workspace protocol binds one file for the lifetime of a child. Read-only
    requests can reuse that clean session. An edit batch retains its child only
    until the batch returns, then always retires it. Switching files selects a
    different child or starts a fresh one.
    """

    def __init__(self, root, work_dir, *, strategy='warm_pool', max_workers=2,
                 max_tasks_per_worker=100, max_rss_mb=256):
        if strategy not in STRATEGIES:
            raise BridgeError('NATIVE_POOL_STRATEGY: choose oneshot, sticky or warm_pool')
        if (type(max_workers) is not int or max_workers < 1 or type(max_tasks_per_worker) is not int
                or max_tasks_per_worker < 1 or type(max_rss_mb) not in (int, float)
                or not math.isfinite(max_rss_mb) or max_rss_mb <= 0):
            raise BridgeError('NATIVE_POOL_LIMITS: positive worker, task and memory limits required')
        self.root = Path(root).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.executable, self.dll = backend_configuration(self.root)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.strategy = strategy
        self.max_workers = 1 if strategy == 'sticky' else max_workers
        self.max_tasks_per_worker = max_tasks_per_worker
        self.max_rss_mb = max_rss_mb
        self._condition = Condition(RLock())
        self._slots = OrderedDict()
        self._active_oneshot = 0
        self._closed = False
        self.started_processes = []
        self.retirements = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _source(file, expected_sha256=None):
        path = Path(file).resolve(strict=True)
        blob, reader, _ = read_native(path)
        digest = sha(blob)
        if expected_sha256 is not None and digest != expected_sha256:
            raise BridgeError('STALE_SOURCE')
        return (path, digest), reader

    @staticmethod
    def _copy(source, target, digest):
        shutil.copyfile(source, target)
        if file_sha256(target) != digest:
            raise BridgeError('STALE_SOURCE: copied snapshot differs from the bound digest')

    def _start(self, key, reader):
        source, digest = key
        folder = Path(tempfile.mkdtemp(prefix='session-', dir=self.work_dir))
        path = folder / ('input' + source.suffix.lower())
        self._copy(source, path, digest)
        state = {'checkpoint': str(path), 'revision': 0, 'native_type': reader.shapeType}
        resources = ExitStack()
        try:
            native = _NativeBuffer(self.executable, self.dll, state, folder)
            resources.callback(native.close)
            # Close the native handle before the job, with job closure as a crash fallback.
            guard = resources.pop_all()
            try:
                resources.enter_context(_guard_owned_process(native.process))
            except Exception:
                guard.close()
                raise
            resources.callback(guard.close)
            if native.active != len(reader.geom):
                raise BridgeError('NATIVE_COUNT_MISMATCH: clean buffer and saved file disagree')
            slot = _Slot(key, path, native, resources)
            self.started_processes.append({'pid': slot.pid, 'start': native.started,
                                           'desktop': native.desktop.name, 'source_sha256': digest})
            return slot
        except BaseException:
            resources.close()
            raise

    def _retire(self, key, reason):
        slot = self._slots.pop(key)
        slot.resources.close()
        self.retirements.append({'pid': slot.pid, 'reason': reason, 'tasks': slot.tasks,
                                 'exit_code': slot.native.process.returncode})
        self._condition.notify_all()

    def _retire_reason(self, slot, *, from_idle=False):
        if slot.native.process.poll() is not None:
            return 'exited'
        if slot.tasks >= self.max_tasks_per_worker:
            return 'task_limit'
        if not from_idle or slot.tasks:
            try:
                if slot.rss_mb() > self.max_rss_mb:
                    return 'rss_limit'
            except psutil.NoSuchProcess:
                return 'exited'
        return None

    def _acquire(self, key, reader):
        with self._condition:
            while not self._closed:
                slot = self._slots.get(key)
                if slot is not None:
                    if slot.leased:
                        self._condition.wait()
                        continue
                    reason = self._retire_reason(slot, from_idle=True)
                    if reason is None:
                        self._slots.move_to_end(key)
                        slot.leased = True
                        return slot, True
                    self._retire(key, reason)
                if len(self._slots) >= self.max_workers:
                    evict = next((k for k, value in self._slots.items() if not value.leased), None)
                    if evict is None:
                        self._condition.wait()
                        continue
                    self._retire(evict, 'switch_file' if self.strategy == 'sticky' else 'capacity')
                slot = self._start(key, reader)
                slot.leased = True
                self._slots[key] = slot
                return slot, False
        raise BridgeError('NATIVE_POOL_CLOSED')

    @contextmanager
    def _cold_lease(self):
        with self._condition:
            while not self._closed and self._active_oneshot >= self.max_workers:
                self._condition.wait()
            if self._closed:
                raise BridgeError('NATIVE_POOL_CLOSED')
            self._active_oneshot += 1
        try:
            yield
        finally:
            with self._condition:
                self._active_oneshot -= 1
                self._condition.notify_all()

    def prewarm(self, files):
        if self.strategy != 'warm_pool':
            raise BridgeError('NATIVE_POOL_STRATEGY: prewarm is for warm_pool')
        frozen = [self._source(file) for file in files]
        if len({key for key, _ in frozen}) > self.max_workers:
            raise BridgeError('NATIVE_POOL_LIMITS: prewarm batch exceeds file-bound worker capacity')
        receipt = []
        for key, reader in frozen:
            started = perf_counter()
            slot, reused = self._acquire(key, reader)
            with self._condition:
                slot.leased = False
                self._condition.notify_all()
            receipt.append({'pid': slot.pid, 'sha256': key[1], 'reused': reused,
                            'startup_ms': 1000 * (perf_counter() - started)})
        return receipt

    def _run_cold(self, source, digest, reader, operations, folder):
        receipts = []
        current = source
        for index, (operation, params) in enumerate(operations):
            phase = folder / str(index)
            phase.mkdir()
            snapshot = phase / ('input' + source.suffix.lower())
            self._copy(current, snapshot, digest if current == source else file_sha256(current))
            kind = reader.shapeType
            if operation == 'inspect':
                host = _invoke(self.executable, self.dll, phase, kind, operation, guard_process=True)
                if host['active'] != len(reader.geom):
                    raise BridgeError('NATIVE_COUNT_MISMATCH')
            else:
                checked = _parameters(operation, params, reader, snapshot)
                (phase / 'updates.txt').write_text(_native_commands(operation, checked, snapshot), encoding='ascii')
                candidate = phase / ('result' + source.suffix.lower())
                host = _invoke(self.executable, self.dll, phase, kind, operation, candidate, guard_process=True)
                _validate(snapshot, candidate, operation, checked)
                _, reader, _ = read_native(candidate)
                current = candidate
            host.pop('log', None)
            receipts.append(host | {'operation': operation, 'job_guarded': True})
        return current, receipts

    def _run_buffer(self, slot, operations, reader):
        current, receipts = slot.path, []
        for operation, params in operations:
            if operation == 'inspect':
                response = slot.native.ask('status')
                if response['active'] != len(reader.geom):
                    raise BridgeError('NATIVE_COUNT_MISMATCH')
            else:
                checked = _parameters(operation, params, reader, current)
                token = uuid.uuid4().hex
                revision = slot.native.revision
                response = slot.native.ask(_native_opcode(operation), token, _native_commands(operation, checked, current))
                candidate = slot.path.parent / token / slot.path.name
                receipt = json.loads(candidate.with_name('native-complete.json').read_text(encoding='utf8'))
                if (response['revision'] != revision + 1 or receipt.get('checkpoint_complete') is not True
                        or receipt.get('revision') != revision + 1 or receipt.get('request') != token
                        or receipt.get('pid') != slot.pid or receipt.get('bits') != 32 or receipt.get('dep_flags') != 0):
                    raise BridgeError('WORKSPACE_INCOMPLETE_CHECKPOINT')
                _validate(current, candidate, operation, checked)
                _, reader, _ = read_native(candidate)
                if receipt['active'] != response['active'] or response['active'] != len(reader.geom):
                    raise BridgeError('NATIVE_COUNT_MISMATCH')
                current = candidate
            receipts.append({'pid': slot.pid, 'desktop': slot.native.desktop.name, 'private_desktop': True,
                             'dep_flags': 0, 'job_guarded': True, 'operation': operation,
                             'active': response['active'], 'revision': response['revision']})
        return current, receipts

    def run(self, file, operation='inspect', parameters=None, *, expected_sha256=None):
        return self.run_batch(file, [(operation, parameters or {})], expected_sha256=expected_sha256)

    def run_batch(self, file, operations, *, expected_sha256=None):
        allowed = WORKSPACE_OPERATIONS | {'inspect'} | ({'resave'} if self.strategy == 'oneshot' else set())
        operations = list(operations)
        if (not operations or any(op not in allowed or not isinstance(params, dict) or op == 'inspect' and params
                                  for op, params in operations)):
            raise BridgeError('NATIVE_POOL_OPERATION: use supported saved-file operations for this strategy')
        key, reader = self._source(file, expected_sha256)
        source, digest = key
        start = perf_counter()
        folder = Path(tempfile.mkdtemp(prefix='task-', dir=self.work_dir))
        changed = any(op != 'inspect' for op, _ in operations)
        reused, rss = False, None
        if self.strategy == 'oneshot':
            with self._cold_lease():
                current, receipts = self._run_cold(source, digest, reader, operations, folder)
        else:
            slot, reused = self._acquire(key, reader)
            try:
                current, receipts = self._run_buffer(slot, operations, reader)
                rss = slot.rss_mb()
            except BaseException:
                with self._condition:
                    self._retire(key, 'failed_batch')
                raise
            with self._condition:
                slot.tasks += 1
                reason = 'edited_batch_reset' if changed else self._retire_reason(slot)
                if reason is not None:
                    self._retire(key, reason)
                else:
                    slot.leased = False
                    self._condition.notify_all()
        # The result is an owned byte copy, never another task's live checkpoint.
        if file_sha256(source) != digest:
            raise BridgeError('STALE_DURING_NATIVE_READ')
        output = folder / ('result' + source.suffix.lower())
        if current != output:
            self._copy(current, output, file_sha256(current))
        output_blob, final_reader, _ = read_native(output)
        bounds = [float(v) for v in final_reader.bbox] if final_reader.geom else None
        return {'strategy': self.strategy, 'pid': receipts[-1]['pid'], 'reused_session': reused,
                'workspace_cross_file_rebind': False, 'rss_mb': rss, 'elapsed_ms': 1000 * (perf_counter() - start),
                'output': str(output), 'sha256': sha(output_blob), 'source_sha256': digest,
                'bounds': bounds, 'active': len(final_reader.geom), 'record_ids': native_ids(final_reader),
                'crs': (final_reader.crs.to_wkt() if hasattr(final_reader.crs, 'to_wkt')
                        else (str(final_reader.crs) if final_reader.crs is not None else None)),
                'native': receipts}

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            while self._active_oneshot or any(slot.leased for slot in self._slots.values()):
                self._condition.wait()
            for key in list(self._slots):
                self._retire(key, 'close')


WorkerPool = NativeWorkerPool
