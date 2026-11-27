"""Persistent receipts for background operations owned by a local process."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
import inspect
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator
from uuid import uuid4

import psutil

from .core import BridgeError
from .errors import classify_error


class JobState(StrEnum):
    QUEUED = 'QUEUED'
    RUNNING = 'RUNNING'
    SUCCEEDED = 'SUCCEEDED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    UNCERTAIN_NEEDS_RECONCILE = 'UNCERTAIN_NEEDS_RECONCILE'


class _Cancelled(Exception):
    pass


_RECEIPT_COLUMNS = ('job_id,op_id,request_id,state,progress,stage,created_at,updated_at,'
                    'result_json,error_json,cancel_requested,worker_pid')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class JobContext:
    """Progress and safe cancellation checkpoints for a context-aware callable."""

    def __init__(self, manager: JobManager, job_id: str):
        self._manager = manager
        self.job_id = job_id

    def update(self, progress: int, stage: str) -> None:
        if type(progress) is not int or not 0 <= progress <= 100 or not isinstance(stage, str) or not 1 <= len(stage) <= 160:
            raise BridgeError('INVALID_JOB_PROGRESS: expected 0..100 and a short stage')
        with self._manager._connect() as db:
            db.execute("UPDATE jobs SET progress=MAX(progress,?),stage=?,updated_at=? "
                       "WHERE job_id=? AND state='RUNNING'", (progress, stage, _now(), self.job_id))

    def checkpoint(self) -> None:
        """Call only at a point where stopping leaves a known-safe outcome."""
        with self._manager._connect() as db:
            row = db.execute('SELECT cancel_requested FROM jobs WHERE job_id=?', (self.job_id,)).fetchone()
        if row['cancel_requested']:
            raise _Cancelled()


class JobManager:
    def __init__(self, root: str | Path, *, max_workers: int = 2):
        self.db_path = Path(root).resolve() / 'evidence' / 'jobs.sqlite3'
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._pid = os.getpid()
        self._started = psutil.Process(self._pid).create_time()
        with self._connect() as db:
            states = ','.join(repr(state.value) for state in JobState)
            db.execute(f'''CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, op_id TEXT NOT NULL, request_id TEXT UNIQUE,
                state TEXT NOT NULL CHECK(state IN ({states})),
                progress INTEGER NOT NULL CHECK(progress BETWEEN 0 AND 100),
                stage TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result_json TEXT, error_json TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0, 1)),
                worker_pid INTEGER, worker_started_at REAL, fingerprint TEXT
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC, job_id DESC)')
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='mapgis-job')

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict:
        receipt = {key: row[key] for key in ('job_id', 'op_id', 'request_id', 'state', 'progress',
                                             'stage', 'created_at', 'updated_at', 'worker_pid')}
        receipt['cancel_requested'] = bool(row['cancel_requested'])
        receipt['result'] = json.loads(row['result_json']) if row['result_json'] is not None else None
        receipt['error'] = json.loads(row['error_json']) if row['error_json'] is not None else None
        return receipt

    def submit(self, op_id: str, fn: Callable[..., Any], *, request_id: str | None = None,
               fingerprint: str | None = None) -> dict:
        """Queue a zero-argument callable or a callable requiring one JobContext."""
        if not isinstance(op_id, str) or not op_id or not callable(fn):
            raise BridgeError('INVALID_TOOL_ARGUMENTS: expected an operation ID and callable')
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100):
            raise BridgeError('INVALID_TOOL_ARGUMENTS: invalid job request_id')
        job_id = uuid4().hex
        now = _now()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = (db.execute(f'SELECT {_RECEIPT_COLUMNS},fingerprint FROM jobs WHERE request_id=?',
                              (request_id,)).fetchone() if request_id else None)
            if old is not None:
                if (old['op_id'], old['fingerprint']) != (op_id, fingerprint):
                    raise BridgeError('JOB_REQUEST_CONFLICT: request_id is already bound')
                return self._receipt(old)
            db.execute('''INSERT INTO jobs(job_id,op_id,request_id,state,progress,stage,created_at,updated_at,
                                           worker_pid,worker_started_at,fingerprint)
                          VALUES (?,?,?,'QUEUED',0,'queued',?,?,?,?,?)''',
                       (job_id, op_id, request_id, now, now, self._pid, self._started, fingerprint))
        try:
            self._executor.submit(self._run, job_id, fn)
        except RuntimeError:
            with self._connect() as db:
                db.execute("UPDATE jobs SET state='CANCELLED',stage='interrupted_before_start',updated_at=?,"
                           "error_json=? WHERE job_id=? AND state='QUEUED'",
                           (_now(), _json(classify_error(BridgeError('JOB_INTERRUPTED'))), job_id))
        return self.status(job_id)

    def _run(self, job_id: str, fn: Callable[..., Any]) -> None:
        with self._connect() as db:
            claimed = db.execute("UPDATE jobs SET state='RUNNING',progress=1,stage='running',updated_at=? "
                                 "WHERE job_id=? AND state='QUEUED'", (_now(), job_id)).rowcount
        if not claimed:
            return
        context = JobContext(self, job_id)
        try:
            context.checkpoint()
            try:
                signature = inspect.signature(fn)
            except (TypeError, ValueError):
                result = fn()
            else:
                try:
                    signature.bind()
                except TypeError:
                    result = fn(context)
                else:
                    result = fn()
        except _Cancelled:
            self._finish(job_id, JobState.CANCELLED, 'cancelled')
        except Exception as exc:
            self._finish(job_id, JobState.FAILED, 'failed', error=classify_error(exc))
        except BaseException:
            self._finish(job_id, JobState.UNCERTAIN_NEEDS_RECONCILE, 'worker_interrupted',
                         error=classify_error(BridgeError('JOB_UNCERTAIN')))
        else:
            try:
                encoded = _json(result)
            except (TypeError, ValueError):
                self._finish(job_id, JobState.UNCERTAIN_NEEDS_RECONCILE, 'result_not_serializable',
                             error=classify_error(BridgeError('JOB_UNCERTAIN')))
            else:
                self._finish(job_id, JobState.SUCCEEDED, 'completed', result=encoded)

    def _finish(self, job_id: str, state: JobState, stage: str,
                *, result: str | None = None, error: dict | None = None) -> None:
        with self._connect() as db:
            db.execute('''UPDATE jobs SET state=?,stage=?,updated_at=?,result_json=?,error_json=?,
                          progress=CASE WHEN ?='SUCCEEDED' THEN 100 ELSE progress END
                          WHERE job_id=? AND state='RUNNING' ''',
                       (state, stage, _now(), result, _json(error) if error is not None else None, state, job_id))

    def status(self, job_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(f'SELECT {_RECEIPT_COLUMNS} FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise BridgeError('UNKNOWN_JOB')
        return self._receipt(row)

    def list_jobs(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or limit < 0:
            raise BridgeError('INVALID_LIMIT: limit must be a nonnegative integer')
        with self._connect() as db:
            rows = db.execute(f'SELECT {_RECEIPT_COLUMNS} FROM jobs ORDER BY created_at DESC,job_id DESC LIMIT ?', (limit,))
            return [self._receipt(row) for row in rows]

    def cancel(self, job_id: str) -> dict:
        with self._connect() as db:
            db.execute("UPDATE jobs SET cancel_requested=1,updated_at=?,"
                       "state=CASE WHEN state='QUEUED' THEN 'CANCELLED' ELSE state END,"
                       "stage=CASE WHEN state='QUEUED' THEN 'cancelled' ELSE stage END "
                       "WHERE job_id=? AND state IN ('QUEUED','RUNNING') AND cancel_requested=0", (_now(), job_id))
        return self.status(job_id)

    @staticmethod
    def _live_owner(pid: int | None, started: float | None) -> bool:
        if not pid or started is None:
            return False
        try:
            process = psutil.Process(pid)
            return process.create_time() == started and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except psutil.AccessDenied:
            return True
        except psutil.NoSuchProcess:
            return False

    def reconcile_startup(self) -> dict[str, int]:
        """Settle orphaned receipt states without invoking a submitted callable."""
        recovered = {JobState.CANCELLED.value: 0, JobState.UNCERTAIN_NEEDS_RECONCILE.value: 0}
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT job_id,state,worker_pid,worker_started_at FROM jobs "
                              "WHERE state IN ('QUEUED','RUNNING')").fetchall()
            for row in rows:
                if self._live_owner(row['worker_pid'], row['worker_started_at']):
                    continue
                if row['state'] == JobState.QUEUED:
                    state, stage, code = JobState.CANCELLED, 'interrupted_before_start', 'JOB_INTERRUPTED'
                else:
                    state, stage, code = JobState.UNCERTAIN_NEEDS_RECONCILE, 'reconcile_required', 'JOB_UNCERTAIN'
                db.execute('UPDATE jobs SET state=?,stage=?,updated_at=?,error_json=? WHERE job_id=?',
                           (state, stage, _now(), _json(classify_error(BridgeError(code))), row['job_id']))
                recovered[state] += 1
        return recovered

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
