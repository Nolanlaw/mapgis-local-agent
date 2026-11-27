"""Private, newly-created process controls; no attachment to existing windows.

The project-specific authorization gate belongs in the public adapter.
The helper never selects the input desktop or injects physical input.
"""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid


def encoded(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    # Some original ANSI controls expose invalid UTF-16 units in spare fields.
    # Retain those units as legal JSON escapes without losing valid Chinese
    # text or changing existing valid request fingerprints.
    return re.sub('[\ud800-\udfff]', lambda match: '\\u%04x' % ord(match.group()), text)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf8')).hexdigest()


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def display_text(value):
    """Keep malformed legacy UTF-16 units visible without breaking transports."""
    return re.sub('[\ud800-\udfff]', lambda match: '\\u%04x' % ord(match.group()), value)


def file_confirm_allowed(children, bindings):
    """OK on a file dialog needs a path bound by set_text(file).

    A common chooser may remember a non-task folder, so its own filename
    control must show the bound path. A MapGIS parameter dialog (e.g.
    离散数据网格化) echoes the path chosen in its save-as dialog in a Static;
    that text must equal a path bound earlier in this session."""
    chooser = any(n['class'].lower() in {'comboboxex32', 'shelldll_defview'} for n in children)
    if not chooser and not any('文件名' in n.get('text', '') for n in children):
        return True
    if any(n['hwnd'] in bindings and n.get('text') == bindings[n['hwnd']] for n in children):
        return True
    return not chooser and any(n['class'].lower() == 'static' and n.get('text') in bindings.values() for n in children)


def save(path, value):
    temporary = path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w', encoding='utf8') as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


class NativeUIClient:
    """File RPC to one short-lived helper; every action has a stable request ID."""
    def __init__(self, folder):
        self.folder = Path(folder).resolve(strict=True)

    @classmethod
    def start(cls, executable, expected_sha256, folder, *, idle_seconds=120, mapgis_environment=None):
        executable = Path(executable).resolve(strict=True)
        if file_sha(executable) != expected_sha256:
            raise ValueError('PRIVATE_UI_BUILD_MISMATCH')
        if type(idle_seconds) is not int or not 15 <= idle_seconds <= 900:
            raise ValueError('PRIVATE_UI_IDLE_LIMIT')
        folder = Path(folder).resolve(); folder.mkdir()
        (folder/'requests').mkdir(); (folder/'files').mkdir()
        spec = {'schema': 1, 'executable': str(executable), 'sha256': expected_sha256,
            'idle_seconds': idle_seconds, 'session': uuid.uuid4().hex,
            'mapgis_environment': mapgis_environment,
            'helper_script':str(Path(__file__).resolve()),'helper_sha256':file_sha(__file__)}
        save(folder/'spec.json', spec)
        with (folder/'helper.stdout').open('xb') as out, (folder/'helper.stderr').open('xb') as err:
            process = subprocess.Popen([sys.executable, '-s', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--child', str(folder)],
                # File RPC never consumes stdin. Inheriting an MCP named pipe
                # can block Windows Python while it initializes standard input.
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
        import psutil
        save(folder/'launcher.json', {'pid': process.pid, 'start': psutil.Process(process.pid).create_time(), 'executable': str(Path(sys.executable).resolve())})
        client = cls(folder); deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            status = client.status()
            if status.get('state') in {'READY', 'FAILED', 'CLOSED'}:
                if status['state'] != 'READY':
                    raise RuntimeError('PRIVATE_UI_START_FAILED: '+str(status))
                return client
            if process.poll() is not None:
                raise RuntimeError('PRIVATE_UI_HELPER_EXIT: '+str(process.returncode))
            time.sleep(.05)
        raise RuntimeError('PRIVATE_UI_START_PENDING: reconcile '+str(folder))

    def status(self):
        path = self.folder/'status.json'
        result=json.loads(path.read_text(encoding='utf8')) if path.is_file() else {'state':'STARTING'}
        launcher=self.folder/'launcher.json'
        if launcher.is_file():result['launcher']=json.loads(launcher.read_text(encoding='utf8'))
        return result

    def request(self, request_id, action, *, token=None, parameters=None, timeout=12):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', request_id):
            raise ValueError('PRIVATE_UI_REQUEST_ID')
        body = {'id': request_id, 'action': action, 'token': token, 'parameters': parameters or {}}
        key = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        path = self.folder/'requests'/(key+'.json'); response = self.folder/'requests'/(key+'.response.json')
        if path.exists():
            if json.loads(path.read_text(encoding='utf8')) != body:
                raise ValueError('PRIVATE_UI_REQUEST_CONFLICT')
        else:
            incoming = path.with_name(path.stem+'.incoming-'+uuid.uuid4().hex)
            with incoming.open('x', encoding='utf8') as stream:
                stream.write(encoded(body)); stream.flush(); os.fsync(stream.fileno())
            try:
                os.link(incoming, path)  # Publish a complete request without overwriting another writer.
            except FileExistsError:
                if json.loads(path.read_text(encoding='utf8')) != body:
                    raise ValueError('PRIVATE_UI_REQUEST_CONFLICT')
            finally:
                incoming.unlink()
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if response.exists():
                value = json.loads(response.read_text(encoding='utf8'))
                if value['request_sha256'] != digest(body):
                    raise ValueError('PRIVATE_UI_RESPONSE_IDENTITY')
                return value
            state = self.status()
            if state.get('state') in {'CLOSED', 'FAILED'}:
                raise RuntimeError('PRIVATE_UI_ENDED_WITHOUT_REPLY: '+str(state))
            time.sleep(.05)
        raise RuntimeError('PRIVATE_UI_REQUEST_PENDING: do not resend with a new ID; reconcile '+str(path))

    def observe(self, request_id):
        return self.request(request_id, 'observe')

    def close(self, request_id, *, discard=False):
        aborted = self.folder/('discard-'+hashlib.sha256(request_id.encode()).hexdigest()[:32]+'.json')
        if discard and aborted.is_file():
            prior = json.loads(aborted.read_text(encoding='utf8'))
            if prior.get('state') == 'CLOSED': return prior
        try:
            result = self.request(request_id, 'close', parameters={'discard': discard})
        except RuntimeError as exc:
            if not discard: raise
            result = {'state':'UNKNOWN','error':str(exc)}
        if not discard or result.get('state') == 'CLOSED': return result
        # Explicit discard may recover a nonresponsive owned GUI without asking
        # that GUI to enumerate controls. Never attach to/kill a named app.
        import psutil
        status = self.status(); spec = json.loads((self.folder/'spec.json').read_text(encoding='utf8'))
        def matches(info):
            if not info: return False
            try:
                proc=psutil.Process(info['pid'])
                if abs(proc.create_time()-info['start'])>=1e-5: return False
                if not os.path.samefile(proc.exe(),info['executable']): raise ValueError('PRIVATE_UI_DISCARD_EXECUTABLE')
                return True
            except psutil.NoSuchProcess:return False
        app = status.get('application'); helper = status.get('helper'); launcher=status.get('launcher')
        startup_helpers=[]
        if not app and not helper and matches(launcher):
            script=Path(spec.get('helper_script',''))
            if not script.is_file()or file_sha(script)!=spec.get('helper_sha256'):
                raise ValueError('PRIVATE_UI_STARTUP_HELPER_BUILD')
            owner=psutil.Process(launcher['pid'])
            for candidate in [owner,*owner.children(recursive=True)]:
                try:
                    argv=candidate.cmdline()
                    if len(argv)<3 or argv[-2]!='--child' or Path(argv[-1]).resolve()!=self.folder:continue
                    if Path(argv[-3]).resolve()!=script.resolve()or candidate.create_time()<launcher['start']-1e-5:
                        raise ValueError('PRIVATE_UI_STARTUP_HELPER_IDENTITY')
                    startup_helpers.append({'pid':candidate.pid,'start':candidate.create_time(),'executable':candidate.exe()})
                except psutil.NoSuchProcess:continue
            if not any(x['pid']==launcher['pid']for x in startup_helpers):
                raise ValueError('PRIVATE_UI_STARTUP_LAUNCHER_IDENTITY')
            save(aborted,{'state':'DISCARD_REQUESTED','request_id':request_id,'startup_helpers':startup_helpers,'normal_close':result})
            # Only this verified Python launcher/helper chain. Killing its
            # helper also closes any not-yet-published KILL_ON_JOB_CLOSE handle.
            for info in reversed(startup_helpers):
                if matches(info):psutil.Process(info['pid']).terminate()
        if matches(helper):
            argv = psutil.Process(helper['pid']).cmdline()
            if len(argv)<3 or argv[-2]!='--child' or Path(argv[-1]).resolve()!=self.folder:
                raise ValueError('PRIVATE_UI_DISCARD_HELPER_IDENTITY')
        if matches(app):
            if file_sha(app['executable']) != spec['sha256']: raise ValueError('PRIVATE_UI_DISCARD_BUILD')
            kernel=ct.WinDLL('kernel32',use_last_error=True)
            kernel.OpenJobObjectW.argtypes=[wt.DWORD,wt.BOOL,wt.LPCWSTR];kernel.OpenJobObjectW.restype=wt.HANDLE
            kernel.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD];kernel.OpenProcess.restype=wt.HANDLE
            kernel.IsProcessInJob.argtypes=[wt.HANDLE,wt.HANDLE,ct.POINTER(wt.BOOL)];kernel.IsProcessInJob.restype=wt.BOOL
            kernel.TerminateJobObject.argtypes=[wt.HANDLE,wt.UINT];kernel.TerminateJobObject.restype=wt.BOOL
            kernel.CloseHandle.argtypes=[wt.HANDLE]
            job=kernel.OpenJobObjectW(0xC,False,'MapGISNativeUI_'+spec['session'])
            process=kernel.OpenProcess(0x1000,False,app['pid'])
            try:
                owned=wt.BOOL()
                if not job or not process or not kernel.IsProcessInJob(process,job,ct.byref(owned)) or not owned.value:
                    raise ValueError('PRIVATE_UI_DISCARD_JOB_IDENTITY')
                save(aborted,{'state':'DISCARD_REQUESTED','request_id':request_id,'application':app,'helper':helper,'normal_close':result})
                if not kernel.TerminateJobObject(job,0): raise RuntimeError('PRIVATE_UI_DISCARD_JOB_FAILED')
            finally:
                if process:kernel.CloseHandle(process)
                if job:kernel.CloseHandle(job)
        deadline=time.monotonic()+8
        def still_running():return matches(app)or matches(helper)or matches(launcher)or any(matches(info)for info in startup_helpers)
        while still_running()and time.monotonic()<deadline:time.sleep(.05)
        result={'state':'CLOSE_PENDING' if still_running()else 'CLOSED',
            'request_id':request_id,'forced_discard':True,'normal_close':result,
            'processes_alive':{'application':matches(app),'helper':matches(helper),'launcher':matches(launcher)},
            'startup_helpers':startup_helpers}
        save(aborted,result)
        if result['state']=='CLOSED':
            save(self.folder/'status.json',status|{'state':'CLOSED','reason':'explicit_owned_discard',
                'startup_helpers':startup_helpers})
        return result


def _stage_mapgis_environment(folder, executable, host, alias, configuration):
    if set(configuration) != {'system_library','color_library'}:
        raise ValueError('PRIVATE_UI_ENVIRONMENT_CONFIGURATION')
    environment_source = executable.parent/'MCONFIG.GIS'
    original_environment = environment_source.read_bytes()
    if len(original_environment) != 648 or original_environment[:6] != b'GISENV' or int.from_bytes(original_environment[6:8],'little') < 0x4405:
        raise ValueError('PRIVATE_UI_ENVIRONMENT_FORMAT')
    environment_data = bytearray(original_environment)
    copied_libraries = []
    for name, source in [('system',configuration['system_library']),('color',configuration['color_library'])]:
        source = Path(source).resolve(strict=True)
        target = folder/'files/libraries'/name
        members = sorted(source.rglob('*'))
        files = [p for p in members if p.is_file()]
        if any(p.is_symlink() or p.is_junction() for p in members) or len(files)>10000 or sum(p.stat().st_size for p in files)>64*1024**2:
            raise ValueError('PRIVATE_UI_LIBRARY_COPY_LIMIT')
        for member in files:
            before = file_sha(member); destination = target/member.relative_to(source)
            destination.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(member,destination)
            if file_sha(destination) != before or file_sha(member) != before:
                raise ValueError('PRIVATE_UI_LIBRARY_SOURCE_CHANGED')
            copied_libraries.append({'source':str(member),'relative':str(destination.relative_to(folder/'files')),'sha256':before})
    for name in ('inputs','temp'):
        (folder/'files'/name).mkdir(exist_ok=True)
    slots = {0:alias.path/'libraries/color', 1:alias.path/'host', 2:alias.path/'inputs',
        3:alias.path/'temp', 4:alias.path/'libraries/system'}
    for slot, value in slots.items():
        raw = str(value).encode('gbk',errors='strict')
        if len(raw)>=128: raise ValueError('PRIVATE_UI_ENVIRONMENT_PATH_LENGTH')
        start = 8+128*slot
        environment_data[start:start+128] = raw+b'\0'*(128-len(raw))
    (host/'MCONFIG.GIS').write_bytes(environment_data)
    environment_record = {'source':str(environment_source),'source_sha256':hashlib.sha256(original_environment).hexdigest(),
        'copy':str(host/'MCONFIG.GIS'),'copy_sha256':hashlib.sha256(environment_data).hexdigest(),
        'libraries':copied_libraries,'installed_environment_modified':False}
    # Legacy program-relative data loads also require an ASCII program
    # path. Snapshot local runtime DLLs and lookup tables, so future
    # library/palette edits cannot write to the installation.
    environment_record['runtime_copies'] = []
    known={'basroot.dll':'d1109d09d3708911a68a490fc86d64e8fbf30b3f53a7ffce8de2241ec5cc36ba',
        'bas60.dll':'680029d3ed7a04338a6799f1e16cab30d6e421d13e3d8c66bbeccb057a50ee24'}
    runtime_files=[p for p in executable.parent.iterdir()if p.is_file()and p.suffix.lower()in {'.dll','.ocx'}]
    for branch in ('lut','HeightLut'):
        runtime_files.extend(p for p in (executable.parent/branch).rglob('*')if p.is_file())
    if len(runtime_files)>1000 or sum(p.stat().st_size for p in runtime_files)>128*1024**2:
        raise ValueError('PRIVATE_UI_PROGRAM_COPY_LIMIT')
    for source in runtime_files:
        if source.is_symlink() or source.is_junction():raise ValueError('PRIVATE_UI_PROGRAM_COPY_LINK')
        expected=file_sha(source)
        if source.name.lower()in known and known[source.name.lower()]!=expected:
            raise ValueError('PRIVATE_UI_ENVIRONMENT_RUNTIME_BUILD')
        destination=host/source.relative_to(executable.parent);destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,destination)
        if file_sha(destination)!=expected or file_sha(source)!=expected:
            raise ValueError('PRIVATE_UI_ENVIRONMENT_RUNTIME_COPY')
        environment_record['runtime_copies'].append({'source':str(source),'copy':str(destination),'sha256':expected})
    save(folder/'environment-copy.json',environment_record)


def run_child(folder):
    # Avoid a BLAS pool in this owned helper; the path adapter is imported only
    # after binding its UI thread to its own private desktop.
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
    import win32api, win32con, win32event, win32gui, win32job, win32process
    import psutil
    spec = json.loads((folder/'spec.json').read_text(encoding='utf8'))
    executable = Path(spec['executable']).resolve(strict=True)
    if spec['schema'] != 1 or file_sha(executable) != spec['sha256']:
        raise ValueError('PRIVATE_UI_SPEC_BUILD')
    user = ct.WinDLL('user32', use_last_error=True)
    user.CreateDesktopW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ct.c_void_p, wt.DWORD, wt.DWORD, ct.c_void_p]
    user.CreateDesktopW.restype = wt.HANDLE
    user.CloseDesktop.argtypes = [wt.HANDLE]; user.CloseDesktop.restype = wt.BOOL
    user.SetThreadDesktop.argtypes = [wt.HANDLE]; user.SetThreadDesktop.restype = wt.BOOL
    user.GetThreadDesktop.argtypes = [wt.DWORD]; user.GetThreadDesktop.restype = wt.HANDLE
    user.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ct.POINTER(ct.c_size_t)]
    user.SendMessageTimeoutW.restype = ct.c_size_t
    callback_type = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user.EnumDesktopWindows.argtypes = [wt.HANDLE, callback_type, wt.LPARAM]
    user.EnumDesktopWindows.restype = wt.BOOL
    kernel = ct.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentThreadId.restype = wt.DWORD
    kernel.SetInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD]
    kernel.SetInformationJobObject.restype = wt.BOOL
    old_desktop = user.GetThreadDesktop(kernel.GetCurrentThreadId())
    desktop_name = 'MapGISAgent_'+uuid.uuid4().hex
    desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0xCF, None)
    if not desktop: raise RuntimeError('PRIVATE_UI_DESKTOP_CREATE: '+str(ct.get_last_error()))
    job = process = thread = alias = None
    identity = {}
    sequence = 0
    token = observed = None
    exit_requested = False
    idle_deadline = time.monotonic()+spec['idle_seconds']
    try:
        if not user.SetThreadDesktop(desktop): raise RuntimeError('PRIVATE_UI_THREAD_BIND')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from mapgis_agent.native_paths import NativePathAlias
        from mapgis_agent._ui_dispatch import ActionDispatcher, WindowObservation
        alias = NativePathAlias(folder/'files', folder)
        # rt_LoadEnv loads MCONFIG.GIS beside the application executable. Use a
        # byte-identical executable copy and its own environment file, rather
        # than changing the installed program's environment. Dependent DLLs
        # remain loaded through the child-only program search path.
        host = folder/'files/host'; host.mkdir()
        launch_executable = host/executable.name
        shutil.copyfile(executable, launch_executable)
        if file_sha(launch_executable) != spec['sha256']:
            raise ValueError('PRIVATE_UI_LAUNCH_COPY_MISMATCH')
        configuration = spec.get('mapgis_environment')
        if configuration is not None:
            _stage_mapgis_environment(folder, executable, host, alias, configuration)
        job = win32job.CreateJobObject(None, 'MapGISNativeUI_'+spec['session'])
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        # No clipboard, host USER handles, desktop switching, system/display
        # changes or global-atom communication by this newly owned app job.
        restrictions = wt.DWORD(0xFF)
        if not kernel.SetInformationJobObject(int(job), 4, ct.byref(restrictions), ct.sizeof(restrictions)):
            raise RuntimeError('PRIVATE_UI_JOB_UI_RESTRICTIONS: '+str(ct.get_last_error()))
        startup = win32process.STARTUPINFO(); startup.lpDesktop = 'WinSta0\\'+desktop_name
        # Start with normal window state inside this private desktop.
        # The job cannot switch desktops; no user-input desktop is selected.
        startup.dwFlags = win32con.STARTF_USESHOWWINDOW | 0x80; startup.wShowWindow = win32con.SW_SHOWNORMAL
        child_environment = dict(os.environ)
        child_environment['PATH'] = str(executable.parent)+';'+child_environment.get('PATH','')
        launch_path = alias.path/'host'/executable.name
        process, thread, pid, tid = win32process.CreateProcess(str(launch_path), None, None, None, False,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW, child_environment, str(alias.path), startup)
        win32job.AssignProcessToJobObject(job, process); win32process.ResumeThread(thread)
        helper = psutil.Process()
        identity = {'helper': {'pid': helper.pid, 'start': helper.create_time(), 'executable': helper.exe()},
            'application': {'pid': pid, 'start': psutil.Process(pid).create_time(), 'executable': str(launch_executable),
                'source_executable':str(executable),'sha256': spec['sha256']},
            'desktop': desktop_name, 'private_desktop': True, 'desktop_input_used': False, 'ui_restrictions': restrictions.value,
            'files_alias': str(alias.path), 'folder': str(folder)}
        observation = WindowObservation(user, desktop, callback_type, pid, process, display_text)
        dispatcher = ActionDispatcher(observation, folder, alias, identity, desktop_name, job,
                                      'MapGISNativeUI_'+spec['session'], save, file_confirm_allowed)
        alive, guard, snapshot = observation.alive, observation.guard, observation.snapshot
        prepare_action = dispatcher.prepare
        save(folder/'status.json', identity | {'state': 'READY', 'sequence': sequence})
        while alive() and not exit_requested and time.monotonic() < idle_deadline:
            for request in sorted((folder/'requests').glob('*.json')):
                if request.name.endswith('.response.json') or request.name.endswith('.intent.json'): continue
                response = request.with_name(request.stem+'.response.json')
                if response.exists(): continue
                intent = request.with_name(request.stem+'.intent.json')
                body = json.loads(request.read_text(encoding='utf8')); fingerprint = digest(body)
                result = {'request_sha256': fingerprint, 'request_id': body.get('id'), 'sequence': sequence}
                try:
                    if intent.exists(): raise RuntimeError('PRIVATE_UI_ACTION_UNKNOWN_DO_NOT_REPLAY')
                    kind, params = body['action'], body['parameters']
                    if kind == 'observe':
                        observed = snapshot(); token = uuid.uuid4().hex
                        result.update(state='OBSERVED', token=token, snapshot=observed, snapshot_sha256=digest(observed))
                    elif kind == 'close':
                        if set(params) != {'discard'} or type(params['discard']) is not bool: raise ValueError('PRIVATE_UI_CLOSE_PARAMETERS')
                        save(intent, {'request_sha256': fingerprint, 'state': 'CLOSE_REQUESTED'})
                        for window in snapshot()['windows']:
                            if not alive(): break
                            hwnd = window['hwnd']
                            try:
                                if win32gui.IsWindow(hwnd): win32gui.PostMessage(guard(hwnd), win32con.WM_CLOSE, 0, 0)
                            except Exception:
                                if alive() and win32gui.IsWindow(hwnd): raise
                        end = time.monotonic()+2
                        while alive() and time.monotonic() < end: time.sleep(.05)
                        forced = alive() and params['discard']
                        if forced: win32job.TerminateJobObject(job, 0)
                        exit_requested = not alive() or forced
                        result.update(state='CLOSED' if exit_requested else 'CLOSE_PENDING', forced_discard=bool(forced))
                    else:
                        if token is None or body.get('token') != token: raise ValueError('PRIVATE_UI_OBSERVATION_TOKEN')
                        current = snapshot()
                        if digest(current) != digest(observed): raise ValueError('PRIVATE_UI_OBSERVATION_CHANGED')
                        execute = prepare_action(kind, params, current)
                        token = None
                        save(intent, {'request_sha256': fingerprint, 'state': 'INPUT_REQUESTED'})
                        result.update(execute()); sequence += 1; result['sequence'] = sequence
                    idle_deadline = time.monotonic()+spec['idle_seconds']
                except Exception as exc:
                    result.update(state='UNKNOWN' if intent.exists() else 'REJECTED', error=str(exc))
                save(response, result)
                save(folder/'status.json', identity | {'state': 'CLOSED' if exit_requested else 'READY', 'sequence': sequence})
            time.sleep(.05)
        save(folder/'status.json', identity | {'state': 'CLOSED', 'sequence': sequence,
            'reason': 'requested' if exit_requested else 'application_exit' if not alive() else 'idle_timeout',
            'application_exit_code': win32process.GetExitCodeProcess(process) if not alive() else None})
    except Exception as exc:
        save(folder/'status.json', identity | {'state': 'FAILED', 'error': str(exc)})
        raise
    finally:
        if job is not None: job.Close()  # KILL_ON_JOB_CLOSE affects this owned app tree only.
        if process is not None: win32event.WaitForSingleObject(process, 3000); process.Close()
        if thread is not None: thread.Close()
        if alias is not None: alias.close()
        user.SetThreadDesktop(old_desktop); user.CloseDesktop(desktop)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--child': raise SystemExit(2)
    run_child(Path(sys.argv[2]).resolve(strict=True))
