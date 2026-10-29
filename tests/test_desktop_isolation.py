import json
import os
from pathlib import Path
import subprocess
import time
import ctypes as ct
from ctypes import wintypes as wt
import pytest

from mapgis_agent.native_backend import _run_child

ROOT=Path(__file__).resolve().parents[1]
PROBE=ROOT/'work/headless-060/DesktopProbe.exe'
pytestmark=pytest.mark.skipif(os.name!='nt' or not PROBE.exists(),reason='Local owned desktop probe required')


def test_native_child_uses_private_desktop_and_reports_actual_identity(tmp_path):
    result,events=_run_child([str(PROBE)],tmp_path,'desktop.json')
    host=next(e for e in events if e['event']=='host')
    assert host['desktop'].startswith('MapGISAgent_')
    assert result['desktop']==host['desktop']


def test_owned_modal_dialog_cannot_take_the_input_desktop():
    from mapgis_agent.desktop import PrivateDesktop
    user=ct.WinDLL('user32',use_last_error=True)
    user.GetForegroundWindow.restype=wt.HWND
    foreground=user.GetForegroundWindow()
    with PrivateDesktop() as desktop:
        process=subprocess.Popen([str(PROBE),'dialog'],stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=desktop.startupinfo,env=desktop.environment)
        try:
            host=json.loads(process.stdout.readline())
            assert host['desktop']==desktop.name
            time.sleep(.1)
            assert process.poll() is None  # Its own modal dialog is still waiting.
            assert user.GetForegroundWindow()==foreground
        finally:
            process.kill();process.wait(timeout=5)
            process.stdout.close();process.stderr.close()
