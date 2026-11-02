"""Process-owned desktop isolation for legacy DLLs that can show modal errors.

No input desktop is switched, no user window is driven, and no ACL/settings are
changed. The resource exists only while an owned native child uses it.
"""
import ctypes as ct
from ctypes import wintypes as wt
import subprocess
import uuid
import os

from .core import BridgeError


class PrivateDesktop:
    def __init__(self,*,mfc_windows:bool=False):
        self.user=ct.WinDLL('user32',use_last_error=True)
        self.user.CreateDesktopW.argtypes=[wt.LPCWSTR,wt.LPCWSTR,ct.c_void_p,wt.DWORD,wt.DWORD,ct.c_void_p]
        self.user.CreateDesktopW.restype=wt.HANDLE
        self.user.CloseDesktop.argtypes=[wt.HANDLE]
        self.user.CloseDesktop.restype=wt.BOOL
        self.name='MapGISAgent_'+uuid.uuid4().hex
        # MFC's thread-scoped CBT callback needs HOOKCONTROL on this newly
        # created private desktop. Default native operations keep their mask.
        # Neither mask grants desktop switching, journaling or ACL changes.
        self.access=0xC7|(0x08 if mfc_windows else 0)
        self.handle=self.user.CreateDesktopW(self.name,None,None,0,self.access,None)
        if not self.handle:raise BridgeError(f'PRIVATE_DESKTOP_CREATE: Windows error {ct.get_last_error()}')
        self.startupinfo=subprocess.STARTUPINFO()
        self.startupinfo.dwFlags=subprocess.STARTF_USESHOWWINDOW|0x80  # No startup cursor feedback.
        self.startupinfo.wShowWindow=0
        # CPython STARTUPINFO does not marshal an lpDesktop attribute. Our own
        # native host binds its initial thread before loading any MapGIS DLL.
        self.environment=dict(os.environ,MAPGIS_AGENT_DESKTOP=self.name)

    def __enter__(self):return self

    def close(self):
        if self.handle:
            if not self.user.CloseDesktop(self.handle):
                raise BridgeError(f'PRIVATE_DESKTOP_CLOSE: Windows error {ct.get_last_error()}')
            self.handle=None

    def __exit__(self,*exc):self.close()
