"""Exact-build native file calls in an owned, isolated legacy x86 process.

No desktop interaction, editor injection or arbitrary native function tool.
Candidates go through the existing Bridge ledger and explicit apply operation.
"""
from __future__ import annotations

import json
import math
import ctypes as ct
from ctypes import wintypes as wt
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import uuid

import psutil

from .comparison import compare_native_files
from .core import Bridge, BridgeError, json_text, layout_signature, native_ids, read_native, sha
from .native_state import PROFILE

KINDS = {'LINE': 0, 'POINT': 1, 'POLYGON': 2}


def backend_configuration(root: Path) -> tuple[Path, Path]:
    config_file = root / 'config.local.json'
    config = json.loads(config_file.read_text(encoding='utf8')) if config_file.exists() else {}
    if config.get('native_backend_legacy_dep_compatibility') is not True:
        raise BridgeError('LEGACY_HOST_DISABLED: enable only the isolated x86 host after accepting its legacy DEP compatibility; no system setting change is needed')
    if not config.get('mapgis_installation'):
        raise BridgeError('NATIVE_INSTALLATION_MISSING: configure the existing licensed MapGIS installation')
    dll = Path(config['mapgis_installation']).resolve() / 'program' / 'bas60.dll'
    if not dll.is_file() or sha(dll.read_bytes()) != PROFILE['bas60.dll']:
        raise BridgeError('NATIVE_BUILD_MISMATCH: this backend requires the verified BAS60 build')
    directory = Path(__file__).resolve().parent / 'bin'
    executable = directory / 'MapgisNativeWorker.exe'
    manifest = json.loads((directory / 'native-worker.json').read_text(encoding='utf8'))
    if not executable.is_file() or sha(executable.read_bytes()) != manifest['sha256']:
        raise BridgeError('NATIVE_WORKER_MISMATCH: rebuild or restore the packaged worker')
    binary = executable.read_bytes()
    pe = struct.unpack_from('<I', binary, 60)[0]
    if struct.unpack_from('<H', binary, pe + 4)[0] != 0x14c or struct.unpack_from('<H', binary, pe + 94)[0] & 0x100:
        raise BridgeError('NATIVE_WORKER_PROFILE: expected isolated x86 legacy host')
    return executable, dll


def _run_child(args: list[str], folder: Path, log_name: str) -> tuple[dict, list[dict]]:
    stdout_file, stderr_file = folder / (log_name + '.stdout'), folder / (log_name + '.stderr')
    # Redirect to owned files: native fflush survives loss of the MCP parent.
    with stdout_file.open('xb') as out, stderr_file.open('xb') as err:
        process = subprocess.Popen(args, cwd=folder, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        created, exited, kernel_time, user_time = (wt.FILETIME() for _ in range(4))
        kernel = ct.WinDLL('kernel32', use_last_error=True)
        kernel.GetProcessTimes.argtypes = [wt.HANDLE, *([ct.POINTER(wt.FILETIME)] * 4)]
        if not kernel.GetProcessTimes(wt.HANDLE(int(process._handle)), ct.byref(created),
                                      ct.byref(exited), ct.byref(kernel_time), ct.byref(user_time)):
            process.kill()
            process.wait()
            raise BridgeError('NATIVE_PROCESS_IDENTITY: cannot persist child creation time')
        started = {'pid': process.pid, 'process_start': ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 1e7 - 11644473600,
                   'executable': str(Path(args[0]).resolve()), 'stdout': str(stdout_file), 'stderr': str(stderr_file)}
        temporary = folder / (log_name + '.started.tmp')
        with temporary.open('xb') as stream:
            stream.write(json_text(started).encode('utf8')); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, folder / (log_name + '.started.json'))
        timed_out = False
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()  # This Popen child only; no name-based termination.
            process.wait()
    stdout, stderr = stdout_file.read_bytes(), stderr_file.read_bytes()
    record = {'pid': process.pid, 'returncode': process.returncode, 'timed_out': timed_out,
              'stdout': stdout.decode('utf8', errors='replace'),
              'stderr': stderr.decode('utf8', errors='replace')}
    (folder / log_name).write_text(json_text(record), encoding='utf8')
    if timed_out or process.returncode:
        raise BridgeError(f'NATIVE_CHILD_FAILED: exit={process.returncode}, timeout={timed_out}; diagnostic {folder / log_name}')
    try:
        events = [json.loads(line) for line in record['stdout'].splitlines()]
        host = next(e for e in events if e.get('event') == 'host')
        if host['bits'] != 32 or host['dep_flags'] != 0 or host['pid'] != process.pid or events[-1]['event'] != 'complete':
            raise ValueError('worker receipt')
    except (ValueError, KeyError, StopIteration, IndexError) as exc:
        raise BridgeError('NATIVE_PROTOCOL: incomplete or invalid receipt') from exc
    return {'pid': process.pid, 'dep_flags': host['dep_flags'], 'log': str(folder / log_name)}, events


def _invoke(executable: Path, dll: Path, folder: Path, kind: str, operation: str,
            output: Path | None = None, log_name: str = 'invoke.json') -> dict:
    args = [str(executable), operation, str(dll), str(folder / ('input' + {0:'.wl',1:'.wt',2:'.wp'}[KINDS[kind]])),
            str(KINDS[kind]), str(output) if output else '-', str(folder / 'updates.txt')]
    result, events = _run_child(args, folder, log_name)
    try:
        count = [e for e in events if e.get('event') == 'count'][-1]
        bounds = next((e['native_bounds'] for e in events if e.get('event') == 'bounds'), None)
        return {**result, 'native_type': kind, 'active': count['active'],
                'record_upper_bound': count['record_upper_bound'], 'native_bounds': bounds}
    except (IndexError, KeyError) as exc:
        raise BridgeError('NATIVE_PROTOCOL: missing native count') from exc


def native_file_info(root: str | Path, file: str, expected_sha256: str) -> dict:
    bridge = Bridge(root)
    path = Path(file).resolve(strict=True)
    blob, reader, _ = read_native(path)
    if sha(blob) != expected_sha256:
        raise BridgeError('STALE_SOURCE')
    executable, dll = backend_configuration(bridge.root)
    # TemporaryDirectory removes only the snapshot and logs it created here.
    with tempfile.TemporaryDirectory(prefix='native-read-', dir=bridge.root / 'work') as temporary:
        folder = Path(temporary)
        snapshot = folder / ('input' + path.suffix.lower())
        snapshot.write_bytes(blob)
        result = _invoke(executable, dll, folder, reader.shapeType, 'inspect')
        if result['active'] != len(reader.geom):
            raise BridgeError('NATIVE_COUNT_MISMATCH: reader and BAS60 disagree')
        result.pop('log')
    if path.read_bytes() != blob:
        raise BridgeError('STALE_DURING_NATIVE_READ')
    return {**result, 'file': str(path), 'sha256': sha(blob), 'source_preserved': True,
            'mode': 'isolated_native_x86', 'profile': 'mapgis-6.7-051118-bas60-v1',
            'scope': 'saved file opened by BAS60 in a fresh process; no live editor state'}


def _parameters(operation: str, parameters: dict, reader) -> dict:
    if operation == 'resave' and parameters == {}:
        return {}
    if operation == 'feature.delete':
        ids = parameters.get('record_ids') if isinstance(parameters, dict) else None
        if (reader.shapeType not in ('POINT', 'LINE') or set(parameters) != {'record_ids'}
                or not isinstance(ids, list) or not 1 <= len(ids) <= 1000
                or any(type(rid) is not int for rid in ids) or len(set(ids)) != len(ids)
                or not set(ids).issubset(native_ids(reader))):
            raise BridgeError('DELETE_IDS: 1..1000 unique active WT/WL physical IDs required')
        return {'record_ids': sorted(ids)}
    if operation != 'point.move':
        raise BridgeError('NATIVE_OPERATION: supported operations are resave, point.move and feature.delete')
    updates = parameters.get('updates') if isinstance(parameters, dict) else None
    if reader.shapeType != 'POINT' or set(parameters) != {'updates'} or not isinstance(updates, list) or not 1 <= len(updates) <= 1000:
        raise BridgeError('POINT_UPDATES: 1..1000 explicit active point IDs and finite x/y required')
    active, seen = set(native_ids(reader)), set()
    for update in updates:
        if not isinstance(update, dict) or set(update) != {'record_id', 'x', 'y'}:
            raise BridgeError('POINT_UPDATES: record_id, x and y required')
        rid = update['record_id']
        if type(rid) is not int or rid not in active or rid in seen:
            raise BridgeError('POINT_UPDATES: unique active physical IDs required')
        seen.add(rid)
        if any(type(update[k]) not in (int, float) or not math.isfinite(update[k]) for k in ('x', 'y')):
            raise BridgeError('POINT_UPDATES: finite numeric x/y required')
    return {'updates': sorted(updates, key=lambda u:u['record_id'])}


def _validate(source: Path, candidate: Path, operation: str, parameters: dict) -> tuple[bytes, dict]:
    blob, reader, _ = read_native(candidate)
    comparison = compare_native_files(str(source), str(candidate), limit=1000)
    (source.parent / 'validation-comparison.json').write_text(json_text(comparison), encoding='utf8')
    removed = set(parameters.get('record_ids', [])) if operation == 'feature.delete' else set()
    metadata = dict(comparison['metadata_changes'])
    if operation == 'feature.delete' and 'header_record_count' in metadata:
        count_change = metadata.pop('header_record_count')
        if count_change != {'before': comparison['before']['count'], 'after': comparison['after']['count']}:
            raise BridgeError('NATIVE_RESULT_DIFF: unexpected native active-count change')
    if (metadata or comparison['counts']['added']
            or set(comparison['removed_ids']) != removed):
        raise BridgeError('NATIVE_RESULT_DIFF: records or coordinate metadata changed unexpectedly')
    allowed = {u['record_id'] for u in parameters.get('updates', [])}
    for change in comparison['changes']:
        if operation == 'feature.delete' and change['status'] == 'removed' and change['record_id'] in removed:
            continue
        if operation != 'point.move' or change['record_id'] not in allowed or change['changed_fields'] != ['geometry']:
            raise BridgeError('NATIVE_RESULT_DIFF: unrequested geometry, attributes, style or text changed')
    if comparison['counts']['changed'] > len(allowed):
        raise BridgeError('NATIVE_RESULT_DIFF: unrequested record changes')
    row_by_id = dict(zip(native_ids(reader), reader.geom))
    for update in parameters.get('updates', []):
        geometry = row_by_id[update['record_id']]
        if geometry.x != update['x'] or geometry.y != update['y']:
            raise BridgeError('NATIVE_POINT_READBACK: saved coordinates differ')
    return blob, comparison


def _journal(before: bytes, after: bytes) -> list[dict]:
    if len(before) != len(after) or layout_signature(before) != layout_signature(after):
        return []  # Original BLOB remains available; structural rollback never guesses.
    changes, index = [], 0
    while index < len(before):
        if before[index] == after[index]:
            index += 1
            continue
        start = index
        while index < len(before) and before[index] != after[index]:
            index += 1
        changes.append({'offset': start, 'before': before[start:index].hex(), 'after': after[start:index].hex(),
                        'record_id': 0, 'group': 'native', 'field': 'native_saved_bytes'})
    return changes


def prepare_native_operation(bridge: Bridge, operation: str, file: str, expected_sha256: str,
                             parameters: dict, request_id: str) -> dict:
    path = bridge.owned_path(file)
    request_id = bridge.request_id(request_id)
    before, reader, _ = read_native(path)
    if sha(before) != expected_sha256:
        raise BridgeError('STALE_SOURCE')
    parameters = _parameters(operation, parameters, reader)
    payload = {'version': 1, 'kind': 'NATIVE', 'operation': operation, 'source': str(path),
               'sha256': expected_sha256, 'parameters': parameters}
    payload_hash = sha(json_text(payload).encode())
    with bridge.connect() as db:
        old = db.execute('SELECT * FROM commands WHERE id=?', (request_id,)).fetchone()
        if old:
            if old['payload_hash'] != payload_hash:
                raise BridgeError('REQUEST_ID_CONFLICT')
            return bridge._receipt(old)  # RUNNING/FAILED is reconciled, never blindly replayed.
    executable, dll = backend_configuration(bridge.root)
    folder = bridge.destination_path(bridge.root / 'work' / 'native-jobs' / sha(request_id.encode())[:24])
    output = bridge.destination_path(bridge.root / 'deliverables' / 'native' / request_id / path.name)
    receipt = {'id': request_id, 'kind': 'NATIVE', 'operation': operation, 'source': str(path),
               'source_sha256': expected_sha256, 'output': str(output), 'stage': str(folder),
               'mode': 'isolated_native_x86', 'parameters': parameters, 'profile': 'mapgis-6.7-051118-bas60-v1'}
    with bridge.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM commands WHERE id=?', (request_id,)).fetchone()
        if old:
            if old['payload_hash'] != payload_hash:
                raise BridgeError('REQUEST_ID_CONFLICT')
            return bridge._receipt(old)
        if folder.exists():
            raise BridgeError('NATIVE_STAGE_CONFLICT: inspect the previous staging directory')
        db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)',
                   (request_id,payload_hash,'RUNNING',str(path),before,b'', '[]',json_text(receipt)))
    try:
        folder.mkdir(parents=True)
        snapshot = folder / ('input' + path.suffix.lower())
        snapshot.write_bytes(before)
        commands = (''.join(f'{rid}\n' for rid in parameters['record_ids']) if operation == 'feature.delete' else
                    ''.join(f"{u['record_id']} {u['x']!r} {u['y']!r}\n" for u in parameters.get('updates', [])))
        (folder / 'updates.txt').write_text(commands,encoding='ascii')
        candidate = folder / ('candidate' + path.suffix.lower())
        execution = _invoke(executable, dll, folder, reader.shapeType, operation, candidate)
        after, comparison = _validate(snapshot, candidate, operation, parameters)
        # A different worker process opens the exact saved candidate bytes.
        reopen = folder / 'reopen'
        reopen.mkdir()
        (reopen / snapshot.name).write_bytes(after)
        reopened = _invoke(executable, dll, reopen, reader.shapeType, 'inspect')
        if execution['pid'] == reopened['pid'] or reopened['active'] != len(read_native(candidate)[1].geom):
            raise BridgeError('NATIVE_REOPEN_MISMATCH')
        if path.read_bytes() != before or snapshot.read_bytes() != before or candidate.read_bytes() != after:
            raise BridgeError('STALE_DURING_NATIVE_OPERATION')
        changes = _journal(before, after)
        receipt.update(candidate_sha256=sha(after), native_execution=execution, native_reopen=reopened,
                       comparison=comparison, changes=changes,
                       recovery='same_layout_byte_spans' if changes or before == after else 'preserved_source_snapshot',
                       rollback_source=str(snapshot), source_preserved=True)
        with bridge.connect() as db:
            db.execute("UPDATE commands SET state='PREPARED',after_blob=?,changes=?,receipt=? WHERE id=? AND state='RUNNING'",
                       (after,json_text(changes),json_text(receipt),request_id))
    except Exception as exc:
        receipt['failure'] = str(exc)
        with bridge.connect() as db:
            db.execute("UPDATE commands SET state='FAILED',receipt=? WHERE id=? AND state='RUNNING'",(json_text(receipt),request_id))
        raise
    return {**receipt, 'state': 'PREPARED'}


def reconcile_native_request(bridge: Bridge, request_id: str) -> dict:
    """Validate a durable candidate after an interrupted parent; never repeat an edit."""
    bridge.request_id(request_id)
    with bridge.connect() as db:
        row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row)
    if receipt.get('kind') not in ('NATIVE','NATIVE_PLOT'):
        raise BridgeError('NATIVE_REQUEST_REQUIRED')
    if row['state'] not in ('RUNNING','FAILED'):
        return bridge.status(request_id)
    folder=bridge.destination_path(receipt['stage'])
    log_name='native-plot.json' if receipt['kind']=='NATIVE_PLOT' else 'invoke.json'
    start_file=folder/(log_name+'.started.json')
    if start_file.exists():
        started=json.loads(start_file.read_text(encoding='utf8'))
        try:
            process=psutil.Process(started['pid'])
            owned=(abs(process.create_time()-started['process_start'])<0.00001
                   and Path(started['executable']).resolve()==Path(__file__).resolve().parent/'bin/MapgisNativeWorker.exe'
                   and Path(process.exe()).resolve()==Path(started['executable']).resolve())
            if owned and process.is_running():
                import time
                if time.time()-started['process_start']<=35:
                    return {**receipt,'state':row['state'],'native_child':'RUNNING','replayed':False}
                # The original 30-second deadline still applies after parent loss.
                process.terminate();process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            raise BridgeError('NATIVE_CHILD_IDENTITY_UNAVAILABLE: do not replay or terminate by name') from exc
    try:
        final_file=folder/log_name
        final={}
        if final_file.exists():
            final=json.loads(final_file.read_text(encoding='utf8'))
            text=final['stdout']
        else:
            text=(folder/(log_name+'.stdout')).read_text(encoding='utf8')
        events=[json.loads(line) for line in text.splitlines()]
        if not events or events[-1].get('event')!='complete':
            raise BridgeError('NATIVE_CANDIDATE_INCOMPLETE: retain the snapshot and use a new request')
        host=next(e for e in events if e.get('event')=='host')
        if host.get('bits')!=32 or host.get('dep_flags')!=0 or (start_file.exists() and host.get('pid')!=started['pid']):
            raise BridgeError('NATIVE_PROTOCOL: recovered host receipt differs')
        executable,dll=backend_configuration(bridge.root)
        recovery_id=uuid.uuid4().hex[:12]
        if receipt['kind']=='NATIVE':
            source=bridge.owned_path(receipt['source'])
            if source.read_bytes()!=row['before_blob']:raise BridgeError('STALE_SOURCE')
            snapshot=folder/('input'+source.suffix.lower())
            if snapshot.read_bytes()!=row['before_blob']:raise BridgeError('NATIVE_SNAPSHOT_CHANGED')
            candidate=folder/('candidate'+source.suffix.lower())
            after,comparison=_validate(snapshot,candidate,receipt['operation'],receipt['parameters'])
            _,reader,_=read_native(candidate)
            reopen=folder/('reconciled-'+recovery_id);reopen.mkdir()
            (reopen/snapshot.name).write_bytes(after)
            fresh=_invoke(executable,dll,reopen,reader.shapeType,'inspect')
            if fresh['active']!=len(reader.geom) or candidate.read_bytes()!=after:
                raise BridgeError('NATIVE_REOPEN_MISMATCH')
            changes=_journal(row['before_blob'],after)
            receipt.update(candidate_sha256=sha(after),comparison=comparison,native_reopen=fresh,
                           changes=changes,rollback_source=str(snapshot),source_preserved=True,
                           recovery='same_layout_byte_spans' if changes or after==row['before_blob'] else 'preserved_source_snapshot')
        else:
            from .native_plot import validate_eps,_ghostscript,_render_check
            if (folder/'source-manifest.json').read_bytes()!=row['before_blob']:raise BridgeError('PLOT_SNAPSHOT_CHANGED')
            drawn=[e for e in events if e.get('event')=='layer_drawn']
            if len(drawn)!=len(receipt['sources']) or any(e.get('index')!=i or e.get('records')!=receipt['sources'][i]['count'] for i,e in enumerate(drawn)):
                raise BridgeError('PLOT_INCOMPLETE_LAYERS')
            if any(sha(Path(x['file']).read_bytes())!=x['sha256'] for x in receipt['sources']):raise BridgeError('STALE_SOURCE')
            candidate=folder/'candidate.eps';after=candidate.read_bytes()
            recovery=folder/('reconciled-'+recovery_id);recovery.mkdir()
            eps=validate_eps(candidate,receipt['view']['paper_mm'])
            pixels=_render_check(_ghostscript(bridge.root),recovery,candidate,receipt['view']['paper_mm'])
            if candidate.read_bytes()!=after:raise BridgeError('PLOT_CANDIDATE_CHANGED')
            changes=[]
            receipt.update(candidate_sha256=sha(after),eps=eps,render_check=pixels,source_preserved=True)
        receipt.setdefault('native_execution',{'pid':host['pid'],'dep_flags':host['dep_flags'],
            'original_exit_code':final.get('returncode'),'completion_receipt_recovered':True,'log':str(final_file)})
        receipt.update(reconciled_after_interruption=True,replayed=False)
        receipt.pop('failure',None)
        with bridge.connect() as db:
            db.execute("UPDATE commands SET state='PREPARED',after_blob=?,changes=?,receipt=? WHERE id=? AND state IN ('RUNNING','FAILED')",
                       (after,json_text(changes),json_text(receipt),request_id))
        return {**receipt,'state':'PREPARED'}
    except Exception as exc:
        receipt['failure']=str(exc)
        with bridge.connect() as db:
            db.execute("UPDATE commands SET state='FAILED',receipt=? WHERE id=? AND state IN ('RUNNING','FAILED')",(json_text(receipt),request_id))
        raise
