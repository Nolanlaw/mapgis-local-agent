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
    identity = {}; sequence = 0; token = None; observed = None; exit_requested = False; file_bindings = {}; tree_items = {}
    idle_deadline = time.monotonic()+spec['idle_seconds']
    try:
        if not user.SetThreadDesktop(desktop): raise RuntimeError('PRIVATE_UI_THREAD_BIND')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from mapgis_agent.native_paths import NativePathAlias
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
        environment_record = None
        configuration = spec.get('mapgis_environment')
        if configuration is not None:
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
        def alive(): return win32event.WaitForSingleObject(process, 0) == win32con.WAIT_TIMEOUT
        def guard(hwnd):
            if not alive() or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                raise ValueError('PRIVATE_UI_WINDOW_IDENTITY')
            return hwnd
        def send(hwnd, message, wparam=0, lparam=0, *, timeout_ms=1500):
            guard(hwnd); result = ct.c_size_t()
            if not user.SendMessageTimeoutW(hwnd, message, wparam, lparam, 1, timeout_ms, ct.byref(result)):
                raise RuntimeError('PRIVATE_UI_SEND_UNKNOWN: '+str(ct.get_last_error()))
            return ct.c_ssize_t(result.value).value
        def text(hwnd, *, timeout_ms=200):
            cls = win32gui.GetClassName(hwnd).lower()
            if cls == 'edit' and win32gui.GetWindowLong(hwnd, -16) & 0x20:
                return '<PASSWORD_REDACTED>'
            if cls not in {'edit','combobox','listbox'}:
                return display_text(win32gui.GetWindowText(hwnd))
            length = send(hwnd, win32con.WM_GETTEXTLENGTH,timeout_ms=timeout_ms)
            if not 0 <= length <= 8192: return '<TEXT_LIMIT>'
            buffer = ct.create_unicode_buffer(length+2)
            send(hwnd, win32con.WM_GETTEXT, len(buffer), ct.addressof(buffer),timeout_ms=timeout_ms); return display_text(buffer.value)
        def menu(handle, depth=0):
            if not handle: return []
            if depth > 12: raise ValueError('PRIVATE_UI_MENU_DEPTH')
            result = []
            for index in range(win32gui.GetMenuItemCount(handle)):
                label = ct.create_unicode_buffer(512)
                # ctypes signature is declared explicitly to preserve 64-bit handles.
                user.GetMenuStringW(handle, index, label, len(label), 0x400)
                sub = win32gui.GetSubMenu(handle, index)
                result.append({'id': win32gui.GetMenuItemID(handle, index), 'text': display_text(label.value),
                    'disabled': bool(win32gui.GetMenuState(handle, index, 0x400) & 3), 'children': menu(sub, depth+1)})
            return result
        user.GetMenuStringW.argtypes = [wt.HMENU, wt.UINT, wt.LPWSTR, ct.c_int, wt.UINT]
        user.GetMenuStringW.restype = ct.c_int
        def node(hwnd):
            guard(hwnd)
            value = {'hwnd': hwnd, 'class': win32gui.GetClassName(hwnd), 'id': win32gui.GetDlgCtrlID(hwnd),
                'parent': win32gui.GetParent(hwnd), 'visible': bool(win32gui.IsWindowVisible(hwnd)),
                'enabled': bool(win32gui.IsWindowEnabled(hwnd)), 'rect': list(win32gui.GetWindowRect(hwnd)),
                'client_rect': list(win32gui.GetClientRect(hwnd))}
            try: value['text'] = text(hwnd)
            except RuntimeError: value['text_status'] = 'UNRESPONSIVE'
            try:
                if value['class'].lower() == 'button': value['check_state'] = send(hwnd, 0xF0,timeout_ms=100)
                if value['class'].lower() in {'combobox', 'listbox'}:
                    combo = value['class'].lower() == 'combobox'
                    value['selected_index'] = send(hwnd, 0x147 if combo else 0x188,timeout_ms=100)
                    value['item_count'] = send(hwnd, 0x146 if combo else 0x18B,timeout_ms=100)
                if value['class'].lower()=='systreeview32':
                    value['item_count']=send(hwnd,0x1105,timeout_ms=100)
                    value['selected_item']=send(hwnd,0x110A,9,0,timeout_ms=100)
            except RuntimeError:
                value['control_state_status'] = 'UNRESPONSIVE'
            return value
        def snapshot():
            roots = []; errors = []
            @callback_type
            def found(hwnd, _):
                if win32process.GetWindowThreadProcessId(hwnd)[1] != pid: return True
                try:
                    value = node(hwnd); value['menu'] = menu(win32gui.GetMenu(hwnd)); children = []
                    def child(h, _):
                        if len(children) >= 2000: raise ValueError('PRIVATE_UI_CONTROL_LIMIT')
                        children.append(node(h)); return True
                    win32gui.EnumChildWindows(hwnd, child, None); value['children'] = children; roots.append(value)
                except Exception as exc: errors.append(str(exc))
                return True
            user.EnumDesktopWindows(desktop, found, 0)
            if errors: raise RuntimeError('PRIVATE_UI_OBSERVATION: '+str(errors))
            return {'application_pid': pid, 'windows': roots}
        def index_nodes(state):
            return {n['hwnd']: n for w in state['windows'] for n in [w, *w['children']]}
        def observed_target(params, state):
            hwnd = params.get('hwnd'); nodes = index_nodes(state)
            if type(hwnd) is not int or hwnd not in nodes: raise ValueError('PRIVATE_UI_UNOBSERVED_TARGET')
            target = nodes[hwnd]; guard(hwnd)
            if not target['enabled']: raise ValueError('PRIVATE_UI_DISABLED_CONTROL')
            return hwnd, target
        def commands(items):
            return {i['id'] for i in items if i['id'] >= 0 and not i['disabled']} | set().union(*(commands(i['children']) for i in items), set())
        def command_labels(items, command, parents=()):
            for item in items:
                path = (*parents, item['text'])
                if item['id'] == command: return path
                found = command_labels(item['children'], command, path)
                if found: return found
            return ()
        def prepare_action(kind, params, state):
            hwnd, target = observed_target(params, state)
            if kind=='tree_items':
                parent=params.get('parent_item',0);limit=params.get('limit',256);after=params.get('after_item',0)
                if set(params)-{'hwnd','parent_item','limit','after_item'} or target['class'].lower()!='systreeview32' or type(parent)is not int or type(after)is not int or type(limit)is not int or not 1<=limit<=512:
                    raise ValueError('PRIVATE_UI_TREE_PARAMETERS')
                if parent and parent not in tree_items.get(hwnd,{}):raise ValueError('PRIVATE_UI_TREE_PARENT_NOT_OBSERVED')
                if after and tree_items.get(hwnd,{}).get(after,{}).get('parent')!=parent:raise ValueError('PRIVATE_UI_TREE_CURSOR_NOT_OBSERVED')
                def execute_tree_read():
                    from mapgis_agent.native_ui_tree import read_tree
                    result=read_tree(pid,hwnd,send,parent,limit,after)
                    tree_items.setdefault(hwnd,{}).update({item['item']:item for item in result['items']})
                    return {'state':'TREE_ITEMS_READ',**result}
                return execute_tree_read
            if kind=='tree':
                item=params.get('item');verb=params.get('verb')
                if set(params)!={'hwnd','item','verb'} or target['class'].lower()!='systreeview32' or type(item)is not int or item not in tree_items.get(hwnd,{}) or verb not in {'select','expand','collapse'}:
                    raise ValueError('PRIVATE_UI_TREE_ITEM_NOT_OBSERVED')
                def execute_tree():
                    if verb=='select':
                        send(hwnd,0x110B,9,item)
                        if send(hwnd,0x110A,9,0)!=item:raise RuntimeError('PRIVATE_UI_TREE_SELECTION_DIFF')
                        return {'state':'CONTROL_READ_BACK','selected_item':item}
                    win32gui.PostMessage(hwnd,0x1102,2 if verb=='expand' else 1,item)
                    return {'state':'POSTED_NOT_COMPLETION_PROOF'}
                return execute_tree
            if kind == 'capture':
                if set(params) != {'hwnd'} or not target['visible']:
                    raise ValueError('PRIVATE_UI_CAPTURE_TARGET')
                width = target['rect'][2]-target['rect'][0]
                height = target['rect'][3]-target['rect'][1]
                if not 0 < width <= 8192 or not 0 < height <= 8192 or width*height > 16000000:
                    raise ValueError('PRIVATE_UI_CAPTURE_SIZE')
                def execute_capture():
                    captures=folder/'captures';captures.mkdir(exist_ok=True)
                    key='frame-'+uuid.uuid4().hex
                    description=captures/(key+'.json')
                    save(description,{'hwnd':hwnd,'width':width,'height':height,'application':identity['application'],
                        'job':'MapGISNativeUI_'+spec['session'],'output':str(captures/(key+'.png'))})
                    capture_startup=win32process.STARTUPINFO();capture_startup.lpDesktop='WinSta0\\'+desktop_name
                    capture_startup.dwFlags=win32con.STARTF_USESHOWWINDOW;capture_startup.wShowWindow=win32con.SW_HIDE
                    arguments=[sys.executable,'-s','-B','-X','utf8',str(Path(__file__).with_name('private_capture.py')),str(folder),str(description)]
                    capture_process=capture_thread=None
                    try:
                        capture_process,capture_thread,capture_pid,_=win32process.CreateProcess(sys.executable,
                            subprocess.list2cmdline(arguments),None,None,False,win32con.CREATE_SUSPENDED|win32con.CREATE_NO_WINDOW,
                            None,str(folder),capture_startup)
                        win32job.AssignProcessToJobObject(job,capture_process);win32process.ResumeThread(capture_thread)
                        if win32event.WaitForSingleObject(capture_process,8000)!=win32con.WAIT_OBJECT_0:
                            raise RuntimeError('PRIVATE_UI_CAPTURE_TIMEOUT')
                        if win32process.GetExitCodeProcess(capture_process)!=0 or not description.with_suffix('.result.json').is_file():
                            raise RuntimeError('PRIVATE_UI_CAPTURE_WORKER_FAILED: '+str(description))
                        return json.loads(description.with_suffix('.result.json').read_text(encoding='utf8'))|{'capture_pid':capture_pid}
                    finally:
                        if capture_process is not None:
                            if win32event.WaitForSingleObject(capture_process,0)!=win32con.WAIT_OBJECT_0:
                                win32api.TerminateProcess(capture_process,1);win32event.WaitForSingleObject(capture_process,3000)
                            capture_process.Close()
                        if capture_thread is not None:capture_thread.Close()
                return execute_capture
            if kind == 'key':
                keys={'ENTER':13,'ESCAPE':27,'TAB':9,'SPACE':32,'BACKSPACE':8,'DELETE':46,'INSERT':45,
                    'HOME':36,'END':35,'LEFT':37,'RIGHT':39,'UP':38,'DOWN':40,'PAGEUP':33,'PAGEDOWN':34,
                    **{'F'+str(i):111+i for i in range(2,13)}}
                key=params.get('key')
                if set(params) != {'hwnd','key'} or key not in keys or not target['visible']:
                    raise ValueError('PRIVATE_UI_KEY_PARAMETERS')
                def execute_key():
                    vk=keys[key];scan=win32api.MapVirtualKey(vk,0)
                    lparam=1|(scan<<16)|(0x1000000 if key in {'DELETE','INSERT','HOME','END','LEFT','RIGHT','UP','DOWN','PAGEUP','PAGEDOWN'} else 0)
                    win32gui.PostMessage(hwnd,0x100,vk,lparam)
                    win32gui.PostMessage(hwnd,0x101,vk,lparam|0xC0000000)
                    return {'state':'POSTED_NOT_COMPLETION_PROOF','key':key,'global_key_state_changed':False}
                return execute_key
            if kind == 'pointer':
                event, x, y = params.get('event'), params.get('x'), params.get('y')
                events = {'move': ((0x200, 0),), 'left_down': ((0x201, 1),),
                    'left_up': ((0x202, 0),), 'left_click': ((0x201, 1), (0x202, 0)),
                    'right_down': ((0x204, 2),), 'right_up': ((0x205, 0),),
                    'right_click': ((0x204, 2), (0x205, 0)),
                    'double_click': ((0x201, 1), (0x202, 0), (0x203, 1), (0x202, 0))}
                if set(params) != {'hwnd', 'event', 'x', 'y'} or event not in events or type(x) is not int or type(y) is not int:
                    raise ValueError('PRIVATE_UI_POINTER_PARAMETERS')
                _, _, width, height = target['client_rect']
                if not target['visible'] or not 0 <= x < min(width, 32768) or not 0 <= y < min(height, 32768):
                    raise ValueError('PRIVATE_UI_POINTER_CLIENT_BOUNDS')
                def execute_pointer():
                    # Client-coordinate messages only: no SendInput, cursor move,
                    # desktop switch, foreground activation or host key state.
                    for message, buttons in events[event]:
                        win32gui.PostMessage(hwnd, message, buttons, x | (y << 16))
                    return {'state': 'POSTED_NOT_COMPLETION_PROOF', 'client_position': [x, y]}
                return execute_pointer
            if kind == 'set_text':
                if target['class'].lower() not in {'edit', 'combobox'}: raise ValueError('PRIVATE_UI_TEXT_CONTROL')
                if target.get('text') == '<PASSWORD_REDACTED>': raise ValueError('PRIVATE_UI_PASSWORD_ROUTE_NOT_IMPLEMENTED')
                if set(params) == {'hwnd', 'file'}:
                    path = Path(params['file']).resolve(strict=False)
                    if not path.is_relative_to((folder/'files').resolve()): raise ValueError('PRIVATE_UI_FILE_OUTSIDE_OWNED_COPY')
                    value = str(alias.path/path.relative_to((folder/'files').resolve()))
                elif set(params) == {'hwnd', 'value'}:
                    value = params['value']
                    if not isinstance(value, str) or re.search(r'(?:[A-Za-z]:[\\/]|\\\\)', value):
                        raise ValueError('PRIVATE_UI_LITERAL_PATH_REQUIRES_FILE_BINDING')
                else: raise ValueError('PRIVATE_UI_TEXT_PARAMETERS')
                if '\0' in value or len(value) > 8192: raise ValueError('PRIVATE_UI_TEXT_SIZE')
                def execute_text():
                    buffer = ct.create_unicode_buffer(value); send(hwnd, win32con.WM_SETTEXT, 0, ct.addressof(buffer))
                    if text(hwnd,timeout_ms=1500) != value: raise RuntimeError('PRIVATE_UI_TEXT_READBACK_DIFF')
                    if 'file' in params: file_bindings[hwnd] = value
                    else: file_bindings.pop(hwnd, None)
                    return {'state': 'CONTROL_READ_BACK', 'value': value}
                return execute_text
            if kind == 'button':
                if set(params) != {'hwnd'} or target['class'].lower() != 'button': raise ValueError('PRIVATE_UI_BUTTON_CONTROL')
                if re.search(r'帮助|help|https?://', target.get('text', ''), re.I): raise ValueError('PRIVATE_UI_EXTERNAL_ROUTE_NOT_ALLOWED')
                # A common file chooser may remember a non-task folder. Its OK
                # button requires an explicit full path bound by set_text(file).
                if target['id'] == 1:
                    owner = next((w for w in state['windows'] if w['hwnd'] == target['parent']), None)
                    if owner and (any(n['class'].lower() in {'comboboxex32', 'shelldll_defview'} for n in owner['children']) or
                            any('文件名' in n.get('text', '') for n in owner['children'])):
                        if not any(n['hwnd'] in file_bindings and n.get('text') == file_bindings[n['hwnd']] for n in owner['children']):
                            raise ValueError('PRIVATE_UI_FILE_CONFIRM_REQUIRES_BOUND_PATH')
                # Normal BN_CLICKED delivery works on the private inactive dialog.
                def execute_button():
                    win32gui.PostMessage(target['parent'], win32con.WM_COMMAND, target['id'] & 0xFFFF, hwnd)
                    return {'state': 'POSTED_NOT_COMPLETION_PROOF'}
                return execute_button
            if kind == 'set_check':
                value = params.get('value')
                if set(params) != {'hwnd', 'value'} or type(value) is not int or value not in {0, 1, 2} or target['class'].lower() != 'button':
                    raise ValueError('PRIVATE_UI_CHECK_PARAMETERS')
                if win32gui.GetWindowLong(hwnd, -16) & 15 not in {2, 3, 4, 5, 6, 9}: raise ValueError('PRIVATE_UI_CHECK_CONTROL')
                def execute_check():
                    send(hwnd, 0xF1, value)
                    if send(hwnd, 0xF0) != value: raise RuntimeError('PRIVATE_UI_CHECK_READBACK_DIFF')
                    win32gui.PostMessage(target['parent'], win32con.WM_COMMAND, target['id'] & 0xFFFF, hwnd)
                    return {'state': 'CONTROL_READ_BACK', 'value': value}
                return execute_check
            if kind == 'select':
                index = params.get('index'); cls = target['class'].lower()
                if set(params) != {'hwnd', 'index'} or type(index) is not int or cls not in {'combobox', 'listbox'} or not 0 <= index < target['item_count']:
                    raise ValueError('PRIVATE_UI_SELECTION_PARAMETERS')
                def execute_selection():
                    if send(hwnd, 0x14E if cls == 'combobox' else 0x186, index) != index: raise RuntimeError('PRIVATE_UI_SELECTION_READBACK_DIFF')
                    win32gui.PostMessage(target['parent'], win32con.WM_COMMAND, (1 << 16) | (target['id'] & 0xFFFF), hwnd)
                    return {'state': 'CONTROL_READ_BACK', 'index': index}
                return execute_selection
            if kind == 'command':
                command = params.get('command_id')
                if set(params) != {'hwnd', 'command_id'} or type(command) is not int or command not in commands(target.get('menu', [])):
                    raise ValueError('PRIVATE_UI_COMMAND_NOT_OBSERVED')
                if re.search(r'服务器|远程数据库|网络数据库|SQL\s*Server', ' '.join(command_labels(target['menu'], command)), re.I):
                    raise ValueError('PRIVATE_UI_SERVER_FEATURE_EXCLUDED')
                # Help/recent-file entries can delegate outside this owned task.
                if 0xE110 <= command <= 0xE11F or command in {0xE141, 0xE142, 0xE143, 0xE144, 0xE145, 0xE146}:
                    raise ValueError('PRIVATE_UI_EXTERNAL_ROUTE_NOT_ALLOWED')
                def execute_command():
                    win32gui.PostMessage(hwnd, win32con.WM_COMMAND, command, 0)
                    return {'state': 'POSTED_NOT_COMPLETION_PROOF'}
                return execute_command
            raise ValueError('PRIVATE_UI_ACTION_NOT_SUPPORTED')
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
