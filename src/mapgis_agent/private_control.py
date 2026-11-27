"""Non-server installed6.7 UI adapter, disabled until its explicit local opt-in.

The low-level helper always creates a private desktop and a new contained app.
This adapter never attaches to an existing application or accepts an HWND/PID
outside its own latest observation. Existing3BN/3DM tools remain separate.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import time
import uuid

from .core import BridgeError
from .native_ui_core import NativeUIClient, digest, encoded, file_sha, save


class PrivateControlClient(NativeUIClient):
    """Publish to the existing Windows helper with a non-overwriting rename."""

    def request(self, request_id, action, *, token=None, parameters=None, timeout=12):
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', request_id):
            raise ValueError('PRIVATE_UI_REQUEST_ID')
        body = {'id': request_id, 'action': action, 'token': token, 'parameters': parameters or {}}
        fingerprint = digest(body)
        key = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        folder = self.folder/'requests'
        path, response = folder/(key+'.json'), folder/(key+'.response.json')
        if not path.exists():
            incoming = folder/(key+'.incoming-'+uuid.uuid4().hex)
            try:
                with incoming.open('x', encoding='utf-8') as stream:
                    stream.write(encoded(body))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.rename(incoming, path)  # On Windows rename fails if the destination exists.
                except FileExistsError:
                    pass
            finally:
                incoming.unlink(missing_ok=True)
        if json.loads(path.read_text(encoding='utf-8')) != body:
            raise ValueError('PRIVATE_UI_REQUEST_CONFLICT')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if response.exists():
                value = json.loads(response.read_text(encoding='utf-8'))
                if value['request_sha256'] != fingerprint:
                    raise ValueError('PRIVATE_UI_RESPONSE_IDENTITY')
                return value
            state = self.status()
            if state.get('state') in {'CLOSED', 'FAILED'}:
                raise RuntimeError('PRIVATE_UI_ENDED_WITHOUT_REPLY: '+str(state))
            time.sleep(.05)
        raise RuntimeError('PRIVATE_UI_REQUEST_PENDING: reconcile '+str(path))


def _profile():
    return json.loads(Path(__file__).with_name('data').joinpath('private-ui-profile.json').read_text(encoding='utf8'))


def _configuration(root):
    path = Path(root)/'config.local.json'
    return json.loads(path.read_text(encoding='utf8')) if path.is_file() else {}


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,79}', value) or PureWindowsPath(value).is_reserved():
        raise BridgeError('PRIVATE_CONTROL_NAME')
    return value


def _relative(value):
    if not isinstance(value, str) or not value: raise BridgeError('PRIVATE_CONTROL_RELATIVE_PATH')
    p = PureWindowsPath(value)
    if not p.parts or p.is_absolute() or p.drive or p.root or '..' in p.parts or any(
            any(c in part for c in '<>:"|?*') or any(ord(c)<32 for c in part) or part.endswith((' ', '.')) or PureWindowsPath(part).is_reserved() for part in p.parts):
        raise BridgeError('PRIVATE_CONTROL_RELATIVE_PATH')
    return Path(*p.parts)


def _enabled(root):
    if _configuration(root).get('native_general_private_ui') is not True:
        raise BridgeError('PRIVATE_CONTROL_AUTHORIZATION_REQUIRED: this task root requires the authorized non-server private-process scope and native_general_private_ui=true')


def private_control(bridge, action, module='', session_id='', parameters=None, request_id='', token=None):
    parameters = {} if parameters is None else parameters
    if not isinstance(parameters, dict): raise BridgeError('PRIVATE_CONTROL_PARAMETERS')
    profiles = _profile()['modules']; cfg = _configuration(bridge.root)
    if action == 'policy':
        return {'scope': 'Only existing installed6.7 non-server modules, task copies and newly owned private processes.',
            'enabled': cfg.get('native_general_private_ui') is True,
            'modules': profiles, 'attach_existing_process': False, 'desktop_input_used': False,
            'operations': ['set_text', 'button', 'command', 'set_check', 'select', 'pointer', 'key', 'capture', 'tree_items', 'tree'],
            'macro_action': 'macro',
            'state_semantics': 'POSTED_NOT_COMPLETION_PROOF is input acceptance only. Observe and independently validate native output.',
            'excluded': ['server-dependent modules', 'system input/clipboard changes', 'arbitrary executable paths', 'unobserved windows/commands'],
            'validation': 'Owned synthetic GUI controls and file output tested. General non-server private UI authorized; per-module execution evidence is recorded separately.'}
    session_id = bridge.request_id(session_id)
    if action == 'macro':
        _enabled(bridge.root)
        if set(parameters)-{'steps', 'artifact_checks', 'timeout_seconds'}:
            raise BridgeError('UI_MACRO_PARAMETERS')
        from .ui_macros import VerifiedUIMacroRunner
        return VerifiedUIMacroRunner(bridge, session_id).run(parameters.get('steps'), parameters.get('artifact_checks', ()),
                    request_id=request_id, timeout_seconds=parameters.get('timeout_seconds', 4))
    folder = bridge.destination_path(bridge.root/'work/private-controls'/digest(session_id)[:24])
    controller = folder/'controller.json'
    if action == 'open':
        _enabled(bridge.root)
        if module not in profiles: raise BridgeError('PRIVATE_CONTROL_MODULE_NOT_ALLOWED')
        if set(parameters)-{'inputs','outputs','idle_seconds'}: raise BridgeError('PRIVATE_CONTROL_OPEN_PARAMETERS')
        inputs, outputs = parameters.get('inputs', []), parameters.get('outputs', [])
        if not isinstance(inputs, list) or not isinstance(outputs, list) or len(inputs)>30 or len(outputs)>30:
            raise BridgeError('PRIVATE_CONTROL_FILE_LIMITS')
        profile = profiles[module]
        exe = (Path(cfg['mapgis_installation'])/'program'/profile['executable']).resolve(strict=True)
        if file_sha(exe) != profile['sha256']: raise BridgeError('PRIVATE_CONTROL_BUILD_MISMATCH')
        payload = {'module': module, 'parameters': parameters, 'executable_sha256': profile['sha256']}
        if controller.exists():
            record = json.loads(controller.read_text(encoding='utf8'))
            if record['payload_sha256'] != digest(payload): raise BridgeError('PRIVATE_CONTROL_SESSION_CONFLICT')
            return private_control(bridge, 'status', session_id=session_id)
        folder.mkdir(parents=True)
        save(controller, {'session_id':session_id,'module':module,'payload_sha256':digest(payload),'payload':payload})
        runtime = folder/'control'; staged = folder/'staged'; staged.mkdir()
        bindings = {}; sources = []; total = 0
        try:
            for entry in inputs:
                if not isinstance(entry, dict) or not {'id','file','expected_sha256'} <= set(entry) or set(entry)-{'id','file','expected_sha256','dependencies'}:
                    raise BridgeError('PRIVATE_CONTROL_INPUT')
                identity = _name(entry['id'])
                if identity in bindings: raise BridgeError('PRIVATE_CONTROL_DUPLICATE_BINDING')
                source = bridge.owned_path(entry['file']); original_name = source.name
                members = [{'file':str(source),'expected_sha256':entry['expected_sha256'],'name':original_name}]+entry.get('dependencies', [])
                seen = set()
                for member in members:
                    if set(member) != {'file','expected_sha256','name'}: raise BridgeError('PRIVATE_CONTROL_DEPENDENCY')
                    relative = _relative(member['name']); key = relative.as_posix().casefold()
                    if key in seen: raise BridgeError('PRIVATE_CONTROL_MEMBER_COLLISION')
                    seen.add(key); path = bridge.owned_path(member['file'])
                    if file_sha(path) != member['expected_sha256']: raise BridgeError('STALE_SOURCE')
                    total += path.stat().st_size
                    if total > 1024**3: raise BridgeError('PRIVATE_CONTROL_INPUT_LIMIT_1GIB')
                    target = staged/'inputs'/identity/relative; target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(path,target)
                    if file_sha(target) != member['expected_sha256']: raise BridgeError('STALE_SOURCE')
                    sources.append({'file':str(path),'sha256':member['expected_sha256']})
                bindings[identity] = {'role':'input','relative':str(Path('inputs')/identity/original_name)}
            for entry in outputs:
                if not isinstance(entry,dict) or set(entry)-{'id','name','directory'} or not {'id','name'} <= set(entry):
                    raise BridgeError('PRIVATE_CONTROL_OUTPUT')
                identity = _name(entry['id']); name = _relative(entry['name'])
                if identity in bindings: raise BridgeError('PRIVATE_CONTROL_DUPLICATE_BINDING')
                if type(entry.get('directory',False)) is not bool: raise BridgeError('PRIVATE_CONTROL_DIRECTORY_FLAG')
                target = staged/'outputs'/identity/name
                target.parent.mkdir(parents=True,exist_ok=True)
                if entry.get('directory'): target.mkdir()
                bindings[identity] = {'role':'output','relative':str(Path('outputs')/identity/name)}
            save(folder/'bindings.json',{'bindings':bindings,'sources':sources})
            installation = Path(cfg['mapgis_installation'])
            client = PrivateControlClient.start(exe,profile['sha256'],runtime,idle_seconds=parameters.get('idle_seconds',900),
                mapgis_environment={'system_library':str(installation/('SuvSlib' if module=='survey' else 'Slib')),
                    'color_library':str(installation/'CLIB')})
            # Move prepared copies only after the helper has established its own
            # files directory. No file-open command is sent before this finishes.
            for branch in staged.iterdir():
                destination = runtime/'files'/branch.name
                if destination.exists():
                    for member in branch.iterdir(): shutil.move(str(member),destination/member.name)
                    branch.rmdir()
                else: shutil.move(str(branch),destination)
            save(folder/'open-state.json',{'state':'OPEN','runtime':str(runtime)})
            return private_control(bridge,'status',session_id=session_id)
        except Exception as exc:
            save(folder/'open-state.json',{'state':'FAILED','error':str(exc)})
            if (runtime/'status.json').is_file():
                try: PrivateControlClient(runtime).close('failed-open-cleanup',discard=True)
                except Exception: pass  # Durable runtime status/identity remain available to reconcile.
            raise
    if not controller.is_file(): raise BridgeError('PRIVATE_CONTROL_SESSION_NOT_FOUND')
    record = json.loads(controller.read_text(encoding='utf8'))
    if record['session_id'] != session_id or record['module'] not in profiles: raise BridgeError('PRIVATE_CONTROL_SESSION_IDENTITY')
    if not (folder/'control').exists():
        if action == 'status':
            state = folder/'open-state.json'
            return {'session_id':session_id,'module':record['module'],'status':json.loads(state.read_text(encoding='utf8')) if state.is_file() else {'state':'PREPARING'},'processes_alive':{}}
        raise BridgeError('PRIVATE_CONTROL_NOT_STARTED')
    client = PrivateControlClient(folder/'control')
    bindings = json.loads((folder/'bindings.json').read_text(encoding='utf8'))
    if action == 'status':
        status = client.status()
        import psutil
        live = {}
        for key in ('helper','application','launcher'):
            info = status.get(key)
            try: live[key] = bool(info and abs(psutil.Process(info['pid']).create_time()-info['start'])<1e-5)
            except psutil.NoSuchProcess: live[key] = False
        return {'session_id':session_id,'module':record['module'],'status':status,'processes_alive':live,
            'bindings':{k:{**v,'file':str(folder/'control/files'/v['relative'])}for k,v in bindings['bindings'].items()}}
    request_id = bridge.request_id(request_id)
    if action == 'observe':
        return client.observe(request_id)
    if action == 'close':
        if set(parameters)-{'discard'}: raise BridgeError('PRIVATE_CONTROL_CLOSE_PARAMETERS')
        return client.close(request_id,discard=parameters.get('discard',False))
    if action != 'act': raise BridgeError('PRIVATE_CONTROL_ACTION')
    _enabled(bridge.root)
    if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{32}',token): raise BridgeError('PRIVATE_CONTROL_OBSERVATION_REQUIRED')
    operation = parameters.get('operation')
    if operation not in {'set_text','button','command','set_check','select','pointer','key','capture','tree_items','tree'}: raise BridgeError('PRIVATE_CONTROL_OPERATION')
    params = {k:v for k,v in parameters.items()if k!='operation'}
    if 'file' in params: raise BridgeError('PRIVATE_CONTROL_USE_EXPLICIT_BINDING')
    if 'binding' in params:
        identity = params.pop('binding')
        if identity not in bindings['bindings']: raise BridgeError('PRIVATE_CONTROL_BINDING_NOT_FOUND')
        path = folder/'control/files'/bindings['bindings'][identity]['relative']
        if 'member' in params:
            member = _relative(params.pop('member'))
            if not path.is_dir(): raise BridgeError('PRIVATE_CONTROL_BINDING_NOT_DIRECTORY')
            path = path/member
            # Directory members still stay within the owned bound directory.
            if not path.resolve().is_relative_to((folder/'control/files'/bindings['bindings'][identity]['relative']).resolve()):
                raise BridgeError('PRIVATE_CONTROL_MEMBER_OUTSIDE_BINDING')
            path.parent.mkdir(parents=True,exist_ok=True)
        params['file'] = str(path)
    elif 'member' in params:
        raise BridgeError('PRIVATE_CONTROL_MEMBER_REQUIRES_BINDING')
    return client.request(request_id,operation,token=token,parameters=params)
