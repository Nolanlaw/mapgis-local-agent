"""Exact-build native file calls in an owned, isolated legacy x86 process.

No desktop interaction, editor injection or arbitrary native function tool.
Candidates go through the existing Bridge ledger and explicit apply operation.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import io
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
from .core import Bridge, BridgeError, json_text, layout_signature, native_ids, read_native, sha, stale_attribute_headers
from .native_state import PROFILE
from .topology import validate_wp_topology, _wp_arc_records, _wp_nodes
from .desktop import PrivateDesktop
from .native_paths import NativePathAlias

KINDS = {'LINE': 0, 'POINT': 1, 'POLYGON': 2}


def _native_opcode(operation: str) -> str:
    return {'line.smooth':'line.replace', 'arc.smooth':'arc.replace', 'line.resize':'line.replace', 'line.extend_to':'line.replace'}.get(operation,operation)


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


def _stop_owned_process(process):
    if process.poll() is None:
        process.kill()
        process.wait()


@contextmanager
def _guard_owned_process(process):
    """Close an ephemeral job object to retire only this owned child on parent loss."""
    import win32job
    job = win32job.CreateJobObject(None, 'MapGISAgentWorker_' + uuid.uuid4().hex)
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        if process.poll() is None:
            try:
                win32job.AssignProcessToJobObject(job, int(process._handle))
            except Exception as exc:
                if process.poll() is None:
                    raise BridgeError('NATIVE_PROCESS_GUARD: cannot guard owned native child') from exc
        yield
    finally:
        job.Close()


def _run_child(args: list[str], folder: Path, log_name: str,*,mfc_windows:bool=False,
               guard_process:bool=False) -> tuple[dict, list[dict]]:
    stdout_file, stderr_file = folder / (log_name + '.stdout'), folder / (log_name + '.stderr')
    # Redirect to owned files: native fflush survives loss of the MCP parent.
    with (stdout_file.open('xb') as out, stderr_file.open('xb') as err,
          PrivateDesktop(mfc_windows=mfc_windows) as desktop, ExitStack() as lifecycle):
        process = subprocess.Popen(args, cwd=folder, stdout=out, stderr=err,
                                   creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=desktop.startupinfo,env=desktop.environment)
        lifecycle.callback(_stop_owned_process, process)
        if guard_process:
            lifecycle.enter_context(_guard_owned_process(process))
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
    record = {'pid': process.pid, 'returncode': process.returncode, 'timed_out': timed_out,'desktop':desktop.name,
              'stdout': stdout.decode('utf8', errors='replace'),
              'stderr': stderr.decode('utf8', errors='replace')}
    (folder / log_name).write_text(json_text(record), encoding='utf8')
    if timed_out or process.returncode:
        raise BridgeError(f'NATIVE_CHILD_FAILED: exit={process.returncode}, timeout={timed_out}; diagnostic {folder / log_name}')
    try:
        events = [json.loads(line) for line in record['stdout'].splitlines()]
        host = next(e for e in events if e.get('event') == 'host')
        if (host['bits'] != 32 or host['dep_flags'] != 0 or host['pid'] != process.pid
                or host.get('desktop')!=desktop.name or events[-1]['event'] != 'complete'):
            raise ValueError('worker receipt')
    except (ValueError, KeyError, StopIteration, IndexError) as exc:
        raise BridgeError('NATIVE_PROTOCOL: incomplete or invalid receipt') from exc
    return {'pid': process.pid, 'dep_flags': host['dep_flags'], 'desktop':desktop.name,
            'private_desktop':True,'log': str(folder / log_name)}, events


def _invoke(executable: Path, dll: Path, folder: Path, kind: str, operation: str,
            output: Path | None = None, log_name: str = 'invoke.json', *, guard_process: bool = False) -> dict:
    # The old DLL also corrupts WT/WL coordinate headers at long save paths,
    # not just persistent WP buffers. Keep every native data path short.
    with NativePathAlias(folder) as alias:
        if output is not None and output.parent.resolve() != folder.resolve():
            raise BridgeError('NATIVE_OUTPUT_FOLDER')
        args = [str(executable), _native_opcode(operation), str(dll),
                str(alias.path / ('input' + {0:'.wl',1:'.wt',2:'.wp'}[KINDS[kind]])),
                str(KINDS[kind]), str(alias.path / output.name) if output else '-',
                str(alias.path / 'updates.txt')]
        result, events = (_run_child(args, folder, log_name, guard_process=True)
                          if guard_process else _run_child(args, folder, log_name))
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
        try:
            result = _invoke(executable, dll, folder, reader.shapeType, 'inspect')
        except BridgeError as exc:
            diagnostics = bridge.root/'work/native-read-failures'
            diagnostics.mkdir(exist_ok=True)
            retained = diagnostics/(folder.name+'.json')
            record = folder/'invoke.json'
            retained.write_text(json_text({'file':str(path),'expected_sha256':expected_sha256,
                'error':str(exc),'native':json.loads(record.read_text(encoding='utf8')) if record.exists() else None}),encoding='utf8')
            raise BridgeError(f'NATIVE_READ_FAILED: retained diagnostic {retained}; {exc}') from exc
        if result['active'] != len(reader.geom):
            raise BridgeError('NATIVE_COUNT_MISMATCH: reader and BAS60 disagree')
        result.pop('log')
    if path.read_bytes() != blob:
        raise BridgeError('STALE_DURING_NATIVE_READ')
    return {**result, 'file': str(path), 'sha256': sha(blob), 'source_preserved': True,
            'warnings': [*result.get('warnings', []), *stale_attribute_headers(blob)],
            'mode': 'isolated_native_x86', 'profile': 'mapgis-6.7-051118-bas60-v1',
            'scope': 'saved file opened by BAS60 in a fresh process; no live editor state'}


def _parameters(operation: str, parameters: dict, reader, source: Path | None = None) -> dict:
    if operation == 'resave' and parameters == {}:
        return {}
    if operation=='feature.append':
        if source is None or not isinstance(parameters,dict) or set(parameters)!={'file','expected_sha256','record_ids'}:
            raise BridgeError('APPEND_PARAMETERS: source file, source expected_sha256 and source record_ids required')
        from .native_append import append_inputs
        incoming,*_=append_inputs(parameters['file'],parameters['expected_sha256'],parameters['record_ids'],
            {'name':'appended'+source.suffix.lower()},{'file':str(source),'expected_sha256':sha(source.read_bytes())})
        return {'file':str(incoming),'expected_sha256':parameters['expected_sha256'],'record_ids':list(parameters['record_ids'])}
    if operation=='point.style':
        from .point_styles import prepare_point_styles
        return prepare_point_styles(parameters,reader)
    if operation=='point.text':
        from .point_text import prepare_point_text
        return prepare_point_text(parameters,reader)
    if operation in {'line.smooth','arc.smooth'}:
        from .smoothing import prepare_smoothing
        result = prepare_smoothing(operation,parameters,reader,source)
        _parameters(_native_opcode(operation),{'updates':result['updates']},reader,source)
        return result
    if operation=='line.resize':
        from .line_editing import prepare_resize
        result=prepare_resize(parameters,reader)
        _parameters('line.replace',{'updates':result['updates']},reader,source)
        return result
    if operation=='line.extend_to':
        from .line_editing import prepare_extend_to
        result=prepare_extend_to(parameters,reader)
        _parameters('line.replace',{'updates':result['updates']},reader,source)
        return result
    if operation=='point.radius':
        if reader.crs and reader.crs.is_geographic:raise BridgeError('POINT_RADIUS_FRAME: projected or declared local drawing coordinates required')
        updates=parameters.get('updates') if isinstance(parameters,dict) else None
        if reader.shapeType!='POINT' or set(parameters)!={'updates'} or not isinstance(updates,list) or not 1<=len(updates)<=1000:raise BridgeError('POINT_RADIUS_UPDATES: explicit WT physical IDs and positive drawing-mm radii required')
        rows=dict(zip(native_ids(reader),reader.point_params['point_type']));seen=set()
        for update in updates:
            if not isinstance(update,dict) or set(update)!={'record_id','radius_mm'}:raise BridgeError('POINT_RADIUS_UPDATES')
            identity=update['record_id'];radius=update['radius_mm']
            if type(identity) is not int or identity not in rows or identity in seen or rows[identity] not in (2,3):raise BridgeError('POINT_RADIUS_IDS: unique active circle/arc physical IDs required')
            if type(radius) not in (int,float) or not math.isfinite(radius) or radius<=0:raise BridgeError('POINT_RADIUS_VALUE: positive finite drawing millimetres required')
            seen.add(identity)
        return {'updates':sorted(updates,key=lambda u:u['record_id'])}
    if operation == 'feature.delete':
        ids = parameters.get('record_ids') if isinstance(parameters, dict) else None
        if (reader.shapeType not in ('POINT', 'LINE', 'POLYGON') or set(parameters) != {'record_ids'}
                or not isinstance(ids, list) or not 1 <= len(ids) <= 1000
                or any(type(rid) is not int for rid in ids) or len(set(ids)) != len(ids)
                or not set(ids).issubset(native_ids(reader))):
            raise BridgeError('DELETE_IDS: 1..1000 unique active WT/WL/WP physical IDs required')
        return {'record_ids': sorted(ids)}
    if operation=='node.move':
        if source is None or reader.shapeType!='POLYGON' or not isinstance(parameters,dict) or set(parameters)!={'updates'}:
            raise BridgeError('NODE_UPDATES: saved WP and explicit node_id/x/y updates required')
        validate_wp_topology(source);arcs=_wp_arc_records(source)[3];nodes=_wp_nodes(arcs)
        updates=parameters['updates'];seen=set();targets={}
        if not isinstance(updates,list) or not 1<=len(updates)<=1000:raise BridgeError('NODE_UPDATES')
        for update in updates:
            if not isinstance(update,dict) or set(update)!={'node_id','x','y'}:raise BridgeError('NODE_UPDATES')
            identity=update['node_id']
            if type(identity) is not int or identity not in nodes or identity in seen:raise BridgeError('NODE_IDS: unique active endpoint identities required')
            if any(type(update[k]) not in (int,float) or not math.isfinite(update[k]) for k in ('x','y')):raise BridgeError('NODE_COORDINATES')
            seen.add(identity);targets[identity]=[update['x'],update['y']]
        locations={}
        for identity,node in nodes.items():
            locations.setdefault(tuple(targets.get(identity,node['coordinates'])),set()).add(identity)
        if any(len(locations[tuple(point)])>1 for point in targets.values()):
            raise BridgeError('NODE_COLLISION: merging distinct topology nodes is a separate operation')
        arc_updates=[];total=0
        for aid,arc in arcs.items():
            if not seen.intersection(arc['node_ids']):continue
            points=[list(p) for p in arc['coordinates']]
            for identity,index in zip(arc['node_ids'],(0,-1)):
                if identity in targets:points[index]=targets[identity]
            if all(point==points[0] for point in points[1:]):raise BridgeError('NODE_COLLAPSED_ARC')
            total+=len(points);arc_updates.append({'arc_id':aid,'coordinates':points})
        if len(arc_updates)>1000 or total>1000000:raise BridgeError('NODE_BATCH_LIMIT: at most 1000 incident arcs / 1000000 vertices per transaction')
        return {'updates':sorted(updates,key=lambda u:u['node_id']),'arc_updates':sorted(arc_updates,key=lambda u:u['arc_id'])}
    if operation in {'line.replace', 'arc.replace'}:
        is_arc = operation == 'arc.replace'
        arc_records = None
        if is_arc:
            if reader.shapeType != 'POLYGON' or source is None:
                raise BridgeError('ARC_UPDATES: owned WP and active arc IDs required')
            validate_wp_topology(source)
            arc_records = _wp_arc_records(source)[3]
        key = 'arc_id' if is_arc else 'record_id'
        updates = parameters.get('updates') if isinstance(parameters, dict) else None
        if (reader.shapeType != ('POLYGON' if is_arc else 'LINE') or set(parameters) != {'updates'}
                or not isinstance(updates, list) or not 1 <= len(updates) <= 1000):
            raise BridgeError('LINE_UPDATES: explicit active line IDs and complete 2D vertex lists required')
        active, seen, total = set(arc_records if is_arc else native_ids(reader)), set(), 0
        for update in updates:
            if not isinstance(update, dict) or set(update) != {key, 'coordinates'}:
                raise BridgeError('LINE_UPDATES: record_id and coordinates required')
            rid, points = update[key], update['coordinates']
            if type(rid) is not int or rid not in active or rid in seen:
                raise BridgeError('LINE_UPDATES: unique active physical IDs required')
            if (not isinstance(points, list) or not 2 <= len(points) <= 100000
                    or any(not isinstance(p, (list, tuple)) or len(p) != 2
                           or any(type(v) not in (int, float) or not math.isfinite(v) for v in p) for p in points)):
                raise BridgeError('LINE_UPDATES: 2..100000 finite XY vertices required')
            if not any(p != points[0] for p in points[1:]):
                raise BridgeError('LINE_UPDATES: collapsed line')
            if is_arc:
                old = arc_records[rid]['coordinates']
                if any(abs(a-b) > 4*max(math.ulp(a), math.ulp(b))
                       for p,q in ((points[0],old[0]),(points[-1],old[-1])) for a,b in zip(p,q)):
                    raise BridgeError('ARC_ENDPOINTS: preserve shared endpoints; node relocation is a separate operation')
            seen.add(rid)
            total += len(points)
        if total > 1000000:
            raise BridgeError('LINE_UPDATES: batch vertex limit is 1000000')
        return {'updates': sorted(updates, key=lambda u: u[key])}
    if operation != 'point.move':
        raise BridgeError('NATIVE_OPERATION: supported operations are resave, point.move, point.radius, point.style, point.text, line.replace, line.smooth, line.resize, line.extend_to, arc.replace, arc.smooth, node.move and feature.delete')
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


def _native_commands(operation: str, parameters: dict, source: Path) -> str:
    operation = _native_opcode(operation)
    if operation=='point.style':
        from .point_styles import native_commands
        return native_commands(parameters)
    if operation=='point.text':
        from .point_text import native_commands
        return native_commands(parameters)
    if operation=='node.move':return _native_commands('arc.replace',{'updates':parameters['arc_updates']},source)
    if operation=='point.radius':return ' '.join(f"{u['record_id']} {u['radius_mm']!r}" for u in parameters['updates'])
    if operation == 'feature.delete':
        return ' '.join(str(rid) for rid in parameters['record_ids'])
    if operation == 'resave':
        return ''
    # The exported raw API uses stored drawing coordinates. Its 2/3 argument
    # describes coordinate dimension, not map units. Reuse the pinned reader's
    # CRS decoder to invert the same factor used for independent readback.
    from pymapgis.reader import _read_crs
    _, factor = _read_crs(io.BytesIO(source.read_bytes()))
    if not math.isfinite(factor) or factor <= 0:
        raise BridgeError('NATIVE_COORDINATE_SCALE')
    if operation == 'point.move':
        return ' '.join(f"{u['record_id']} {u['x']/factor!r} {u['y']/factor!r}" for u in parameters['updates'])
    if operation in {'line.replace', 'arc.replace'}:
        key = 'arc_id' if operation == 'arc.replace' else 'record_id'
        return ' '.join(f"{u[key]} {len(u['coordinates'])} " +
            ' '.join(f'{x/factor!r} {y/factor!r}' for x,y in u['coordinates']) for u in parameters['updates'])
    raise BridgeError('NATIVE_OPERATION')


def _validate(source: Path, candidate: Path, operation: str, parameters: dict) -> tuple[bytes, dict]:
    operation = _native_opcode(operation)
    blob, reader, candidate_sections = read_native(candidate)
    comparison = compare_native_files(str(source), str(candidate), limit=1000)
    if reader.shapeType == 'POLYGON':
        comparison['native_topology'] = {'source':validate_wp_topology(source),
                                         'candidate':validate_wp_topology(candidate)}
        if operation in {'resave','arc.replace','node.move'}:
            from .attributes import _attribute_table
            before,_,source_sections=read_native(source);auxiliary={}
            for kind,index in [('arc',2),('node',5)]:
                original=_attribute_table(before,source_sections,index)
                if not original or original['count']<=1:continue
                actual=_attribute_table(blob,candidate_sections,index)
                if not actual or any(original[k]!=actual[k] for k in ('count','width','descriptors')):
                    raise BridgeError('NATIVE_AUXILIARY_ATTRIBUTES_CHANGED: '+kind+' schema')
                old,new=original['data'],actual['data'];length=original['count']*original['width']
                if before[old:old+length]!=blob[new:new+length]:raise BridgeError('NATIVE_AUXILIARY_ATTRIBUTES_CHANGED: '+kind+' records')
                auxiliary[kind]={'records_byte_identical':True,'stored_rows':original['count']-1}
            comparison['auxiliary_attributes']=auxiliary
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
    if operation in {'arc.replace','node.move'}:
        old_arcs, new_arcs = _wp_arc_records(source)[3], _wp_arc_records(candidate)[3]
        if set(old_arcs) != set(new_arcs):
            raise BridgeError('NATIVE_ARC_IDENTITY_CHANGED')
        updates = {u['arc_id']: u['coordinates'] for u in parameters['arc_updates' if operation=='node.move' else 'updates']}
        allowed = set()
        for aid, old in old_arcs.items():
            new = new_arcs[aid]
            if old['owner_region_ids'] != new['owner_region_ids'] or old['node_ids'] != new['node_ids'] or old['style'] != new['style']:
                raise BridgeError('NATIVE_ARC_METADATA_CHANGED')
            expected = updates.get(aid, old['coordinates'])
            if len(expected) != len(new['coordinates']) or any(abs(a-b) > 4*max(math.ulp(a), math.ulp(b))
                    for p,q in zip(new['coordinates'],expected) for a,b in zip(p,q)):
                raise BridgeError('NATIVE_ARC_GEOMETRY_READBACK')
            if aid in updates:
                allowed.update(old['owner_region_ids'])
        comparison['edited_arc_ids'] = sorted(updates)
        if operation=='node.move':
            old_nodes,new_nodes=_wp_nodes(old_arcs),_wp_nodes(new_arcs)
            expected_nodes={u['node_id']:[u['x'],u['y']] for u in parameters['updates']}
            if old_nodes.keys()!=new_nodes.keys():raise BridgeError('NATIVE_NODE_IDENTITY_CHANGED')
            for nid,old in old_nodes.items():
                actual=new_nodes[nid];expected=expected_nodes.get(nid,old['coordinates'])
                if old['incident_arc_ids']!=actual['incident_arc_ids'] or old['owner_region_ids']!=actual['owner_region_ids']:
                    raise BridgeError('NATIVE_NODE_TOPOLOGY_CHANGED')
                if any(abs(a-b)>4*max(math.ulp(a),math.ulp(b)) for a,b in zip(expected,actual['coordinates'])):
                    raise BridgeError('NATIVE_NODE_GEOMETRY_READBACK')
            comparison['edited_node_ids']=sorted(expected_nodes)
    else:
        allowed = {u['record_id'] for u in parameters.get('updates', [])}
    for change in comparison['changes']:
        if operation == 'feature.delete' and change['status'] == 'removed' and change['record_id'] in removed:
            continue
        if operation=='point.radius' and change['record_id'] in allowed and change['changed_fields']==['radius_mm'] and not change['attributes'] and change['geometry'] is None and change['text'] is None:continue
        if operation=='point.text' and change['record_id'] in allowed and change['changed_fields']==['text'] and not change['attributes'] and change['geometry'] is None:continue
        if operation=='point.style' and change['record_id'] in allowed and not change['attributes'] and change['geometry'] is None and change['text'] is None:
            requested=next(u['style'] for u in parameters['updates'] if u['record_id']==change['record_id'])
            if set(change['changed_fields'])<=set(requested):continue
        if operation not in {'point.move', 'line.replace', 'arc.replace','node.move'} or change['record_id'] not in allowed or change['changed_fields'] != ['geometry']:
            raise BridgeError('NATIVE_RESULT_DIFF: unrequested geometry, attributes, style or text changed')
    if comparison['counts']['changed'] > len(allowed):
        raise BridgeError('NATIVE_RESULT_DIFF: unrequested record changes')
    row_by_id = dict(zip(native_ids(reader), reader.geom))
    if operation=='point.style':
        from .point_styles import validate_saved_styles
        comparison.update(validate_saved_styles(source,candidate,parameters))
    if operation=='point.text':
        from .point_text import validate_saved_texts
        comparison.update(validate_saved_texts(source,candidate,parameters))
    if operation=='point.radius':
        from .comparison import _extract_style,_diff_dicts
        old_blob,old_reader,old_sections=read_native(source);old_rows={rid:i for i,rid in enumerate(native_ids(old_reader))};new_rows={rid:i for i,rid in enumerate(native_ids(reader))}
        for update in parameters['updates']:
            identity=update['record_id'];expected=_extract_style(old_reader,old_rows[identity])|{'radius_mm':update['radius_mm']}
            if _diff_dicts(expected,_extract_style(reader,new_rows[identity])):raise BridgeError('NATIVE_POINT_RADIUS_READBACK')
            x,y=struct.unpack_from('<2d',old_blob,old_sections[0][0]+identity*93+7);radius=update['radius_mm'];wanted=[x-radius,y-radius,x+radius,y+radius]
            actual=struct.unpack_from('<4d',blob,candidate_sections[3][0]+identity*32)
            if any(abs(a-b)>4*max(math.ulp(a),math.ulp(b)) for a,b in zip(actual,wanted)):raise BridgeError('NATIVE_POINT_RADIUS_BOUNDS')
        comparison['native_point_radius_and_bounds_verified']=True
    for update in ([] if operation in {'arc.replace','node.move','point.radius','point.style','point.text'} else parameters.get('updates', [])):
        geometry = row_by_id[update['record_id']]
        expected = update['coordinates'] if operation == 'line.replace' else [[update['x'], update['y']]]
        actual = list(geometry.coords)
        if len(expected) != len(actual) or any(abs(a-b) > 4*max(math.ulp(a), math.ulp(b))
                for p,q in zip(actual, expected) for a,b in zip(p,q)):
            raise BridgeError('NATIVE_GEOMETRY_READBACK: saved vertices differ')
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
    if operation=='feature.append':raise BridgeError('APPEND_ENTRY: use prepare_native_subset with target, or native_workspace edit feature.append')
    path = bridge.owned_path(file)
    request_id = bridge.request_id(request_id)
    before, reader, _ = read_native(path)
    if sha(before) != expected_sha256:
        raise BridgeError('STALE_SOURCE')
    parameters = _parameters(operation, parameters, reader, path)
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
        commands = _native_commands(operation, parameters, snapshot)
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


def completed_native_child(folder: Path,log_name: str):
    """Read one owned child's durable receipt; never restart its operation.

    None means the original child is still running within its original deadline.
    The PID is reconciled with creation time and executable before termination.
    """
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
                    return None
                # The original 30-second deadline still applies after parent loss.
                process.terminate();process.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            raise BridgeError('NATIVE_CHILD_IDENTITY_UNAVAILABLE: do not replay or terminate by name') from exc
    final_file=folder/log_name
    final={}
    if final_file.exists():
        final=json.loads(final_file.read_text(encoding='utf8'))
        if final.get('timed_out') or final.get('returncode')!=0:
            raise BridgeError('NATIVE_CHILD_FAILED: the producing child did not exit successfully')
        text=final['stdout']
    else:text=(folder/(log_name+'.stdout')).read_text(encoding='utf8')
    events=[json.loads(line) for line in text.splitlines()]
    if not events or events[-1].get('event')!='complete':
        raise BridgeError('NATIVE_CANDIDATE_INCOMPLETE: retain the snapshot and use a new request')
    host=next(e for e in events if e.get('event')=='host')
    if host.get('bits')!=32 or host.get('dep_flags')!=0 or (start_file.exists() and host.get('pid')!=started['pid']):
        raise BridgeError('NATIVE_PROTOCOL: recovered host receipt differs')
    return host|{'original_exit_code':final.get('returncode')},events


def reconcile_native_request(bridge: Bridge, request_id: str) -> dict:
    """Validate a durable candidate after an interrupted parent; never repeat an edit."""
    bridge.request_id(request_id)
    with bridge.connect() as db:
        row=db.execute('SELECT * FROM commands WHERE id=?',(request_id,)).fetchone()
    if row is None:raise BridgeError('Unknown request_id')
    receipt=bridge._receipt(row)
    if receipt.get('kind') not in ('NATIVE','NATIVE_PLOT'):
        raise BridgeError('NATIVE_REQUEST_REQUIRED')
    if row['state'] not in ('RUNNING','FAILED'):return bridge.status(request_id)
    folder=bridge.destination_path(receipt['stage'])
    log_name='native-plot.json' if receipt['kind']=='NATIVE_PLOT' else 'invoke.json'
    try:
        recovered=completed_native_child(folder,log_name)
        if recovered is None:return {**receipt,'state':row['state'],'native_child':'RUNNING','replayed':False}
        host,events=recovered
        from .native_paths import cleanup_aliases
        cleanup_aliases(folder)
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
            from .native_plot import validate_eps,_ghostscript,_render_check,normalize_native_ps
            if (folder/'source-manifest.json').read_bytes()!=row['before_blob']:raise BridgeError('PLOT_SNAPSHOT_CHANGED')
            drawn=[e for e in events if e.get('event')=='layer_drawn']
            layers=receipt.get('layers',receipt['sources'])
            if len(drawn)!=len(layers) or any(e.get('index')!=i or e.get('records')!=layers[i]['count'] for i,e in enumerate(drawn)):
                raise BridgeError('PLOT_INCOMPLETE_LAYERS')
            if any(sha(Path(x['file']).read_bytes())!=x['sha256'] for x in receipt['sources']):raise BridgeError('STALE_SOURCE')
            recovery=folder/('reconciled-'+recovery_id);recovery.mkdir()
            candidate=folder/'candidate.eps'
            if receipt.get('plot_protocol')=='native_level2_ps':
                postscript=folder/'candidate.ps';saved=[e for e in events if e.get('event')=='plot_saved']
                if len(saved)!=1 or saved[0]['sha256']!=sha(postscript.read_bytes()):raise BridgeError('PLOT_POSTSCRIPT_CHANGED')
                recovered=recovery/'candidate.eps'
                receipt['normalization']=normalize_native_ps(_ghostscript(bridge.root),recovery,postscript,recovered,receipt['view']['paper_mm'])
                # No DLL is called again. The durable native PS is interpreted
                # into a new validated candidate after an interrupted parent.
                candidate=recovered
            after=candidate.read_bytes()
            eps=validate_eps(candidate,receipt['view']['paper_mm'])
            pixels=_render_check(_ghostscript(bridge.root),recovery,candidate,receipt['view']['paper_mm'])
            if candidate.read_bytes()!=after:raise BridgeError('PLOT_CANDIDATE_CHANGED')
            changes=[]
            receipt.update(candidate_sha256=sha(after),eps=eps,render_check=pixels,source_preserved=True)
        receipt.setdefault('native_execution',{'pid':host['pid'],'dep_flags':host['dep_flags'],
            'original_exit_code':host['original_exit_code'],'completion_receipt_recovered':True,'log':str(folder/log_name)})
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
