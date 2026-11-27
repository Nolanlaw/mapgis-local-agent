"""Validation and UI transitions used by the isolated volume child."""

import ctypes as ct
from dataclasses import dataclass
import json
import time
from typing import Callable

import win32con
import win32gui


@dataclass(frozen=True)
class RenderOptions:
    settings: dict
    settings_hash: str | None
    command: int
    axes: list[str]
    deadline: float

    @property
    def view(self):
        return self.settings['view']


@dataclass(frozen=True)
class VolumeControls:
    main: int
    deadline: float
    user: object
    guard: Callable
    send: Callable
    read_text: Callable
    windows: Callable
    wait: Callable
    contains: Callable
    error: type[Exception]


def normalize_render_settings(folder, parent, action, deadline, *, view_commands, view_planes, file_sha, error):
    settings={'view':'full','planes':{}};settings_hash=parent.get('render_settings_sha256')
    if settings_hash is not None:
        if action!='render' or file_sha(folder/'render-settings.json')!=settings_hash:raise error('VOLUME_NATIVE_SETTINGS_IDENTITY')
        settings=json.loads((folder/'render-settings.json').read_text(encoding='utf8'))
        expected_keys={'view','planes','layers'} if settings.get('view')=='multilayer' else {'view','planes'}
        if set(settings)!=expected_keys or settings['view'] not in view_commands:raise error('VOLUME_NATIVE_SETTINGS_PROFILE')
        if settings['view']=='multilayer':
            if not isinstance(settings['layers'],list) or not 2<=len(settings['layers'])<=100:raise error('VOLUME_NATIVE_LAYER_COUNT')
            deadline+=2*len(settings['layers'])
    view=settings['view'];render_command=view_commands[view]
    expected_axes=list(view_planes[view])
    if set(settings['planes'])!=set(expected_axes):raise error('VOLUME_NATIVE_PLANE_AXES')
    return RenderOptions(settings, settings_hash, render_command, expected_axes, deadline)


def open_volume_and_read(ui, app, source, snapshot, alias):
    command=32958 if source.suffix.lower()=='.3bn' else 16592
    if not ui.contains(app['menu'],command):raise ui.error('VOLUME_NATIVE_MENU_PROFILE')
    ui.guard(ui.main);win32gui.PostMessage(ui.main,win32con.WM_COMMAND,command,0)
    def file_ready(items):
        for w in items:
            edits=[c for c in w['children'] if c['class']=='Edit' and c['id'] in {1148,1001}]
            if w['title']=='打开文件' and len(edits)==1:return w,edits[0]
        return None
    dialog,edit=ui.wait(file_ready)
    # Shell common dialogs can reset edits during their initial layout.
    time.sleep(1.5)
    filename=str(alias.path/snapshot.name)
    if len(filename.encode('gbk'))>=120:raise ui.error('VOLUME_NATIVE_FILENAME_LIMIT')
    text=ct.create_unicode_buffer(filename)
    settle_deadline=min(ui.deadline,time.monotonic()+10);filename_attempts=0
    while True:
        if time.monotonic()>=settle_deadline:raise ui.error('VOLUME_NATIVE_FILE_DIALOG_NOT_SETTLED')
        ui.guard(dialog['hwnd']);filename_attempts+=1
        ui.send(edit['hwnd'],win32con.WM_SETTEXT,0,ct.addressof(text))
        stable=True
        for delay in (0.,.3,.3):
            if delay:time.sleep(delay)
            if ui.read_text(edit['hwnd'])!=filename:stable=False;break
        if stable:break
        # A shell callback can reset this edit during initialization.
        # Replace only after observing that reset. No Open/Save/render
        # command has been sent, and a timed-out send still raises.
        time.sleep(.15)
    button=next(c for c in dialog['children'] if c['class']=='Button' and c['id']==1)
    ui.guard(dialog['hwnd']);ui.guard(button['hwnd'])
    win32gui.PostMessage(dialog['hwnd'],win32con.WM_COMMAND,1,button['hwnd'])
    ui.wait(lambda items:not any(w['hwnd']==dialog['hwnd'] for w in items))
    info_command=32960 if command==32958 else 32964
    ui.guard(ui.main);win32gui.PostMessage(ui.main,win32con.WM_COMMAND,info_command,0)
    title='三维离散数据模型信息' if command==32958 else '立体模型信息'
    info=ui.wait(lambda items:next((w for w in items if w['title']==title and any(c.get('items') for c in w['children'])),None))
    lines=next(c['items'] for c in info['children'] if c['class']=='ListBox' and c['id']==1000)
    ui.guard(info['hwnd']);win32gui.PostMessage(info['hwnd'],win32con.WM_COMMAND,1,0)
    ui.wait(lambda items:not any(w['hwnd']==info['hwnd'] for w in items))
    return lines, filename_attempts


def _post_command(ui, value):
    if not ui.contains(win32gui.GetMenu(ui.main),value):raise ui.error('VOLUME_NATIVE_RENDER_MENU_PROFILE')
    ui.guard(ui.main);win32gui.PostMessage(ui.main,win32con.WM_COMMAND,value,0)


def _render_now(ui, value, render_steps):
    if not ui.contains(win32gui.GetMenu(ui.main),value):raise ui.error('VOLUME_NATIVE_RENDER_MENU_PROFILE')
    before=win32gui.GetWindowText(ui.main)
    ui.guard(ui.main);result=ct.c_size_t();remaining=max(1,int((ui.deadline-time.monotonic())*1000))
    if not ui.user.SendMessageTimeoutW(ui.main,win32con.WM_COMMAND,value,0,1,remaining,ct.byref(result)):
        raise ui.error('VOLUME_NATIVE_RENDER_DID_NOT_RETURN: owned Job will close; no Save replay')
    current=ui.wait(lambda items:next((w for w in items if w['hwnd']==ui.main and w['title']!=before and 'D3M分析结果--' in w['title']
        and all(s in w['title'].upper() for s in ('.WT;','.WL;','.WP'))),None))
    render_steps.append({'command':value,'before_title':before,'after_title':current['title']})
    return current


def _position_on(ui, picker, plane, select_native_plane):
    slider=next(c for c in picker['children'] if c['class']=='msctls_trackbar32' and c['id']==1074)
    selected=select_native_plane(plane,ui.send(slider['hwnd'],0x401),ui.send(slider['hwnd'],0x402))
    tick=selected['tick'];ui.send(slider['hwnd'],0x405,1,tick)
    if ui.send(slider['hwnd'],0x400)!=tick:raise ui.error('VOLUME_NATIVE_PLANE_CONTROL')
    ui.send(picker['hwnd'],0x114,((tick&65535)<<16)|5,slider['hwnd'])
    label=next(c for c in picker['children'] if c['class']=='Static' and c['id']==1075)
    displayed=ui.read_text(label['hwnd'])
    if abs(float(displayed)-selected['ui_position'])>.050001:raise ui.error('VOLUME_NATIVE_PLANE_DISPLAY')
    return selected|{'displayed':displayed}


def _configure_palette(ui, notices):
    # Acknowledge only the exact original known warning on this
    # owned process. Unknown warnings still fail in wait().
    for notice in ui.windows():
        if notice['visible'] and notice['title']=='错误警告' and any(c['class']=='Static' and c['text'].startswith('MapGis多用户服务器工作不正常') for c in notice['children']):
            okay=next(c for c in notice['children'] if c['class']=='Button' and c['id']==1 and c['text'] in {'OK','确定'})
            ui.guard(okay['hwnd']);win32gui.PostMessage(notice['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
            ui.wait(lambda items:not any(w['hwnd']==notice['hwnd'] and w['visible'] for w in items))
    _post_command(ui, 16610)
    chooser=ui.wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770' and w['title']=='' and any(c['class']=='ListBox' and c['id']==1000 for c in w['children'])),None))
    selection=next(c for c in chooser['children'] if c['class']=='ListBox' and c['id']==1000)
    if ui.send(selection['hwnd'],0x18B)!=2:raise ui.error('VOLUME_NATIVE_COLOUR_CHOICES')
    choices=[]
    for index in range(2):
        length=ui.send(selection['hwnd'],0x18A,index)
        if not 0<=length<100:raise ui.error('VOLUME_NATIVE_COLOUR_TEXT')
        value=ct.create_unicode_buffer(length+2);ui.send(selection['hwnd'],0x189,index,ct.addressof(value));choices.append(value.value)
    if choices!=['自动','手动']:raise ui.error('VOLUME_NATIVE_COLOUR_CHOICES')
    ui.send(selection['hwnd'],0x186,0)
    if ui.send(selection['hwnd'],0x188)!=0:raise ui.error('VOLUME_NATIVE_COLOUR_SELECTION')
    okay=next(c for c in chooser['children'] if c['class']=='Button' and c['id']==1 and c['text']=='确定')
    ui.guard(okay['hwnd']);win32gui.PostMessage(chooser['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
    ui.wait(lambda items:not any(w['hwnd']==chooser['hwnd'] for w in items))
    def palette_ready(items):
        dialogs=[w for w in items if w['visible'] and w['class']=='#32770']
        for w in dialogs:
            if (w['title']=='Analysis' and any(c['class']=='Static'
                    and ''.join(c['text'].split())=='去掉负值吗?' for c in w['children'])
                    and any(c['class']=='Button' and c['id']==7 and c['text']=='否(&N)' for c in w['children'])):
                return w
        return True if not dialogs and win32gui.IsWindowEnabled(ui.main) else None
    palette_state=ui.wait(palette_ready)
    if isinstance(palette_state,dict):
        # Negative observations are valid input. Retain them through
        # the original No action; do not silently drop scalar data.
        no=next(c for c in palette_state['children'] if c['class']=='Button' and c['id']==7)
        ui.guard(no['hwnd']);win32gui.PostMessage(palette_state['hwnd'],win32con.WM_COMMAND,7,no['hwnd'])
        ui.wait(lambda items:not any(w['hwnd']==palette_state['hwnd'] for w in items))
        if ui.wait(palette_ready) is not True:raise ui.error('VOLUME_NATIVE_PALETTE_UNEXPECTED_REPEAT')
        notices.append('Original negative-value question answered No; negative observations retained')


def _select_planes(ui, options, select_native_plane):
    planes = []
    for axis in options.axes:
        _post_command(ui, {'x':32967,'y':16599,'z':16600}[axis])
        picker=ui.wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770'
            and w['title'].startswith('设置规则') and w['title'].endswith('剖面位置')
            and any(c['class']=='msctls_trackbar32' and c['id']==1074 for c in w['children'])),None))
        selected=_position_on(ui, picker, options.settings['planes'][axis], select_native_plane)
        okay=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1 and c['text']=='确定')
        ui.guard(okay['hwnd']);win32gui.PostMessage(picker['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
        ui.wait(lambda items:not any(w['hwnd']==picker['hwnd'] for w in items))
        planes.append({'axis':axis,**selected})
    return planes


def _render_layers(ui, options, render_steps, select_native_plane):
    layer_planes = []
    before=win32gui.GetWindowText(ui.main);_post_command(ui, options.command)
    picker=ui.wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770'
        and w['title']=='设置规则水平剖面位置' and any(c['class']=='ListBox' and c['id']==1000 for c in w['children'])
        and any(c['class']=='Button' and c['id']==1079 and c['text']=='增加层' for c in w['children'])),None))
    listing=next(c for c in picker['children'] if c['class']=='ListBox' and c['id']==1000)
    add=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1079)
    if ui.send(listing['hwnd'],0x18B)!=0:raise ui.error('VOLUME_NATIVE_LAYER_INITIAL_STATE')
    for index,plane in enumerate(options.settings['layers']):
        selected=_position_on(ui, picker, plane, select_native_plane)
        if selected['grid_index'] in {v['grid_index'] for v in layer_planes}:raise ui.error('VOLUME_NATIVE_LAYER_COLLAPSED: requested layers reach the same source grid plane')
        ui.send(picker['hwnd'],win32con.WM_COMMAND,1079,add['hwnd'])
        if ui.send(listing['hwnd'],0x18B)!=index+1:raise ui.error('VOLUME_NATIVE_LAYER_ADD_COUNT')
        length=ui.send(listing['hwnd'],0x18A,index)
        if not 0<length<100:raise ui.error('VOLUME_NATIVE_LAYER_LIST_TEXT')
        value=ct.create_unicode_buffer(length+2);ui.send(listing['hwnd'],0x189,index,ct.addressof(value))
        parts=value.value.split('层',1)
        if len(parts)!=2 or parts[0].strip()!=str(index+1) or abs(float(parts[1])-selected['ui_position'])>1e-5:
            raise ui.error('VOLUME_NATIVE_LAYER_LIST_VALUE')
        layer_planes.append({'axis':'z',**selected,'list_text':value.value})
    okay=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1 and c['text']=='确定')
    ui.guard(okay['hwnd']);win32gui.PostMessage(picker['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
    ui.wait(lambda items:not any(w['hwnd']==picker['hwnd'] for w in items))
    current=ui.wait(lambda items:next((w for w in items if w['hwnd']==ui.main and w['title']!=before and 'D3M分析结果--' in w['title']
        and all(s in w['title'].upper() for s in ('.WT;','.WL;','.WP'))),None))
    render_steps.append({'command':options.command,'before_title':before,'after_title':current['title']})
    return layer_planes


def _save_rendered_files(ui, options, folder, alias, digest, *, save, file_sha):
    rendered = []
    # D3M Save16620 always uses model[10:13], the cached full-view
    # files. A section is a new composite document: ordinary File /
    # SaveAs33004 saves that active document's three actual files.
    save_command=16620 if options.view == 'full' else 33004
    _post_command(ui, save_command)
    for suffix in ('.wt','.wl','.wp'):
        picker=ui.wait(lambda items:next((w for w in items if w['visible'] and w['class']=='#32770' and w['title']=='MAPGIS 保存文件'),None))
        edit=next(c for c in picker['children'] if c['class']=='Edit' and c['id']==1152)
        if not ui.read_text(edit['hwnd']).lower().endswith(suffix):raise ui.error('VOLUME_NATIVE_SAVE_KIND')
        target=folder/('render'+suffix)
        if target.exists():raise ui.error('VOLUME_NATIVE_SAVE_EXISTS')
        filename=str(alias.path/target.name);text=ct.create_unicode_buffer(filename)
        ui.send(edit['hwnd'],win32con.WM_SETTEXT,0,ct.addressof(text))
        if ui.read_text(edit['hwnd'])!=filename:raise ui.error('VOLUME_NATIVE_SAVE_FILENAME')
        okay=next(c for c in picker['children'] if c['class']=='Button' and c['id']==1 and c['text']=='保存(&S)')
        save(folder/'save-progress.json',{'state':'commit_requested','target':target.name,'completed':rendered,'source_sha256':digest})
        ui.guard(okay['hwnd']);win32gui.PostMessage(picker['hwnd'],win32con.WM_COMMAND,1,okay['hwnd'])
        ui.wait(lambda items:target.is_file() and not any(w['hwnd']==picker['hwnd'] for w in items))
        rendered.append({'name':target.name,'sha256':file_sha(target),'bytes':target.stat().st_size})
        save(folder/'save-progress.json',{'state':'saved','completed':rendered,'source_sha256':digest})
    return save_command, rendered


def render_volume(ui, options, folder, alias, digest, notices, *, select_native_plane, save, file_sha):
    _configure_palette(ui, notices)
    # The original position and section menus require a first full drawing.
    render_steps = []
    _render_now(ui, 32966, render_steps)
    planes = _select_planes(ui, options, select_native_plane)
    layer_planes = []
    if options.view == 'multilayer':
        layer_planes = _render_layers(ui, options, render_steps, select_native_plane)
    elif options.view != 'full':
        _render_now(ui, options.command, render_steps)
    save_command, rendered = _save_rendered_files(ui, options, folder, alias, digest, save=save, file_sha=file_sha)
    return {'action': 'render', 'rendered_files': rendered, 'render_command': options.command,
            'palette': 'automatic', 'view': options.view, 'planes': planes,
            'render_settings_sha256': options.settings_hash, 'save_command': save_command,
            'render_steps': render_steps, 'layer_planes': layer_planes}


def validate_readback(source, snapshot, digest, ended, lines, *, file_sha, error):
    if not ended or lines is None:
        raise error('VOLUME_NATIVE_READBACK_INCOMPLETE')
    if file_sha(snapshot) != digest or file_sha(source) != digest:
        raise error('STALE_DURING_VOLUME_READ')
