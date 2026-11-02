"""Typed standard TreeView reads in an already verified owned x86 process.

TVM_GETITEM uses a temporary data buffer in the recipient, never executable
memory or caller-selected addresses. No process code/context is changed.
"""
import ctypes as ct
from ctypes import wintypes as wt
import struct


def read_tree(pid, hwnd, send, parent=0, limit=256, after=0):
    kernel=ct.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD];kernel.OpenProcess.restype=wt.HANDLE
    kernel.VirtualAllocEx.argtypes=[wt.HANDLE,ct.c_void_p,ct.c_size_t,wt.DWORD,wt.DWORD];kernel.VirtualAllocEx.restype=ct.c_void_p
    kernel.VirtualFreeEx.argtypes=[wt.HANDLE,ct.c_void_p,ct.c_size_t,wt.DWORD]
    kernel.WriteProcessMemory.argtypes=[wt.HANDLE,ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.POINTER(ct.c_size_t)]
    kernel.ReadProcessMemory.argtypes=[wt.HANDLE,ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.POINTER(ct.c_size_t)]
    kernel.CloseHandle.argtypes=[wt.HANDLE]
    process=kernel.OpenProcess(0x438,False,pid);buffer=None;pending=False
    if not process:raise RuntimeError('PRIVATE_UI_TREE_PROCESS')
    try:
        buffer=kernel.VirtualAllocEx(process,None,1088,0x3000,4)
        if not buffer or buffer>0xFFFFFFFF:raise RuntimeError('PRIVATE_UI_TREE_X86_BUFFER')
        item=send(hwnd,0x110A,1,after)if after else send(hwnd,0x110A,4 if parent else 0,parent)
        result=[];seen=set()
        while item and len(result)<limit:
            if item in seen:raise RuntimeError('PRIVATE_UI_TREE_CYCLE')
            seen.add(item)
            data=struct.pack('<10I',0x41,item,0,0,buffer+64,512,0,0,0,0)+bytes(1088-40)
            local=ct.create_string_buffer(data);written=ct.c_size_t()
            if not kernel.WriteProcessMemory(process,buffer,local,len(data),ct.byref(written)) or written.value!=len(data):
                raise RuntimeError('PRIVATE_UI_TREE_BUFFER_WRITE')
            pending=True
            if not send(hwnd,0x113E,0,buffer):raise RuntimeError('PRIVATE_UI_TREE_ITEM_DISAPPEARED')
            pending=False
            returned=ct.create_string_buffer(1088);size=ct.c_size_t()
            if not kernel.ReadProcessMemory(process,buffer,returned,1088,ct.byref(size)) or size.value!=1088:
                raise RuntimeError('PRIVATE_UI_TREE_BUFFER_READ')
            text=returned.raw[64:].decode('utf-16-le',errors='backslashreplace').split('\0',1)[0]
            result.append({'item':item,'parent':parent,'text':text,'has_children':bool(struct.unpack_from('<I',returned.raw,32)[0])})
            item=send(hwnd,0x110A,1,item)
        return {'items':result,'more':bool(item),'parent_item':parent,'after_item':result[-1]['item']if result else after}
    finally:
        # A timed-out receiver may still use its buffer later. Retain only this
        # small owned-process allocation until that process exits in that case.
        if buffer and not pending:kernel.VirtualFreeEx(process,buffer,0,0x8000)
        kernel.CloseHandle(process)
