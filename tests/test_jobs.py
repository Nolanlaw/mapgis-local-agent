import json
from pathlib import Path
import subprocess
import sys
from threading import Event

import pytest

from mapgis_agent.core import BridgeError
from mapgis_agent.jobs import JobManager


def test_progress_and_idempotent_result_can_be_polled_from_another_manager(tmp_path):
    manager = JobManager(tmp_path)
    observer = JobManager(tmp_path)
    running, release = Event(), Event()
    executed = []

    def work(context):
        executed.append(context.job_id)
        context.update(35, 'readback')
        running.set()
        assert release.wait(10)
        return {'verified': True}

    try:
        job = manager.submit('inspect', work, request_id='request', fingerprint='one')
        assert running.wait(5)
        job_id = job['job_id']
        assert observer.reconcile_startup() == {'CANCELLED': 0, 'UNCERTAIN_NEEDS_RECONCILE': 0}
        status = observer.status(job_id)
        assert (status['state'], status['progress'], status['stage']) == ('RUNNING', 35, 'readback')
        assert status['updated_at'] >= status['created_at']
        assert observer.submit('inspect', work, request_id='request', fingerprint='one')['job_id'] == job_id
        with pytest.raises(BridgeError, match='JOB_REQUEST_CONFLICT'):
            observer.submit('inspect', work, request_id='request', fingerprint='two')
    finally:
        release.set()
        manager.close()
        observer.close()
    final = observer.list_jobs(limit=1)[0]
    assert final['state'] == 'SUCCEEDED' and final['progress'] == 100 and final['result'] == {'verified': True}
    assert executed == [job_id]


def test_queued_and_running_cancellation_respect_safe_checkpoints(tmp_path):
    manager = JobManager(tmp_path, max_workers=1)
    running, release = Event(), Event()
    effects = []

    def work(context):
        running.set()
        assert release.wait(10)
        context.checkpoint()
        effects.append('running effect')

    try:
        first = manager.submit('run', work)
        assert running.wait(5)
        queued = manager.submit('queued', lambda: effects.append('queued effect'))
        assert queued['state'] == 'QUEUED'
        assert manager.cancel(queued['job_id'])['state'] == 'CANCELLED'
        cancelled = manager.cancel(first['job_id'])
        assert cancelled['state'] == 'RUNNING' and cancelled['cancel_requested']
    finally:
        release.set()
        manager.close()
    assert manager.status(first['job_id'])['state'] == 'CANCELLED'
    assert effects == []


def test_noncooperative_result_and_failure_remain_truthful(tmp_path):
    manager = JobManager(tmp_path)
    running, release = Event(), Event()

    def noncooperative():
        running.set()
        assert release.wait(10)
        return 4

    def fails():
        raise BridgeError('STALE_SOURCE: private-detail')

    try:
        first = manager.submit('work', noncooperative)
        assert running.wait(5)
        manager.cancel(first['job_id'])
        failed = manager.submit('check', fails)
    finally:
        release.set()
        manager.close()
    success = manager.status(first['job_id'])
    assert (success['state'], success['result'], success['cancel_requested']) == ('SUCCEEDED', 4, True)
    error = manager.status(failed['job_id'])
    assert error['state'] == 'FAILED' and error['error']['error_code'] == 'STALE_SOURCE'
    assert 'private-detail' not in json.dumps(error)


def test_restart_after_actual_worker_process_exit_records_uncertainty_without_replay(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'src'
    child = """
import os, sys
from threading import Event
sys.path.insert(0, sys.argv[1])
from mapgis_agent.jobs import JobManager
jobs = JobManager(sys.argv[2], max_workers=1)
exit_now = Event()
def work(context):
    context.update(50, 'native_operation')
    exit_now.wait()
    os._exit(17)
jobs.submit('running', work, request_id='old-running')
jobs.submit('queued', lambda: None, request_id='old-queued')
exit_now.set()
jobs.close()
"""
    run = subprocess.run([sys.executable, '-s', '-B', '-X', 'utf8', '-c', child, str(source), str(tmp_path)],
                         capture_output=True, text=True, timeout=20)
    assert run.returncode == 17, run.stderr
    restarted = JobManager(tmp_path)
    try:
        assert restarted.reconcile_startup() == {'CANCELLED': 1, 'UNCERTAIN_NEEDS_RECONCILE': 1}
        by_request = {job['request_id']: job for job in restarted.list_jobs()}
        running = by_request['old-running']
        assert running['state'] == 'UNCERTAIN_NEEDS_RECONCILE' and running['progress'] == 50
        assert running['error']['error_code'] == 'JOB_UNCERTAIN'
        assert by_request['old-queued']['state'] == 'CANCELLED'
        receipt = restarted.submit('running', lambda: pytest.fail('must never replay'), request_id='old-running')
        assert receipt == running and restarted.reconcile_startup()['UNCERTAIN_NEEDS_RECONCILE'] == 0
    finally:
        restarted.close()
