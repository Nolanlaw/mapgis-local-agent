"""Read-only data adapter for the verified MapGIS 6.7 build 051118.

This is a compatibility profile, not an SDK or a UI driver. The field layouts
are backed by the assembly retained in evidence/native-interop. Unsupported
images are rejected. No target function is called and no target memory is
written. Active MDI identity is supplied separately by the existing Sky host.
"""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
from datetime import datetime, timezone
import hashlib
import json
import ntpath
import os
from pathlib import Path
import struct

import psutil


class NativeStateError(RuntimeError):
    pass


PROFILE = {
    'mdiedit6x.exe': '34e34d198cce7a10bfa16ddf11e74dbd6afb85d68ac0a8e7f8561a136d052504',
    'maped6x.dll': 'bd627edec5ac0c1940854596f769bffc98601fd22f2f2f728b0766a6e48b2478',
    'mapprj60.dll': 'faeeddbe9d4190f6b3645bfb0610a60bd68a6724f03d1e638932939f083f3291',
    'bas60.dll': '680029d3ed7a04338a6799f1e16cab30d6e421d13e3d8c66bbeccb057a50ee24',
}
ENTITIES = {0: 'LINE', 1: 'POINT', 2: 'POLYGON', 4: 'ARC'}


def _string(data: bytes) -> str:
    return data.split(b'\0', 1)[0].decode('gbk', errors='strict')


def _project_once(read, address: int) -> dict:
    prefix = read(address, 0x649)
    project = struct.unpack_from('<I', prefix, 0x645)[0]
    count, pool = struct.unpack_from('<iI', prefix, 0x175)
    if not project or not 0 <= count <= 100000 or (count and not pool):
        raise NativeStateError('Invalid native project or selection header')
    header = read(project, 0xB37)
    full_path = _string(header[0x3E0:0x4E0])
    name = _string(header[0x5E4:0x6E4])
    modified = struct.unpack_from('<h', header, 0xA)[0]
    total = struct.unpack_from('<h', header, 0x6E6)[0]
    node = struct.unpack_from('<I', header, 0xB33)[0]
    if not name or not 0 <= total <= 4096:
        raise NativeStateError('Invalid native project name or item count')
    layers, visited = [], set()
    for index in range(total):
        if not node or node in visited:
            raise NativeStateError('Invalid native project item chain')
        visited.add(node)
        item = read(node, 0x19A)
        layers.append({'index': index, 'path': _string(item[6:262]),
                       'native_type': item[4], 'native_state': item[5],
                       'area': struct.unpack_from('<h', item, 0x194)[0],
                       'address': hex(node)})
        node = struct.unpack_from('<I', item, 0x196)[0]
    if node:
        raise NativeStateError('Native project item count and chain disagree')
    data = read(pool, count * 8) if count else b''
    selection = []
    for area, entity, record in struct.iter_unpack('<hhi', data):
        if entity not in ENTITIES or record <= 0 or area <= 0:
            raise NativeStateError('Invalid native selection record')
        owners = [layer for layer in layers if layer['area'] == area
                  and layer['native_type'] == (2 if entity == 4 else entity)]
        selection.append({'area': area, 'entity_code': entity, 'entity': ENTITIES[entity],
                          'record_id': record, 'layer_path': owners[0]['path'] if len(owners) == 1 else None,
                          'layer_binding_status': 'unique' if len(owners) == 1 else 'ambiguous'})
    return {'address': hex(address), 'project_address': hex(project),
            'project_path': full_path if ntpath.isabs(full_path) else None,
            'project_name': name, 'project_modified_flag': modified,
            'project_modified': bool(modified), 'workspace_modified': None,
            'layers': layers, 'selection_count': count, 'selection': selection,
            'selection_pool': hex(pool)}


def read_project_view(read, address: int) -> dict:
    """Parse twice; a changing selection, item chain or project is not publishable."""
    first = _project_once(read, address)
    if first != _project_once(read, address):
        raise NativeStateError('Native project or selection changed during snapshot')
    return first


def read_workspaces(read, bas_base: int) -> dict[int, dict]:
    """BAS60 ordinal 93 resolves this list; ordinal 87 reads flag 0xEB7 bit 0."""
    def once():
        head = struct.unpack('<I', read(bas_base + 0x8D928, 4))[0]
        node, seen, rows = head, set(), {}
        while node:
            if node in seen or len(seen) >= 4096:
                raise NativeStateError('Invalid native workspace chain')
            seen.add(node)
            area = struct.unpack('<h', read(node, 2))[0]
            following = struct.unpack('<I', read(node + 8, 4))[0]
            flags = read(node + 0xEB7, 1)[0]
            if area <= 0 or area in rows:
                raise NativeStateError('Invalid or duplicated native workspace handle')
            rows[area] = {'address': hex(node), 'native_flags': flags, 'modified': bool(flags & 1)}
            node = following
        if struct.unpack('<I', read(bas_base + 0x8D928, 4))[0] != head:
            raise NativeStateError('Workspace list changed during snapshot')
        return rows
    first = once()
    if first != once():
        raise NativeStateError('Workspace state changed during snapshot')
    return first


class _MemoryInfo(ct.Structure):
    _fields_ = [('base', ct.c_void_p), ('allocation_base', ct.c_void_p),
                ('allocation_protection', wt.DWORD), ('partition_and_padding', wt.DWORD),
                ('size', ct.c_size_t), ('state', wt.DWORD), ('protection', wt.DWORD),
                ('kind', wt.DWORD), ('padding', wt.DWORD)]


class _ModuleInfo(ct.Structure):
    _fields_ = [('base', ct.c_void_p), ('size', wt.DWORD), ('entry', ct.c_void_p)]


class _ProcessMemory:
    def __init__(self, pid: int):
        if os.name != 'nt' or ct.sizeof(ct.c_void_p) != 8:
            raise NativeStateError('This profile requires 64-bit Python on Windows')
        self.kernel = ct.WinDLL('kernel32', use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        self.kernel.OpenProcess.restype = wt.HANDLE
        self.kernel.CloseHandle.argtypes = [wt.HANDLE]
        self.kernel.ReadProcessMemory.argtypes = [wt.HANDLE, ct.c_void_p, ct.c_void_p,
                                                  ct.c_size_t, ct.POINTER(ct.c_size_t)]
        self.kernel.ReadProcessMemory.restype = wt.BOOL
        self.kernel.VirtualQueryEx.argtypes = [wt.HANDLE, ct.c_void_p, ct.POINTER(_MemoryInfo), ct.c_size_t]
        self.kernel.VirtualQueryEx.restype = ct.c_size_t
        self.handle = self.kernel.OpenProcess(0x410, False, pid)
        if not self.handle:
            raise ct.WinError(ct.get_last_error())

    def close(self):
        self.kernel.CloseHandle(self.handle)

    def read(self, address: int, size: int) -> bytes:
        if not 0 < address < 0x100000000 or not 0 < size <= 1024 * 1024 + 3:
            raise NativeStateError('Invalid bounded x86 memory read')
        buffer, transferred = ct.create_string_buffer(size), ct.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, address, buffer, size, ct.byref(transferred)):
            raise ct.WinError(ct.get_last_error())
        if transferred.value != size:
            raise NativeStateError('Incomplete native read')
        return buffer.raw

    def modules(self) -> dict:
        psapi = ct.WinDLL('psapi', use_last_error=True)
        psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ct.POINTER(wt.HMODULE), wt.DWORD,
                                               ct.POINTER(wt.DWORD), wt.DWORD]
        psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
        psapi.GetModuleInformation.argtypes = [wt.HANDLE, wt.HMODULE, ct.POINTER(_ModuleInfo), wt.DWORD]
        handles, needed = (wt.HMODULE * 1024)(), wt.DWORD()
        if not psapi.EnumProcessModulesEx(self.handle, handles, ct.sizeof(handles), ct.byref(needed), 1):
            raise ct.WinError(ct.get_last_error())
        if needed.value > ct.sizeof(handles):
            raise NativeStateError('Native module inventory exceeded bound')
        result = {}
        for handle in handles[:needed.value // ct.sizeof(wt.HMODULE)]:
            name, info = ct.create_unicode_buffer(32768), _ModuleInfo()
            if not psapi.GetModuleFileNameExW(self.handle, handle, name, len(name)):
                raise ct.WinError(ct.get_last_error())
            key = ntpath.basename(name.value).lower()
            if key not in PROFILE:
                continue
            if key in result or not psapi.GetModuleInformation(self.handle, handle, ct.byref(info), ct.sizeof(info)):
                raise NativeStateError('Ambiguous or unavailable native module')
            result[key] = {'path': name.value, 'base': int(info.base), 'size': info.size,
                           'sha256': hashlib.sha256(Path(name.value).read_bytes()).hexdigest()}
        if set(result) != set(PROFILE) or any(result[k]['sha256'] != v for k, v in PROFILE.items()):
            raise NativeStateError('UNSUPPORTED_NATIVE_BUILD: exact verified module hashes required')
        return result

    def find_views(self, vtable: int):
        needle, address, seen = struct.pack('<I', vtable), 0, set()
        while address < 0x100000000:
            info = _MemoryInfo()
            if not self.kernel.VirtualQueryEx(self.handle, address, ct.byref(info), ct.sizeof(info)):
                break
            following = int(info.base or 0) + info.size
            if following <= address:
                raise NativeStateError('Invalid memory-region enumeration')
            if (info.state == 0x1000 and info.kind == 0x20000 and not info.protection & 0x100
                    and (info.protection & 0xFF) in (4, 8, 0x40, 0x80)):
                for start in range(int(info.base or 0), following, 1024 * 1024):
                    try:
                        data = self.read(start, min(1024 * 1024 + 3, following - start))
                    except OSError:
                        continue
                    position = data.find(needle)
                    while position >= 0:
                        found = start + position
                        if found not in seen:
                            seen.add(found)
                            yield found
                        position = data.find(needle, position + 1)
            address = following


def live_state(root: str | Path, pid: int) -> dict:
    config = json.loads((Path(root) / 'config.local.json').read_text(encoding='utf-8'))
    expected = (Path(config['mapgis_installation']) / 'program/mdiedit6x.exe').resolve()
    process = psutil.Process(pid)
    started = process.create_time()
    if os.path.normcase(process.exe()) != os.path.normcase(str(expected)):
        raise NativeStateError('Editor path does not match the configured installation')
    memory = _ProcessMemory(pid)
    try:
        modules = memory.modules()
        image, editor, project, bas = [modules[n]['base'] for n in PROFILE]
        checks = [(editor + 0x25720, bytes.fromhex('8b8145060000c3')),
                  (project + 0x8540, bytes.fromhex('668b410ac3')),
                  (image + 0xAF1E, b'\xc7\x06' + struct.pack('<I', image + 0x210F8)),
                  (bas + 0x1AE23, bytes.fromhex('8a80b70e00002401'))]
        if any(memory.read(address, len(code)) != code for address, code in checks):
            raise NativeStateError('Loaded native code differs from the verified profile')
        workspaces = read_workspaces(memory.read, bas)
        views, rejected = [], []
        for address in memory.find_views(image + 0x210F8):
            try:
                view = read_project_view(memory.read, address)
                for layer in view['layers']:
                    layer['workspace'] = workspaces.get(layer['area'])
                opened = [layer for layer in view['layers'] if layer['area'] > 0]
                if opened and all(layer['workspace'] is not None for layer in opened):
                    view['workspace_modified'] = any(layer['workspace']['modified'] for layer in opened)
                views.append(view)
            except (OSError, ValueError, NativeStateError) as error:
                rejected.append({'address': hex(address), 'error': str(error)})
        current = psutil.Process(pid)
        if (workspaces != read_workspaces(memory.read, bas) or current.create_time() != started
                or os.path.normcase(current.exe()) != os.path.normcase(str(expected))):
            raise NativeStateError('Native process or workspace changed during snapshot')
        result = {'profile': 'mapgis-6.7-051118-readonly-v1', 'pid': pid, 'create_time': started,
                  'executable': str(expected), 'modules': modules, 'project_views': views,
                  'rejected': rejected, 'active_view': None,
                  'active_view_status': 'requires independent Sky window observation',
                  'access_rights': 'PROCESS_QUERY_INFORMATION | PROCESS_VM_READ',
                  'memory_write_performed': False, 'target_code_invoked': False}
        result['token'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode('utf-8')).hexdigest()
        result['observed_at'] = datetime.now(timezone.utc).isoformat()
        return result
    finally:
        memory.close()
