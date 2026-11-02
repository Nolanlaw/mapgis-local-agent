"""Short, process-owned junctions for the legacy native file interfaces.

Reuses the New-Item Junction approach in Prepare-NativeOutput.ps1. Files remain
inside their owned workspace; removing a link never recursively removes data.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from .core import BridgeError,json_text


def _remove_link(alias:Path,target:Path):
    if not os.path.lexists(alias):return
    if alias.parent!=Path(tempfile.gettempdir()).resolve() or not alias.name.startswith('mg6-'):
        raise BridgeError('NATIVE_ALIAS_IDENTITY')
    if not alias.is_junction() or not os.path.samefile(alias,target):raise BridgeError('NATIVE_ALIAS_TARGET_CHANGED')
    os.rmdir(alias)  # Junction only; no recursive delete and no target traversal.


class NativePathAlias:
    def __init__(self,folder:Path,record_folder:Path|None=None):
        self.target=folder.resolve(strict=True)
        self.path=Path(tempfile.gettempdir()).resolve()/('mg6-'+uuid.uuid4().hex[:12])
        # Read-only input folders and artifact member sets must not receive
        # control records. Their caller supplies its separate diagnostic folder.
        records=(self.target if record_folder is None else Path(record_folder).resolve(strict=True))/'native-path-aliases'
        records.mkdir(exist_ok=True)
        self.record=records/(self.path.name+'.json')
        self._save('CREATING')
        powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        def literal(value):return "'"+str(value).replace("'","''")+"'"
        script="$ErrorActionPreference='Stop';New-Item -ItemType Junction -Path "+literal(self.path)+' -Target '+literal(self.target)+' | Out-Null'
        encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
        result=subprocess.run([str(powershell),'-NoProfile','-NonInteractive','-EncodedCommand',encoded],
            capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode or not self.path.is_junction() or not os.path.samefile(self.path,self.target):
            raise BridgeError('NATIVE_ALIAS_CREATE: cannot create the owned short path')
        self._save('OPEN')

    def _save(self,state):
        temporary=self.record.with_suffix('.tmp')
        with temporary.open('w',encoding='utf8') as stream:
            stream.write(json_text({'alias':str(self.path),'target':str(self.target),'state':state}));stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.record)

    def path_for(self,path):
        relative=Path(path).resolve(strict=True).relative_to(self.target)
        result=self.path/relative
        if len(str(result).encode('gbk',errors='strict'))>190:raise BridgeError('NATIVE_ALIAS_PATH_TOO_LONG: use a shorter native filename')
        return result

    def __enter__(self):return self

    def close(self):
        _remove_link(self.path,self.target);self._save('CLOSED')

    def __exit__(self,*exc):self.close()


def cleanup_aliases(folder:Path):
    target=folder.resolve(strict=True);records=target/'native-path-aliases'
    if not records.is_dir():return
    for record in records.glob('mg6-*.json'):
        data=json.loads(record.read_text(encoding='utf8'))
        if data['state']=='CLOSED':continue
        if Path(data['target']).resolve(strict=True)!=target or Path(data['alias']).name!=record.stem:raise BridgeError('NATIVE_ALIAS_IDENTITY')
        _remove_link(Path(data['alias']),target)
        data['state']='CLOSED';record.write_text(json_text(data),encoding='utf8')
