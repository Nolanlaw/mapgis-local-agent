"""Original 6.7 volume readback and rendering in owned private processes only.

User-authorized 2026-11-01 exception for 3BN/3DM. No existing window binding,
input-desktop switch, keyboard/mouse event, injection or installation change.
The parent job contains the helper and its newly created original application.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from types import SimpleNamespace

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

if __name__=='__main__':
    # A header-reading child does not need the GIS/NumPy/GDAL import graph.
    # Validation ran in the parent and the copied bytes are bound by SHA256.
    import hashlib
    class BridgeError(RuntimeError):pass
    def json_text(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)
    def file_sha(path):
        with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
else:
    from mapgis_agent.core import BridgeError,json_text
    from mapgis_agent.raster import file_sha

EXE_SHA='ec91e2b3d8fc0b48c2ff008f150dfc07344d672a0df9447ad500296f2bf60e61'
VIEW_COMMANDS={'full':32966,'stereo_x':32968,'stereo_y':16602,'stereo_z':16603,
    'flat_x':16601,'flat_y':32969,'flat_z':32970,'block_1':16604,'block_2':16605,'block_3':16606,
    'scalar_height_z':16902,'multilayer':16624}
VIEW_PLANES={'full':'','stereo_x':'x','stereo_y':'y','stereo_z':'z',
    'flat_x':'x','flat_y':'y','flat_z':'z','block_1':'xz','block_2':'yz','block_3':'xyz','scalar_height_z':'z','multilayer':''}


def select_native_plane(plane,low,high):
    """Match the observed0.1-unit float32 slider and native grid-node reader."""
    import bisect
    import math
    import struct
    minimum,maximum,count,position,error=(plane[k] for k in ('minimum','maximum','count','position','max_error'))
    if (not all(math.isfinite(v) for v in (minimum,maximum,position,error)) or error<0
            or maximum<=minimum or type(count) is not int or not 2<=count<=32767
            or not 0<=low<=high<=2147483647):raise BridgeError('VOLUME_NATIVE_PLANE_PROFILE')
    if not minimum<=position<=maximum:raise BridgeError('VOLUME_NATIVE_PLANE_BOUNDS')
    step=(maximum-minimum)/(count-1)
    def sample(tick):
        ui=struct.unpack('<f',struct.pack('<f',minimum+tick*.1))[0]
        ui=max(minimum,min(maximum,ui))
        index=max(0,min(count-1,int((ui-minimum)/step)))
        actual=maximum if index==count-1 else minimum+index*step
        return ui,index,actual
    ticks=range(low,high+1)
    crossing=low+bisect.bisect_left(ticks,position,key=lambda tick:sample(tick)[2])
    candidates={max(low,min(high,t)) for t in (crossing-1,crossing,crossing+1)}
    best=min(candidates,key=lambda t:(abs(sample(t)[2]-position),abs(sample(t)[0]-position),t))
    actual=sample(best)[2]
    left=low+bisect.bisect_left(ticks,actual,key=lambda t:sample(t)[2])
    right=low+bisect.bisect_right(ticks,actual,key=lambda t:sample(t)[2])-1
    preferred=(position-minimum)/.1
    candidates.update(max(left,min(right,t)) for t in (math.floor(preferred),math.ceil(preferred),left,right))
    best=min(candidates,key=lambda t:(abs(sample(t)[2]-position),abs(sample(t)[0]-position),t))
    ui,index,actual=sample(best);difference=abs(actual-position)
    if difference>error:raise BridgeError(f'VOLUME_NATIVE_PLANE_ERROR: closest reachable plane={actual}, error={difference}, approved={error}')
    return {'tick':best,'range':[low,high],'ui_position':ui,'grid_index':index,'actual_position':actual,
        'requested_position':position,'error':difference,'max_error':error,
        'sampling':'observed0.1-unit float32 UI; legacy native grid index truncates toward zero; no interpolation'}


def native_route_enabled(root):
    path=Path(root)/'config.local.json'
    config=json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
    return sys.platform=='win32' and config.get('native_volume_private_ui') is True and config.get('native_backend_legacy_dep_compatibility') is True


def _save(path,record):
    temporary=path.with_suffix(path.suffix+'.tmp')
    with temporary.open('w',encoding='utf8') as stream:
        stream.write(json_text(record));stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def native_volume_readback(root,file,folder):
    return _volume_session(root,file,folder,'inspect')


def native_volume_render(root,file,folder,settings=None):
    if Path(file).suffix.lower()!='.3dm':raise BridgeError('VOLUME_NATIVE_RENDER_3DM_REQUIRED')
    return _volume_session(root,file,folder,'render',settings)


def _volume_session(root,file,folder,action,settings=None):
    if not native_route_enabled(root):raise BridgeError('VOLUME_NATIVE_ROUTE_NOT_ENABLED: explicit native_volume_private_ui and existing legacy-host opt-ins required')
    from mapgis_agent.native_backend import backend_configuration
    from mapgis_agent.desktop import PrivateDesktop
    from mapgis_agent.native_paths import NativePathAlias
    _,bas=backend_configuration(Path(root))
    exe=bas.parent/'W60_Anly.exe'
    if file_sha(exe)!=EXE_SHA:raise BridgeError('VOLUME_NATIVE_BUILD_MISMATCH')
    folder=Path(folder).resolve(strict=True);file=Path(file).resolve(strict=True)
    with PrivateDesktop(mfc_windows=True) as desktop,NativePathAlias(folder) as alias:
        return _reader_job(root,file,folder,exe,desktop.name,alias.path,action,settings)


def _reader_job(root,file,folder,exe,desktop_name,alias_path,action='inspect',settings=None):
    if action not in {'inspect','render'}:raise BridgeError('VOLUME_NATIVE_ACTION')
    import psutil
    import win32job
    digest=file_sha(file)
    job_name='MapGISVolume_'+uuid.uuid4().hex
    job=win32job.CreateJobObject(None,job_name)
    limits=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation)
    limits['BasicLimitInformation']['LimitFlags']|=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job,win32job.JobObjectExtendedLimitInformation,limits)
    process=None
    try:
        settings_hash=None
        extra_time=0
        if settings is not None:
            if action!='render':raise BridgeError('VOLUME_NATIVE_SETTINGS_ACTION')
            if settings.get('view')=='multilayer':
                if not isinstance(settings.get('layers'),list) or not 2<=len(settings['layers'])<=100:raise BridgeError('VOLUME_NATIVE_LAYER_COUNT')
                extra_time=2*len(settings['layers'])
            _save(folder/'render-settings.json',settings);settings_hash=file_sha(folder/'render-settings.json')
        with (folder/'reader.stdout').open('xb') as out,(folder/'reader.stderr').open('xb') as err:
            process=subprocess.Popen([sys.executable,'-s','-B','-X','utf8',str(Path(__file__).resolve()),
                '--child-render' if action=='render' else '--child',str(Path(root).resolve()),str(file),digest,str(folder),job_name,desktop_name,str(alias_path)],
                cwd=folder,stdin=subprocess.DEVNULL,stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW)
            identity=psutil.Process(process.pid)
            started={'pid':process.pid,'start':identity.create_time(),'executable':identity.exe(),'job_name':job_name,'action':action}
            if settings_hash is not None:started['render_settings_sha256']=settings_hash
            _save(folder/'reader.started.json',started)
            win32job.AssignProcessToJobObject(job,int(process._handle))
            # The helper cannot create any application until it observes this
            # gate AND verifies its membership in this exact named job.
            _save(folder/'reader.ready.json',started)
            try:process.wait(timeout=90+extra_time if action=='render' else 60)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
                raise BridgeError('VOLUME_NATIVE_READ_TIMEOUT: owned job closed; candidate retained')
        if process.returncode:
            failure=folder/'reader.failure.json';detail=''
            if failure.exists():detail='; '+str(json.loads(failure.read_text(encoding='utf8')).get('error',''))[:1000]
            raise BridgeError(f'VOLUME_NATIVE_READER_FAILED: exit={process.returncode}{detail}; diagnostic {folder/"reader.stderr"}')
        result=json.loads((folder/'readback.json').read_text(encoding='utf8'))
        if (result['source_sha256']!=digest or result['snapshot_sha256']!=digest
                or result['executable_sha256']!=EXE_SHA or not result['own_process_ended']
                or not result['private_desktop'] or result['host_input_used'] is not False):
            raise BridgeError('VOLUME_NATIVE_READBACK_IDENTITY')
        if action=='render' and (result.get('action')!='render' or len(result.get('rendered_files',[]))!=3):
            raise BridgeError('VOLUME_NATIVE_RENDER_INCOMPLETE')
        if settings_hash is not None and result.get('render_settings_sha256')!=settings_hash:raise BridgeError('VOLUME_NATIVE_SETTINGS_IDENTITY')
        if file_sha(file)!=digest:raise BridgeError('STALE_DURING_VOLUME_READ')
        return result|{'log':str(folder/'readback.json'),'helper_pid':result['reader_pid'],'launcher_pid':process.pid,'job_name':job_name}
    finally:
        if process is not None and process.poll() is None:
            process.kill();process.wait()
        job.Close()  # Only this helper and its newly created descendants.


def _child(root,source,digest,folder,job_name,desktop_name,alias_path,action='inspect'):
    import ctypes as ct
    from ctypes import wintypes as wt
    import win32con
    import win32event
    import win32gui
    import win32job
    import win32process
    import win32api
    import psutil

    if action not in {'inspect','render'} or (action=='render' and source.suffix.lower()!='.3dm'):
        raise BridgeError('VOLUME_NATIVE_ACTION')
    deadline=time.monotonic()+(75 if action=='render' else 45)
    kernel=ct.WinDLL('kernel32',use_last_error=True)
    kernel.IsProcessInJob.argtypes=[wt.HANDLE,wt.HANDLE,ct.POINTER(wt.BOOL)]
    kernel.IsProcessInJob.restype=wt.BOOL
    if not job_name.startswith('MapGISVolume_'):raise BridgeError('VOLUME_NATIVE_JOB_NAME')
    job=win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY|win32job.JOB_OBJECT_ASSIGN_PROCESS,False,job_name)
    try:
        member=wt.BOOL()
        if not kernel.IsProcessInJob(wt.HANDLE(-1),int(job),ct.byref(member)):raise BridgeError('VOLUME_NATIVE_JOB_QUERY')
        if not member.value:
            # Windows venv python.exe can be a redirector whose real Python
            # child starts before the launcher is assigned. Bind this helper
            # itself before any original application can be created.
            win32job.AssignProcessToJobObject(job,win32api.GetCurrentProcess())
        if not kernel.IsProcessInJob(wt.HANDLE(-1),int(job),ct.byref(member)) or not member.value:
            raise BridgeError('VOLUME_NATIVE_JOB_NOT_BOUND')
    finally:job.Close()
    gate=folder/'reader.ready.json'
    while not gate.exists():
        if time.monotonic()>deadline:raise BridgeError('VOLUME_NATIVE_JOB_GATE')
        time.sleep(.02)
    parent=json.loads(gate.read_text(encoding='utf8'))
    if parent.get('action','inspect')!=action:raise BridgeError('VOLUME_NATIVE_ACTION_IDENTITY')
    settings={'view':'full','planes':{}};settings_hash=parent.get('render_settings_sha256')
    if settings_hash is not None:
        if action!='render' or file_sha(folder/'render-settings.json')!=settings_hash:raise BridgeError('VOLUME_NATIVE_SETTINGS_IDENTITY')
        settings=json.loads((folder/'render-settings.json').read_text(encoding='utf8'))
        expected_keys={'view','planes','layers'} if settings.get('view')=='multilayer' else {'view','planes'}
        if set(settings)!=expected_keys or settings['view'] not in VIEW_COMMANDS:raise BridgeError('VOLUME_NATIVE_SETTINGS_PROFILE')
        if settings['view']=='multilayer':
            if not isinstance(settings['layers'],list) or not 2<=len(settings['layers'])<=100:raise BridgeError('VOLUME_NATIVE_LAYER_COUNT')
            deadline+=2*len(settings['layers'])
    view=settings['view'];render_command=VIEW_COMMANDS[view]
    expected_axes=list(VIEW_PLANES[view])
    if set(settings['planes'])!=set(expected_axes):raise BridgeError('VOLUME_NATIVE_PLANE_AXES')
    lineage=[psutil.Process(),*psutil.Process().parents()]
    if parent['job_name']!=job_name or not any(p.pid==parent['pid'] and abs(p.create_time()-parent['start'])<1e-5 and p.exe()==parent['executable'] for p in lineage):
        raise BridgeError('VOLUME_NATIVE_JOB_IDENTITY')
    current=psutil.Process()
    _save(folder/'reader.actual.json',{'pid':current.pid,'start':current.create_time(),'executable':current.exe(),'job_name':job_name})
    cfg=json.loads((root/'config.local.json').read_text(encoding='utf8'))
    if cfg.get('native_volume_private_ui') is not True or cfg.get('native_backend_legacy_dep_compatibility') is not True:raise BridgeError('VOLUME_NATIVE_ROUTE_NOT_ENABLED')
    program=Path(cfg['mapgis_installation'])/'program';exe=program/'W60_Anly.exe'
    if file_sha(program/'bas60.dll')!='680029d3ed7a04338a6799f1e16cab30d6e421d13e3d8c66bbeccb057a50ee24':raise BridgeError('VOLUME_NATIVE_BAS_PROFILE')
    if file_sha(exe)!=EXE_SHA or source.suffix.lower() not in {'.3bn','.3dm'}:raise BridgeError('VOLUME_NATIVE_PROFILE')
    # Parent checked dimensions, offsets, finite values and statistics before
    # launching; this exact digest binds those checks to the native snapshot.
    if file_sha(source)!=digest:raise BridgeError('STALE_SOURCE')
    snapshot=folder/('input'+source.suffix.lower());shutil.copyfile(source,snapshot)
    if file_sha(snapshot)!=digest:raise BridgeError('VOLUME_SNAPSHOT_DIFF')
    user=ct.WinDLL('user32',use_last_error=True)
    callback_type=ct.WINFUNCTYPE(wt.BOOL,wt.HWND,wt.LPARAM)
    user.EnumDesktopWindows.argtypes=[wt.HANDLE,callback_type,wt.LPARAM]
    user.EnumDesktopWindows.restype=wt.BOOL
    user.GetThreadDesktop.argtypes=[wt.DWORD];user.GetThreadDesktop.restype=wt.HANDLE
    kernel.GetCurrentThreadId.restype=wt.DWORD
    user.SetThreadDesktop.argtypes=[wt.HANDLE];user.SetThreadDesktop.restype=wt.BOOL
    user.OpenDesktopW.argtypes=[wt.LPCWSTR,wt.DWORD,wt.BOOL,wt.DWORD];user.OpenDesktopW.restype=wt.HANDLE
    user.CloseDesktop.argtypes=[wt.HANDLE];user.CloseDesktop.restype=wt.BOOL
    user.GetMenuStringW.argtypes=[wt.HMENU,wt.UINT,wt.LPWSTR,ct.c_int,wt.UINT]
    user.SendMessageTimeoutW.argtypes=[wt.HWND,wt.UINT,wt.WPARAM,wt.LPARAM,wt.UINT,wt.UINT,ct.POINTER(ct.c_size_t)]
    user.SendMessageTimeoutW.restype=ct.c_size_t
    notices=[];process=None;thread=None;main=None;lines=None;forced=False;pid=None;rendered=[];planes=[];layer_planes=[];render_steps=[]
    old_desktop=user.GetThreadDesktop(kernel.GetCurrentThreadId())
    with ExitStack() as resources:
        if not desktop_name.startswith('MapGISAgent_'):raise BridgeError('VOLUME_NATIVE_DESKTOP_NAME')
        handle=user.OpenDesktopW(desktop_name,0,False,0xCF)
        if not handle:raise BridgeError('VOLUME_NATIVE_DESKTOP_OPEN')
        resources.callback(user.CloseDesktop,handle)
        desktop=SimpleNamespace(name=desktop_name,handle=handle)
        alias=SimpleNamespace(path=Path(alias_path))
        if not alias.path.is_junction() or not os.path.samefile(alias.path,folder):raise BridgeError('VOLUME_NATIVE_ALIAS_IDENTITY')
        if not user.SetThreadDesktop(desktop.handle):raise BridgeError('VOLUME_NATIVE_PRIVATE_THREAD: '+str(ct.get_last_error()))
        try:
            startup=win32process.STARTUPINFO();startup.lpDesktop='WinSta0\\'+desktop.name
            startup.dwFlags=win32con.STARTF_USESHOWWINDOW|0x80;startup.wShowWindow=win32con.SW_HIDE
            process,thread,pid,tid=win32process.CreateProcess(str(exe),None,None,None,False,
                win32con.CREATE_NO_WINDOW,None,str(folder),startup)
            import psutil
            app_identity={'pid':pid,'start':psutil.Process(pid).create_time(),'executable':str(exe),'desktop':desktop.name}
            _save(folder/'application.started.json',app_identity)

            def alive():return win32event.WaitForSingleObject(process,0)==win32con.WAIT_TIMEOUT

            def guard(hwnd):
                if not alive() or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1]!=pid:
                    raise BridgeError('VOLUME_NATIVE_WINDOW_IDENTITY')

            def send(hwnd,message,wparam=0,lparam=0):
                guard(hwnd);result=ct.c_size_t()
                remaining=max(1,min(5000,int((deadline-time.monotonic())*1000)))
                # A common file dialog can still be initializing after it first
                # exposes its controls. Give its message thread time to respond;
                # never resend a timed-out mutation or outlive the job deadline.
                if not user.SendMessageTimeoutW(hwnd,message,wparam,lparam,1,remaining,ct.byref(result)):
                    raise BridgeError('VOLUME_NATIVE_MESSAGE_FAILED: '+str(ct.get_last_error()))
                return ct.c_ssize_t(result.value).value

            def read_text(hwnd):
                # GetWindowText does not retrieve another process's edit text.
                length=send(hwnd,win32con.WM_GETTEXTLENGTH)
                if not 0<=length<32768:raise BridgeError('VOLUME_NATIVE_CONTROL_TEXT_SIZE')
                value=ct.create_unicode_buffer(length+2)
                send(hwnd,win32con.WM_GETTEXT,len(value),ct.addressof(value))
                return value.value

            def children(hwnd):
                result=[];errors=[]
                def child(child_hwnd,_):
                    try:
                        guard(child_hwnd)
                        item={'hwnd':child_hwnd,'class':win32gui.GetClassName(child_hwnd),'id':win32gui.GetDlgCtrlID(child_hwnd),'text':win32gui.GetWindowText(child_hwnd)}
                        # Enumeration observes control identity without sending
                        # WM_GETTEXT into a Shell dialog still initializing.
                        # File text is read explicitly after the settled picker.
                        if item['class']=='ListBox' and item['id']==1000 and win32gui.GetWindowText(hwnd) in {'三维离散数据模型信息','立体模型信息'}:
                            style=win32gui.GetWindowLong(child_hwnd,win32con.GWL_STYLE)
                            if style&0x30 and not style&0x40:raise BridgeError('VOLUME_NATIVE_OWNERDRAW_LIST_UNSUPPORTED')
                            count=send(child_hwnd,0x18B)
                            if not 0<=count<=10000:raise BridgeError('VOLUME_NATIVE_INFO_COUNT')
                            item['items']=[]
                            for index in range(count):
                                length=send(child_hwnd,0x18A,index)
                                if not 0<=length<16384:raise BridgeError('VOLUME_NATIVE_INFO_TEXT')
                                text=ct.create_unicode_buffer(length+2)
                                send(child_hwnd,0x189,index,ct.addressof(text));item['items'].append(text.value)
                        result.append(item)
                    except Exception as exc:
                        if win32gui.IsWindow(child_hwnd):errors.append(exc)
                    return True
                win32gui.EnumChildWindows(hwnd,child,None)
                if errors:raise errors[0]
                return result

            def windows():
                result=[];errors=[]
                @callback_type
                def enum(hwnd,_):
                    try:
                        if win32process.GetWindowThreadProcessId(hwnd)[1]==pid:
                            name=win32gui.GetClassName(hwnd);title=win32gui.GetWindowText(hwnd)
                            result.append({'hwnd':hwnd,'class':name,'title':title,'menu':win32gui.GetMenu(hwnd),
                                'visible':bool(win32gui.IsWindowVisible(hwnd)),'children':children(hwnd) if name=='#32770' else []})
                    except Exception as exc:
                        if win32gui.IsWindow(hwnd):errors.append(exc)
                    return True
                ct.set_last_error(0)
                if not user.EnumDesktopWindows(desktop.handle,enum,0) and ct.get_last_error():raise ct.WinError(ct.get_last_error())
                if errors:raise errors[0]
                return result

            def wait(predicate):
                while time.monotonic()<deadline:
                    if not alive():raise BridgeError('VOLUME_NATIVE_APPLICATION_EXITED')
                    items=windows()
                    for item in items:
                        if (item['class']=='#32770' and item['title']=='请选择网络卡(狗)所在的计算机名或IP地址'
                                and any(c['class']=='Static' and c['id']==1016 and '未发现MapGis USB狗' in c['text'] for c in item['children'])):
                            raise BridgeError('VOLUME_NATIVE_ENVIRONMENT_UNAVAILABLE: original helper/USB support is not ready; no host or network selection attempted')
                        if item['class']=='#32770' and item['title']=='错误警告':
                            text='\n'.join(c['text'] for c in item['children'] if c['class']=='Static')
                            # A newly created dialog is observable before its
                            # Static controls have received their text.
                            if not text.strip():continue
                            if not text.startswith('MapGis多用户服务器工作不正常'):raise BridgeError('VOLUME_NATIVE_UNEXPECTED_WARNING: '+text)
                            if text not in notices:notices.append(text)
                    found=predicate(items)
                    if found:return found
                    time.sleep(.05)
                _save(folder/'last-windows.json',{'pid':pid,'windows':windows()})
                raise BridgeError('VOLUME_NATIVE_UI_INCOMPLETE: diagnostic '+str(folder/'last-windows.json'))

            app=wait(lambda items:next((w for w in items if w['menu'] and w['class'].startswith('Afx:') and '空间分析子系统' in w['title']),None))
            main=app['hwnd']
            command=32958 if source.suffix.lower()=='.3bn' else 16592
            def contains(menu,target,depth=0):
                if depth>8:raise BridgeError('VOLUME_NATIVE_MENU_DEPTH')
                for index in range(win32gui.GetMenuItemCount(menu)):
                    if win32gui.GetMenuItemID(menu,index)==target:return True
                    sub=win32gui.GetSubMenu(menu,index)
                    if sub and contains(sub,target,depth+1):return True
                return False
            if not contains(app['menu'],command):raise BridgeError('VOLUME_NATIVE_MENU_PROFILE')
            guard(main);win32gui.PostMessage(main,win32con.WM_COMMAND,command,0)
            def file_ready(items):
                for w in items:
                    edits=[c for c in w['children'] if c['class']=='Edit' and c['id'] in {1148,1001}]
                    if w['title']=='打开文件' and len(edits)==1:return w,edits[0]
                return None
            dialog,edit=wait(file_ready)
            # Shell common dialogs can reset edits during their initial layout.
            time.sleep(1.5)
            filename=str(alias.path/snapshot.name)
            if len(filename.encode('gbk'))>=120:raise BridgeError('VOLUME_NATIVE_FILENAME_LIMIT')
            text=ct.create_unicode_buffer(filename)
            settle_deadline=min(deadline,time.monotonic()+10);filename_attempts=0
            while True:
                if time.monotonic()>=settle_deadline:raise BridgeError('VOLUME_NATIVE_FILE_DIALOG_NOT_SETTLED')
                guard(dialog['hwnd']);filename_attempts+=1
                send(edit['hwnd'],win32con.WM_SETTEXT,0,ct.addressof(text))
                stable=True
                for delay in (0.,.3,.3):
                    if delay:time.sleep(delay)
                    if read_text(edit['hwnd'])!=filename:stable=False;break
                if stable:break
                # A shell callback can reset this edit during initialization.
                # Replace only after observing that reset. No Open/Save/render
                # command has been sent, and a timed-out send still raises.
                time.sleep(.15)
            button=next(c for c in dialog['children'] if c['class']=='Button' and c['id']==1)
            guard(dialog['hwnd']);guard(button['hwnd'])
            win32gui.PostMessage(dialog['hwnd'],win32con.WM_COMMAND,1,button['hwnd'])
            wait(lambda items:not any(w['hwnd']==dialog['hwnd'] for w in items))
            info_command=32960 if command==32958 else 32964
            guard(main);win32gui.PostMessage(main,win32con.WM_COMMAND,info_command,0)
            title='三维离散数据模型信息' if command==32958 else '立体模型信息'
            info=wait(lambda items:next((w for w in items if w['title']==title and any(c.get('items') for c in w['children'])),None))
            lines=next(c['items'] for c in info['children'] if c['class']=='ListBox' and c['id']==1000)
            guard(info['hwnd']);win32gui.PostMessage(info['hwnd'],win32con.WM_COMMAND,1,0)
            wait(lambda items:not any(w['hwnd']==info['hwnd'] for w in items))
            if action=='render':
                def command_now(value):
                    if not contains(win32gui.GetMenu(main),value):raise BridgeError('VOLUME_NATIVE_RENDER_MENU_PROFILE')
                    guard(main);win32gui.PostMessage(main,win32con.WM_COMMAND,value,0)
                def render_now(value):
                    if not contains(win32gui.GetMenu(main),value):raise BridgeError('VOLUME_NATIVE_RENDER_MENU_PROFILE')
                    before=win32gui.GetWindowText(main)
                    guard(main);result=ct.c_size_t();remaining=max(1,int((deadline-time.monotonic())*1000))
                    if not user.SendMessageTimeoutW(main,win32con.WM_COMMAND,value,0,1,remaining,ct.byref(result)):
                        raise BridgeError('VOLUME_NATIVE_RENDER_DID_NOT_RETURN: owned Job will close; no Save replay')
                    current=wait(lambda items:next((w for w in items if w['hwnd']==main and w['title']!=before and 'D3M分析结果--' in w['title']
                        and all(s in w['title'].upper() for s in ('.WT;','.WL;','.WP'))),None))
                    render_steps.append({'command':value,'before_title':before,'after_title':current['title']})
                    return current
                def position_on(picker,plane):
                    slider=next(c for c in picker['children'] if c['class']=='msctls_trackbar32' and c['id']==1074)
                    selected=select_native_plane(plane,send(slider['hwnd'],0x401),send(slider['hwnd'],0x402))
                    tick=selected['tick'];send(slider['hwnd'],0x405,1,tick)
                    if send(slider['hwnd'],0x400)!=tick:raise BridgeError('VOLUME_NATIVE_PLANE_CONTROL')
                    send(picker['hwnd'],0x114,((tick&65535)<<16)|5,slider['hwnd'])
                    label=next(c for c in picker['children'] if c['class']=='Static' and c['id']==1075)
                    displayed=read_text(label['hwnd'])
                    if abs(float(displayed)-selected['ui_position'])>.050001:raise BridgeError('VOLUME_NATIVE_PLANE_DISPLAY')
                    return selected|{'displayed':displayed}
                # Acknowledge only the exact original known warning on this
                # owned process. Unknown warnings still fail in wait().
                for notice in windows():
                    if notice['visible'] and notice['title']=='错误警告' and any(c['class']=='Static' and c['text'].startswith('MapGis多用户服务器工作不正常') for c in notice['children']):
                        okay=next(c for c in notice['children'] if c['class']=='Button' and c['id']==1 and c['text'] in {'OK','确定'})
                        guard(okay['hwnd']);win32gui.PostMessage(notice['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
                        wait(lambda items:not any(w['hwnd']==notice['hwnd'] and w['visible'] for w in items))
                command_now(16610)
                chooser=wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770' and w['title']=='' and any(c['class']=='ListBox' and c['id']==1000 for c in w['children'])),None))
                selection=next(c for c in chooser['children'] if c['class']=='ListBox' and c['id']==1000)
                if send(selection['hwnd'],0x18B)!=2:raise BridgeError('VOLUME_NATIVE_COLOUR_CHOICES')
                choices=[]
                for index in range(2):
                    length=send(selection['hwnd'],0x18A,index)
                    if not 0<=length<100:raise BridgeError('VOLUME_NATIVE_COLOUR_TEXT')
                    value=ct.create_unicode_buffer(length+2);send(selection['hwnd'],0x189,index,ct.addressof(value));choices.append(value.value)
                if choices!=['自动','手动']:raise BridgeError('VOLUME_NATIVE_COLOUR_CHOICES')
                send(selection['hwnd'],0x186,0)
                if send(selection['hwnd'],0x188)!=0:raise BridgeError('VOLUME_NATIVE_COLOUR_SELECTION')
                okay=next(c for c in chooser['children'] if c['class']=='Button' and c['id']==1 and c['text']=='确定')
                guard(okay['hwnd']);win32gui.PostMessage(chooser['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
                wait(lambda items:not any(w['hwnd']==chooser['hwnd'] for w in items))
                def palette_ready(items):
                    dialogs=[w for w in items if w['visible'] and w['class']=='#32770']
                    for w in dialogs:
                        if (w['title']=='Analysis' and any(c['class']=='Static'
                                and ''.join(c['text'].split())=='去掉负值吗?' for c in w['children'])
                                and any(c['class']=='Button' and c['id']==7 and c['text']=='否(&N)' for c in w['children'])):
                            return w
                    return True if not dialogs and win32gui.IsWindowEnabled(main) else None
                palette_state=wait(palette_ready)
                if isinstance(palette_state,dict):
                    # Negative observations are valid input. Retain them through
                    # the original No action; do not silently drop scalar data.
                    no=next(c for c in palette_state['children'] if c['class']=='Button' and c['id']==7)
                    guard(no['hwnd']);win32gui.PostMessage(palette_state['hwnd'],win32con.WM_COMMAND,7,no['hwnd'])
                    wait(lambda items:not any(w['hwnd']==palette_state['hwnd'] for w in items))
                    if wait(palette_ready) is not True:raise BridgeError('VOLUME_NATIVE_PALETTE_UNEXPECTED_REPEAT')
                    notices.append('Original negative-value question answered No; negative observations retained')
                # Position/section menus are enabled only after a first full
                # drawing (the original this[6] flag). Observe that warm-up.
                render_now(32966)
                for axis in expected_axes:
                    command_now({'x':32967,'y':16599,'z':16600}[axis])
                    picker=wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770'
                        and w['title'].startswith('设置规则') and w['title'].endswith('剖面位置')
                        and any(c['class']=='msctls_trackbar32' and c['id']==1074 for c in w['children'])),None))
                    selected=position_on(picker,settings['planes'][axis])
                    okay=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1 and c['text']=='确定')
                    guard(okay['hwnd']);win32gui.PostMessage(picker['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
                    wait(lambda items:not any(w['hwnd']==picker['hwnd'] for w in items))
                    planes.append({'axis':axis,**selected})
                if view=='multilayer':
                    before=win32gui.GetWindowText(main);command_now(render_command)
                    picker=wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770'
                        and w['title']=='设置规则水平剖面位置' and any(c['class']=='ListBox' and c['id']==1000 for c in w['children'])
                        and any(c['class']=='Button' and c['id']==1079 and c['text']=='增加层' for c in w['children'])),None))
                    listing=next(c for c in picker['children'] if c['class']=='ListBox' and c['id']==1000)
                    add=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1079)
                    if send(listing['hwnd'],0x18B)!=0:raise BridgeError('VOLUME_NATIVE_LAYER_INITIAL_STATE')
                    for index,plane in enumerate(settings['layers']):
                        selected=position_on(picker,plane)
                        if selected['grid_index'] in {v['grid_index'] for v in layer_planes}:raise BridgeError('VOLUME_NATIVE_LAYER_COLLAPSED: requested layers reach the same source grid plane')
                        send(picker['hwnd'],win32con.WM_COMMAND,1079,add['hwnd'])
                        if send(listing['hwnd'],0x18B)!=index+1:raise BridgeError('VOLUME_NATIVE_LAYER_ADD_COUNT')
                        length=send(listing['hwnd'],0x18A,index)
                        if not 0<length<100:raise BridgeError('VOLUME_NATIVE_LAYER_LIST_TEXT')
                        value=ct.create_unicode_buffer(length+2);send(listing['hwnd'],0x189,index,ct.addressof(value))
                        parts=value.value.split('层',1)
                        if len(parts)!=2 or parts[0].strip()!=str(index+1) or abs(float(parts[1])-selected['ui_position'])>1e-5:
                            raise BridgeError('VOLUME_NATIVE_LAYER_LIST_VALUE')
                        layer_planes.append({'axis':'z',**selected,'list_text':value.value})
                    okay=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1 and c['text']=='确定')
                    guard(okay['hwnd']);win32gui.PostMessage(picker['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
                    wait(lambda items:not any(w['hwnd']==picker['hwnd'] for w in items))
                    current=wait(lambda items:next((w for w in items if w['hwnd']==main and w['title']!=before and 'D3M分析结果--' in w['title']
                        and all(s in w['title'].upper() for s in ('.WT;','.WL;','.WP'))),None))
                    render_steps.append({'command':render_command,'before_title':before,'after_title':current['title']})
                elif view!='full':render_now(render_command)
                # D3M Save16620 always uses model[10:13], the cached full-view
                # files. A section is a new composite document: ordinary File /
                # SaveAs33004 saves that active document's three actual files.
                save_command=16620 if view=='full' else 33004
                command_now(save_command)
                for suffix in ('.wt','.wl','.wp'):
                    picker=wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770' and w['title']=='MAPGIS 保存文件'),None))
                    edit=next(c for c in picker['children'] if c['class']=='Edit' and c['id']==1152)
                    if not read_text(edit['hwnd']).lower().endswith(suffix):raise BridgeError('VOLUME_NATIVE_SAVE_KIND')
                    target=folder/('render'+suffix)
                    if target.exists():raise BridgeError('VOLUME_NATIVE_SAVE_EXISTS')
                    filename=str(alias.path/target.name);text=ct.create_unicode_buffer(filename)
                    send(edit['hwnd'],win32con.WM_SETTEXT,0,ct.addressof(text))
                    if read_text(edit['hwnd'])!=filename:raise BridgeError('VOLUME_NATIVE_SAVE_FILENAME')
                    okay=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1 and c['text']=='保存(&S)')
                    _save(folder/'save-progress.json',{'state':'commit_requested','target':target.name,'completed':rendered,'source_sha256':digest})
                    guard(okay['hwnd']);win32gui.PostMessage(picker['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
                    wait(lambda items:target.is_file() and not any(w['hwnd']==picker['hwnd'] for w in items))
                    rendered.append({'name':target.name,'sha256':file_sha(target),'bytes':target.stat().st_size})
                    _save(folder/'save-progress.json',{'state':'saved','completed':rendered,'source_sha256':digest})
                _save(folder/'render-complete.json',{'pid':pid,'reader_pid':os.getpid(),'job_name':job_name,
                    'source_sha256':digest,'snapshot_sha256':file_sha(snapshot),'source_preserved':file_sha(source)==digest,
                    'executable_sha256':EXE_SHA,'lines':lines,'private_desktop':True,'host_input_used':False,
                    'desktop':desktop.name,'action':'render','rendered_files':rendered,'render_command':render_command,
                    'palette':'automatic','view':view,'planes':planes,'render_settings_sha256':settings_hash,
                    'save_command':save_command,'render_steps':render_steps,'layer_planes':layer_planes})
        finally:
            if process is not None:
                if win32event.WaitForSingleObject(process,0)==win32con.WAIT_TIMEOUT:
                    if main and win32gui.IsWindow(main):win32gui.PostMessage(main,win32con.WM_CLOSE,0,0)
                    if win32event.WaitForSingleObject(process,2500)==win32con.WAIT_TIMEOUT:
                        win32process.TerminateProcess(process,1);win32event.WaitForSingleObject(process,2500);forced=True
                ended=win32event.WaitForSingleObject(process,0)==win32con.WAIT_OBJECT_0
                exit_code=win32process.GetExitCodeProcess(process)
                thread.Close();process.Close()
                _save(folder/'application.ended.json',{'pid':pid,'own_process_ended':ended,'exit_code':exit_code,'forced_cleanup':forced})
            else:ended=True;exit_code=None
            if not user.SetThreadDesktop(old_desktop):raise BridgeError('VOLUME_NATIVE_THREAD_RESTORE')
        if not ended or lines is None:raise BridgeError('VOLUME_NATIVE_READBACK_INCOMPLETE')
        if file_sha(snapshot)!=digest or file_sha(source)!=digest:raise BridgeError('STALE_DURING_VOLUME_READ')
        result={'pid':pid,'reader_pid':os.getpid(),'bits':32,'desktop':desktop.name,'private_desktop':True,'host_input_used':False,
            'source_sha256':digest,'snapshot_sha256':file_sha(snapshot),'source_preserved':True,
            'executable_sha256':file_sha(exe),'lines':lines,'own_process_ended':ended,'exit_code':exit_code,
            'input_filename_attempts':filename_attempts,
            'forced_cleanup_of_owned_process':forced,'runtime_notices':notices,
            'scope':'original application reads ordered model/layer headers; scalar payload independently checked by the compatible parser'}
        if action=='render':
            result.update(action='render',rendered_files=rendered,render_command=render_command,palette='automatic',
                view=view,planes=planes,layer_planes=layer_planes,render_settings_sha256=settings_hash,save_command=save_command,render_steps=render_steps,
                scope='original colour3D view, saved as editable native2D projection WT/WL/WP; not an interactive3D solid')
    _save(folder/'readback.json',result)
    return result


if __name__=='__main__':
    if len(sys.argv)!=9 or sys.argv[1] not in {'--child','--child-render'}:raise SystemExit('Internal volume reader entry only')
    import faulthandler
    _save(Path(sys.argv[5])/'reader.entry.json',{'pid':os.getpid(),'phase':'before_native_imports'})
    faulthandler.dump_traceback_later(15,file=sys.stderr)
    try:
        result=_child(Path(sys.argv[2]),Path(sys.argv[3]),sys.argv[4],Path(sys.argv[5]),sys.argv[6],sys.argv[7],sys.argv[8],
            'render' if sys.argv[1]=='--child-render' else 'inspect')
        print(json_text({'status':'READBACK_COMPLETE','pid':result['pid'],'source_preserved':result['source_preserved']}))
    except Exception as exc:
        import traceback
        _save(Path(sys.argv[5])/'reader.failure.json',{'error':str(exc),'type':type(exc).__name__})
        traceback.print_exc();raise SystemExit(1)
    finally:faulthandler.cancel_dump_traceback_later()
