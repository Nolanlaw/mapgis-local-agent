"""Observation and authorized action preparation for one owned UI process."""

import ctypes as ct
from ctypes import wintypes as wt
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

import win32api
import win32con
import win32event
import win32gui
import win32job
import win32process


class WindowObservation:
    def __init__(self, user, desktop, callback_type, pid, process, display_text):
        self.user = user
        self.desktop = desktop
        self.callback_type = callback_type
        self.pid = pid
        self.process = process
        self.display_text = display_text
        user.GetMenuStringW.argtypes = [wt.HMENU, wt.UINT, wt.LPWSTR, ct.c_int, wt.UINT]
        user.GetMenuStringW.restype = ct.c_int

    def alive(self):
        return win32event.WaitForSingleObject(self.process, 0) == win32con.WAIT_TIMEOUT

    def guard(self, hwnd):
        if not self.alive() or not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1] != self.pid:
            raise ValueError('PRIVATE_UI_WINDOW_IDENTITY')
        return hwnd

    def send(self, hwnd, message, wparam=0, lparam=0, *, timeout_ms=1500):
        self.guard(hwnd); result = ct.c_size_t()
        if not self.user.SendMessageTimeoutW(hwnd, message, wparam, lparam, 1, timeout_ms, ct.byref(result)):
            raise RuntimeError('PRIVATE_UI_SEND_UNKNOWN: '+str(ct.get_last_error()))
        return ct.c_ssize_t(result.value).value

    def text(self, hwnd, *, timeout_ms=200):
        cls = win32gui.GetClassName(hwnd).lower()
        if cls == 'edit' and win32gui.GetWindowLong(hwnd, -16) & 0x20:
            return '<PASSWORD_REDACTED>'
        if cls not in {'edit','combobox','listbox'}:
            return self.display_text(win32gui.GetWindowText(hwnd))
        length = self.send(hwnd, win32con.WM_GETTEXTLENGTH,timeout_ms=timeout_ms)
        if not 0 <= length <= 8192: return '<TEXT_LIMIT>'
        buffer = ct.create_unicode_buffer(length+2)
        self.send(hwnd, win32con.WM_GETTEXT, len(buffer), ct.addressof(buffer),timeout_ms=timeout_ms); return self.display_text(buffer.value)

    def _menu(self, handle, depth=0):
        if not handle: return []
        if depth > 12: raise ValueError('PRIVATE_UI_MENU_DEPTH')
        result = []
        for index in range(win32gui.GetMenuItemCount(handle)):
            label = ct.create_unicode_buffer(512)
            # ctypes signature is declared explicitly to preserve 64-bit handles.
            self.user.GetMenuStringW(handle, index, label, len(label), 0x400)
            sub = win32gui.GetSubMenu(handle, index)
            result.append({'id': win32gui.GetMenuItemID(handle, index), 'text': self.display_text(label.value),
                'disabled': bool(win32gui.GetMenuState(handle, index, 0x400) & 3), 'children': self._menu(sub, depth+1)})
        return result

    def _node(self, hwnd):
        self.guard(hwnd)
        value = {'hwnd': hwnd, 'class': win32gui.GetClassName(hwnd), 'id': win32gui.GetDlgCtrlID(hwnd),
            'parent': win32gui.GetParent(hwnd), 'visible': bool(win32gui.IsWindowVisible(hwnd)),
            'enabled': bool(win32gui.IsWindowEnabled(hwnd)), 'rect': list(win32gui.GetWindowRect(hwnd)),
            'client_rect': list(win32gui.GetClientRect(hwnd))}
        try: value['text'] = self.text(hwnd)
        except RuntimeError: value['text_status'] = 'UNRESPONSIVE'
        try:
            if value['class'].lower() == 'button': value['check_state'] = self.send(hwnd, 0xF0,timeout_ms=100)
            if value['class'].lower() in {'combobox', 'listbox'}:
                combo = value['class'].lower() == 'combobox'
                value['selected_index'] = self.send(hwnd, 0x147 if combo else 0x188,timeout_ms=100)
                value['item_count'] = self.send(hwnd, 0x146 if combo else 0x18B,timeout_ms=100)
            if value['class'].lower()=='systreeview32':
                value['item_count']=self.send(hwnd,0x1105,timeout_ms=100)
                value['selected_item']=self.send(hwnd,0x110A,9,0,timeout_ms=100)
        except RuntimeError:
            value['control_state_status'] = 'UNRESPONSIVE'
        return value

    def snapshot(self):
        roots = []; errors = []
        @self.callback_type
        def found(hwnd, _):
            if win32process.GetWindowThreadProcessId(hwnd)[1] != self.pid: return True
            try:
                value = self._node(hwnd); value['menu'] = self._menu(win32gui.GetMenu(hwnd)); children = []
                def child(h, _):
                    if len(children) >= 2000: raise ValueError('PRIVATE_UI_CONTROL_LIMIT')
                    children.append(self._node(h)); return True
                win32gui.EnumChildWindows(hwnd, child, None); value['children'] = children; roots.append(value)
            except Exception as exc: errors.append(str(exc))
            return True
        self.user.EnumDesktopWindows(self.desktop, found, 0)
        if errors: raise RuntimeError('PRIVATE_UI_OBSERVATION: '+str(errors))
        return {'application_pid': self.pid, 'windows': roots}


def commands(items):
    return {i['id'] for i in items if i['id'] >= 0 and not i['disabled']} | set().union(*(commands(i['children']) for i in items), set())
def command_labels(items, command, parents=()):
    for item in items:
        path = (*parents, item['text'])
        if item['id'] == command: return path
        found = command_labels(item['children'], command, path)
        if found: return found
    return ()


class ActionDispatcher:
    def __init__(self, observation, folder, alias, identity, desktop_name, job, job_name, save, file_confirm_allowed):
        self.observation = observation
        self.folder = folder
        self.alias = alias
        self.application = identity['application']
        self.desktop_name = desktop_name
        self.job = job
        self.job_name = job_name
        self.save = save
        self.file_confirm_allowed = file_confirm_allowed
        self.file_bindings = {}
        self.tree_items = {}

    def _observed_target(self, params, state):
        hwnd = params.get('hwnd')
        nodes = {node['hwnd']: node for window in state['windows'] for node in [window, *window['children']]}
        if type(hwnd) is not int or hwnd not in nodes:
            raise ValueError('PRIVATE_UI_UNOBSERVED_TARGET')
        target = nodes[hwnd]
        self.observation.guard(hwnd)
        if not target['enabled']:
            raise ValueError('PRIVATE_UI_DISABLED_CONTROL')
        return hwnd, target

    def prepare(self, kind, params, state):
        """Validate the observed target and return an action for the intent journal."""
        hwnd, target = self._observed_target(params, state)
        handler = self._HANDLERS.get(kind) if isinstance(kind, str) else None
        if handler is None:
            raise ValueError('PRIVATE_UI_ACTION_NOT_SUPPORTED')
        return handler(self, hwnd, target, params, state)

    def _prepare_tree_items(self, hwnd, target, params, state):
        parent=params.get('parent_item',0);limit=params.get('limit',256);after=params.get('after_item',0)
        if set(params)-{'hwnd','parent_item','limit','after_item'} or target['class'].lower()!='systreeview32' or type(parent)is not int or type(after)is not int or type(limit)is not int or not 1<=limit<=512:
            raise ValueError('PRIVATE_UI_TREE_PARAMETERS')
        if parent and parent not in self.tree_items.get(hwnd,{}):raise ValueError('PRIVATE_UI_TREE_PARENT_NOT_OBSERVED')
        if after and self.tree_items.get(hwnd,{}).get(after,{}).get('parent')!=parent:raise ValueError('PRIVATE_UI_TREE_CURSOR_NOT_OBSERVED')
        def execute_tree_read():
            from mapgis_agent.native_ui_tree import read_tree
            result=read_tree(self.observation.pid,hwnd,self.observation.send,parent,limit,after)
            self.tree_items.setdefault(hwnd,{}).update({item['item']:item for item in result['items']})
            return {'state':'TREE_ITEMS_READ',**result}
        return execute_tree_read

    def _prepare_tree(self, hwnd, target, params, state):
        item=params.get('item');verb=params.get('verb')
        if set(params)!={'hwnd','item','verb'} or target['class'].lower()!='systreeview32' or type(item)is not int or item not in self.tree_items.get(hwnd,{}) or verb not in {'select','expand','collapse'}:
            raise ValueError('PRIVATE_UI_TREE_ITEM_NOT_OBSERVED')
        def execute_tree():
            if verb=='select':
                self.observation.send(hwnd,0x110B,9,item)
                if self.observation.send(hwnd,0x110A,9,0)!=item:raise RuntimeError('PRIVATE_UI_TREE_SELECTION_DIFF')
                return {'state':'CONTROL_READ_BACK','selected_item':item}
            win32gui.PostMessage(hwnd,0x1102,2 if verb=='expand' else 1,item)
            return {'state':'POSTED_NOT_COMPLETION_PROOF'}
        return execute_tree

    def _prepare_capture(self, hwnd, target, params, state):
        if set(params) != {'hwnd'} or not target['visible']:
            raise ValueError('PRIVATE_UI_CAPTURE_TARGET')
        width = target['rect'][2]-target['rect'][0]
        height = target['rect'][3]-target['rect'][1]
        if not 0 < width <= 8192 or not 0 < height <= 8192 or width*height > 16000000:
            raise ValueError('PRIVATE_UI_CAPTURE_SIZE')
        def execute_capture():
            captures=self.folder/'captures';captures.mkdir(exist_ok=True)
            key='frame-'+uuid.uuid4().hex
            description=captures/(key+'.json')
            self.save(description,{'hwnd':hwnd,'width':width,'height':height,'application':self.application,
                'job':self.job_name,'output':str(captures/(key+'.png'))})
            capture_startup=win32process.STARTUPINFO();capture_startup.lpDesktop='WinSta0\\'+self.desktop_name
            capture_startup.dwFlags=win32con.STARTF_USESHOWWINDOW;capture_startup.wShowWindow=win32con.SW_HIDE
            arguments=[sys.executable,'-s','-B','-X','utf8',str(Path(__file__).with_name('private_capture.py')),str(self.folder),str(description)]
            capture_process=capture_thread=None
            try:
                capture_process,capture_thread,capture_pid,_=win32process.CreateProcess(sys.executable,
                    subprocess.list2cmdline(arguments),None,None,False,win32con.CREATE_SUSPENDED|win32con.CREATE_NO_WINDOW,
                    None,str(self.folder),capture_startup)
                win32job.AssignProcessToJobObject(self.job,capture_process);win32process.ResumeThread(capture_thread)
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

    def _prepare_key(self, hwnd, target, params, state):
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

    def _prepare_pointer(self, hwnd, target, params, state):
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

    def _prepare_set_text(self, hwnd, target, params, state):
        if target['class'].lower() not in {'edit', 'combobox'}: raise ValueError('PRIVATE_UI_TEXT_CONTROL')
        if target.get('text') == '<PASSWORD_REDACTED>': raise ValueError('PRIVATE_UI_PASSWORD_ROUTE_NOT_IMPLEMENTED')
        if set(params) == {'hwnd', 'file'}:
            path = Path(params['file']).resolve(strict=False)
            if not path.is_relative_to((self.folder/'files').resolve()): raise ValueError('PRIVATE_UI_FILE_OUTSIDE_OWNED_COPY')
            value = str(self.alias.path/path.relative_to((self.folder/'files').resolve()))
        elif set(params) == {'hwnd', 'value'}:
            value = params['value']
            if not isinstance(value, str) or re.search(r'(?:[A-Za-z]:[\\/]|\\\\)', value):
                raise ValueError('PRIVATE_UI_LITERAL_PATH_REQUIRES_FILE_BINDING')
        else: raise ValueError('PRIVATE_UI_TEXT_PARAMETERS')
        if '\0' in value or len(value) > 8192: raise ValueError('PRIVATE_UI_TEXT_SIZE')
        def execute_text():
            buffer = ct.create_unicode_buffer(value); self.observation.send(hwnd, win32con.WM_SETTEXT, 0, ct.addressof(buffer))
            if self.observation.text(hwnd,timeout_ms=1500) != value: raise RuntimeError('PRIVATE_UI_TEXT_READBACK_DIFF')
            if 'file' in params: self.file_bindings[hwnd] = value
            else: self.file_bindings.pop(hwnd, None)
            return {'state': 'CONTROL_READ_BACK', 'value': value}
        return execute_text

    def _prepare_button(self, hwnd, target, params, state):
        if set(params) != {'hwnd'} or target['class'].lower() != 'button': raise ValueError('PRIVATE_UI_BUTTON_CONTROL')
        if re.search(r'帮助|help|https?://', target.get('text', ''), re.I): raise ValueError('PRIVATE_UI_EXTERNAL_ROUTE_NOT_ALLOWED')
        if target['id'] == 1:
            owner = next((w for w in state['windows'] if w['hwnd'] == target['parent']), None)
            if owner and not self.file_confirm_allowed(owner['children'], self.file_bindings):
                raise ValueError('PRIVATE_UI_FILE_CONFIRM_REQUIRES_BOUND_PATH')
        # Normal BN_CLICKED delivery works on the private inactive dialog.
        def execute_button():
            win32gui.PostMessage(target['parent'], win32con.WM_COMMAND, target['id'] & 0xFFFF, hwnd)
            return {'state': 'POSTED_NOT_COMPLETION_PROOF'}
        return execute_button

    def _prepare_set_check(self, hwnd, target, params, state):
        value = params.get('value')
        if set(params) != {'hwnd', 'value'} or type(value) is not int or value not in {0, 1, 2} or target['class'].lower() != 'button':
            raise ValueError('PRIVATE_UI_CHECK_PARAMETERS')
        if win32gui.GetWindowLong(hwnd, -16) & 15 not in {2, 3, 4, 5, 6, 9}: raise ValueError('PRIVATE_UI_CHECK_CONTROL')
        def execute_check():
            self.observation.send(hwnd, 0xF1, value)
            if self.observation.send(hwnd, 0xF0) != value: raise RuntimeError('PRIVATE_UI_CHECK_READBACK_DIFF')
            win32gui.PostMessage(target['parent'], win32con.WM_COMMAND, target['id'] & 0xFFFF, hwnd)
            return {'state': 'CONTROL_READ_BACK', 'value': value}
        return execute_check

    def _prepare_select(self, hwnd, target, params, state):
        index = params.get('index'); cls = target['class'].lower()
        if set(params) != {'hwnd', 'index'} or type(index) is not int or cls not in {'combobox', 'listbox'} or not 0 <= index < target['item_count']:
            raise ValueError('PRIVATE_UI_SELECTION_PARAMETERS')
        def execute_selection():
            if self.observation.send(hwnd, 0x14E if cls == 'combobox' else 0x186, index) != index: raise RuntimeError('PRIVATE_UI_SELECTION_READBACK_DIFF')
            win32gui.PostMessage(target['parent'], win32con.WM_COMMAND, (1 << 16) | (target['id'] & 0xFFFF), hwnd)
            return {'state': 'CONTROL_READ_BACK', 'index': index}
        return execute_selection

    def _prepare_command(self, hwnd, target, params, state):
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

    _HANDLERS = {
        'tree_items': _prepare_tree_items,
        'tree': _prepare_tree,
        'capture': _prepare_capture,
        'key': _prepare_key,
        'pointer': _prepare_pointer,
        'set_text': _prepare_set_text,
        'button': _prepare_button,
        'set_check': _prepare_set_check,
        'select': _prepare_select,
        'command': _prepare_command,
    }
