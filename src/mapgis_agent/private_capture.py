"""Paint one owned window inside its existing restricted job, without screen DCs."""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib


def main(folder, specification):
    import psutil
    import win32gui, win32process
    spec=json.loads(specification.read_text(encoding='utf8'))
    identity=spec['application'];process=psutil.Process(identity['pid'])
    if abs(process.create_time()-identity['start'])>=1e-5:
        raise ValueError('PRIVATE_CAPTURE_PROCESS_IDENTITY')
    hwnd=spec['hwnd']
    if not win32gui.IsWindow(hwnd) or win32process.GetWindowThreadProcessId(hwnd)[1]!=identity['pid']:
        raise ValueError('PRIVATE_CAPTURE_WINDOW_IDENTITY')
    width,height=spec['width'],spec['height']
    if width<=0 or height<=0 or width*height>16000000:raise ValueError('PRIVATE_CAPTURE_SIZE')
    output=Path(spec['output']).resolve()
    if not output.is_relative_to((folder/'captures').resolve()):raise ValueError('PRIVATE_CAPTURE_OUTPUT')
    user=ct.WinDLL('user32',use_last_error=True)
    user.PrintWindow.argtypes=[wt.HWND,wt.HDC,wt.UINT];user.PrintWindow.restype=wt.BOOL
    kernel=ct.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wt.HANDLE
    kernel.OpenProcess.argtypes=[wt.DWORD,wt.BOOL,wt.DWORD];kernel.OpenProcess.restype=wt.HANDLE
    kernel.OpenJobObjectW.argtypes=[wt.DWORD,wt.BOOL,wt.LPCWSTR];kernel.OpenJobObjectW.restype=wt.HANDLE
    kernel.IsProcessInJob.argtypes=[wt.HANDLE,wt.HANDLE,ct.POINTER(wt.BOOL)];kernel.IsProcessInJob.restype=wt.BOOL
    kernel.CloseHandle.argtypes=[wt.HANDLE]
    job=kernel.OpenJobObjectW(4,False,spec['job'])
    target=kernel.OpenProcess(0x1000,False,identity['pid'])
    try:
        for handle in (kernel.GetCurrentProcess(),target):
            member=wt.BOOL()
            if not handle or not job or not kernel.IsProcessInJob(handle,job,ct.byref(member)) or not member.value:
                raise ValueError('PRIVATE_CAPTURE_JOB_IDENTITY')
    finally:
        if job:kernel.CloseHandle(job)
        if target:kernel.CloseHandle(target)
    class Header(ct.Structure):
        _fields_=[('size',wt.DWORD),('width',wt.LONG),('height',wt.LONG),('planes',wt.WORD),('bits',wt.WORD),
            ('compression',wt.DWORD),('image_size',wt.DWORD),('xppm',wt.LONG),('yppm',wt.LONG),('used',wt.DWORD),('important',wt.DWORD)]
    gdi=ct.WinDLL('gdi32',use_last_error=True)
    gdi.CreateCompatibleDC.argtypes=[wt.HDC];gdi.CreateCompatibleDC.restype=wt.HDC
    gdi.CreateDIBSection.argtypes=[wt.HDC,ct.c_void_p,wt.UINT,ct.POINTER(ct.c_void_p),wt.HANDLE,wt.DWORD];gdi.CreateDIBSection.restype=wt.HBITMAP
    gdi.SelectObject.argtypes=[wt.HDC,wt.HANDLE];gdi.SelectObject.restype=wt.HANDLE
    gdi.DeleteObject.argtypes=[wt.HANDLE];gdi.DeleteDC.argtypes=[wt.HDC]
    gdi.PatBlt.argtypes=[wt.HDC,ct.c_int,ct.c_int,ct.c_int,ct.c_int,wt.DWORD]
    header=Header(ct.sizeof(Header),width,-height,1,32,0,width*height*4,0,0,0,0)
    bits=ct.c_void_p();dc=gdi.CreateCompatibleDC(0);bitmap=previous=None
    if not dc:raise RuntimeError('PRIVATE_CAPTURE_DC')
    try:
        bitmap=gdi.CreateDIBSection(dc,ct.byref(header),0,ct.byref(bits),None,0)
        if not bitmap or not bits.value:raise RuntimeError('PRIVATE_CAPTURE_BITMAP')
        previous=gdi.SelectObject(dc,bitmap);gdi.PatBlt(dc,0,0,width,height,0x00FF0062)
        if not user.PrintWindow(hwnd,dc,2):raise RuntimeError('PRIVATE_CAPTURE_PRINT_FAILED')
        bgra=ct.string_at(bits,width*height*4);rgb=bytearray(width*height*3)
        rgb[0::3]=bgra[2::4];rgb[1::3]=bgra[1::4];rgb[2::3]=bgra[0::4]
        rows=b''.join(b'\0'+rgb[y*width*3:(y+1)*width*3]for y in range(height))
        def chunk(name,data):return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(rows,6))+chunk(b'IEND',b'')
        output.write_bytes(png)
        result={'state':'IMAGE_CAPTURED','file':str(output),'sha256':hashlib.sha256(png).hexdigest(),
            'width':width,'height':height,'method':'OWNED_JOB_PRINTWINDOW','screen_dc_read':False,'desktop_input_used':False}
        specification.with_suffix('.result.json').write_text(json.dumps(result),encoding='utf8')
    finally:
        if previous:gdi.SelectObject(dc,previous)
        if bitmap:gdi.DeleteObject(bitmap)
        gdi.DeleteDC(dc)


if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit(2)
    main(Path(sys.argv[1]).resolve(strict=True),Path(sys.argv[2]).resolve(strict=True))
