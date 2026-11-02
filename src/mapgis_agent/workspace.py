"""Owned persistent native editing buffers with durable recovery checkpoints.

These buffers belong to the agent. They do not attach to an external editor.
Only explicit save publishes a deliverable; checkpoints keep dirty work recoverable.
"""
from __future__ import annotations
from contextlib import ExitStack

import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

import psutil

from .core import Bridge, BridgeError, json_text, read_native, sha
from .native_backend import KINDS, _parameters, _validate, _native_commands, _native_opcode, backend_configuration, native_file_info, prepare_native_operation
from .desktop import PrivateDesktop
from .native_paths import NativePathAlias,cleanup_aliases


class _NativeBuffer:
    def __init__(self, executable: Path, dll: Path, state: dict, folder: Path):
        self.revision = state['revision']
        self.events = queue.Queue()
        self.error_file = (folder/('native-'+uuid.uuid4().hex+'.stderr')).open('xb')
        self.resources=ExitStack()
        try:
            self.path_alias=self.resources.enter_context(NativePathAlias(folder))
            self.desktop=self.resources.enter_context(PrivateDesktop())
            self.process = subprocess.Popen([str(executable),'workspace',str(dll),str(self.path_alias.path_for(state['checkpoint'])),
                str(KINDS[state['native_type']]),str(self.path_alias.path),str(self.revision)],cwd=folder,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.error_file,
                creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=self.desktop.startupinfo,env=self.desktop.environment)
        except Exception:
            self.error_file.close();self.resources.close();raise
        self.started = psutil.Process(self.process.pid).create_time()
        self.log = folder/f'native-{self.process.pid}.jsonl'
        self.reader = threading.Thread(target=self._read,daemon=True)
        self.reader.start()
        try:
            ready = self._response(None)
            if (ready.get('event')!='ready' or ready.get('bits')!=32 or ready.get('dep_flags')!=0
                    or ready.get('pid')!=self.process.pid or ready.get('revision')!=self.revision
                    or ready.get('desktop')!=self.desktop.name):
                raise BridgeError('WORKSPACE_PROTOCOL: invalid native initialization')
            self.active = ready['active']
        except Exception:
            self.close()
            raise

    def _read(self):
        with self.log.open('ab') as log:
            for line in self.process.stdout:
                log.write(line); log.flush()
                try:self.events.put(json.loads(line))
                except ValueError:self.events.put({'event':'invalid'})
        self.events.put({'event':'eof'})

    def _response(self, token):
        deadline = time.monotonic()+30
        while time.monotonic()<deadline:
            try:event = self.events.get(timeout=max(.01,deadline-time.monotonic()))
            except queue.Empty:break
            if event.get('event') in ('eof','invalid'):
                raise BridgeError(f'WORKSPACE_CHILD: native worker ended or sent invalid data; {self.log}')
            if token is not None and event.get('request')!=token:
                continue  # A late response is logged; it never causes an edit replay.
            if event.get('event')=='error':
                raise BridgeError(f'WORKSPACE_NATIVE_EDIT: native command rejected; {self.log}')
            return event
        raise BridgeError(f'WORKSPACE_TIMEOUT: reconcile the checkpoint before another edit; {self.log}')

    def ask(self, operation: str, token: str | None = None, data: str = '') -> dict:
        token = token or uuid.uuid4().hex
        if self.process.poll() is not None:
            raise BridgeError('WORKSPACE_LOST: recover the last complete checkpoint')
        self.process.stdin.write(f'{token} {self.revision} {operation} {data}\n'.encode('ascii'))
        self.process.stdin.flush()
        response = self._response(token)
        if response.get('pid')!=self.process.pid:
            raise BridgeError('WORKSPACE_PROTOCOL: PID differs')
        self.revision = response['revision']
        self.active = response['active']
        return response

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()  # EOF closes this buffer's native area normally.
        try:self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()  # Popen-owned child only.
            self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.error_file.close()
        self.resources.close()


class NativeWorkspaces:
    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.buffers: dict[str,_NativeBuffer] = {}
        self.leases = {}
        self.lock = threading.RLock()
        atexit.register(self.close_all)

    def _folder(self, identity):
        self.bridge.request_id(identity)
        return self.bridge.destination_path(self.bridge.root/'work/native-workspaces'/sha(identity.encode())[:24])

    def _write_state(self, folder, state):
        temporary = folder/('state-'+uuid.uuid4().hex+'.tmp')
        with temporary.open('xb') as stream:
            stream.write(json_text(state).encode('utf8')); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,folder/'state.json')

    def _claim(self,folder,identity):
        if identity in self.leases:return
        stream=(folder/'state.lock').open('a+b')
        if stream.tell()==0:stream.write(b'\0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise BridgeError('WORKSPACE_BUSY: another controller owns this buffer') from exc
        self.leases[identity]=stream

    def _release(self,identity):
        stream=self.leases.pop(identity,None)
        if stream is not None:stream.close()  # OS releases the lease on crash too.

    def _load(self, identity):
        folder = self._folder(identity)
        if not folder.is_dir():raise BridgeError('WORKSPACE_MISSING')
        was_owned=identity in self.leases
        self._claim(folder,identity)
        try:
            try:state = json.loads((folder/'state.json').read_text(encoding='utf8'))
            except FileNotFoundError as exc:raise BridgeError('WORKSPACE_MISSING') from exc
            if state['id']!=identity:
                raise BridgeError('WORKSPACE_IDENTITY')
            checkpoint = self.bridge.owned_path(state['checkpoint'])
            if not checkpoint.is_relative_to(folder) or sha(checkpoint.read_bytes())!=state['checkpoint_sha256']:
                raise BridgeError('WORKSPACE_CHECKPOINT_CHANGED')
            return folder,state
        except Exception:
            if not was_owned:self._release(identity)
            raise

    def _public(self,state):
        buffer = self.buffers.get(state['id'])
        running = buffer is not None and buffer.process.poll() is None
        return {key:state[key] for key in ('id','revision','dirty','active','native_type','checkpoint','checkpoint_sha256')} | {
            'running':running,'native_pid':buffer.process.pid if running else None,
            'state':state['state'] if running or state['state']=='CLOSED' else 'LOST',
            'saved_output':state.get('saved_output'),'pending_request':(state.get('pending') or {}).get('id'),
            'scope':'agent-owned native buffer; external editor memory is not changed',
            'replayed':False}

    def _start(self,folder,state,*,persist=True):
        self._claim(folder,state['id'])
        executable,dll = backend_configuration(self.bridge.root)
        buffer = _NativeBuffer(executable,dll,state,folder)
        if buffer.active != state['active']:
            buffer.close(); raise BridgeError('WORKSPACE_NATIVE_COUNT')
        self.buffers[state['id']] = buffer
        controller = psutil.Process()
        state.update(state='OPEN',native_pid=buffer.process.pid,native_start=buffer.started,
            native_desktop=buffer.desktop.name,
            native_path_alias=str(buffer.path_alias.path),
            native_executable=str(executable.resolve()),controller_pid=controller.pid,
            controller_start=controller.create_time())
        if persist:self._write_state(folder,state)

    def _stop(self,state):
        buffer = self.buffers.pop(state['id'],None)
        if buffer is not None:
            buffer.close()

    def _retire_orphan(self,state):
        if state['id'] not in self.leases:raise BridgeError('WORKSPACE_BUSY')
        self._stop(state)
        if not state.get('native_pid'):
            cleanup_aliases(self._folder(state['id']))
            return
        try:
            process = psutil.Process(state['native_pid'])
            if (abs(process.create_time()-state['native_start'])<.001
                    and Path(process.exe()).resolve()==Path(state['native_executable']).resolve()):
                process.terminate(); process.wait(timeout=5)
        except psutil.NoSuchProcess:pass
        cleanup_aliases(self._folder(state['id']))

    def _validate_checkpoint(self,state,pending):
        candidate = self.bridge.owned_path(pending['candidate'])
        complete = json.loads(candidate.with_name('native-complete.json').read_text(encoding='utf8'))
        if (not complete.get('checkpoint_complete') or complete.get('request')!=pending['token']
                or complete.get('revision')!=state['revision']+1 or complete.get('pid')!=pending['native_pid']
                or complete.get('bits')!=32 or complete.get('dep_flags')!=0):
            raise BridgeError('WORKSPACE_INCOMPLETE_CHECKPOINT')
        blob,comparison = _validate(Path(state['checkpoint']),candidate,pending['operation'],pending['parameters'])
        reopened = native_file_info(self.bridge.root,str(candidate),sha(blob))
        if reopened['active']!=complete['active']:
            raise BridgeError('WORKSPACE_REOPEN_COUNT')
        return candidate,sha(blob),complete['active'],comparison,reopened

    def _commit_edit(self,folder,state,pending,validated):
        candidate,digest,active,comparison,reopened = validated
        state.update(checkpoint=str(candidate),checkpoint_sha256=digest,active=active,
            revision=state['revision']+1,dirty=True,pending=None)
        receipt = self._public(state) | {'comparison':comparison,'native_reopen':reopened}
        state['requests'][pending['id']] = {'hash':pending['hash'],'receipt':receipt}
        self._write_state(folder,state)
        return receipt

    def _commit_save(self,folder,state,pending,result):
        state.update(dirty=False,saved_revision=state['revision'],saved_checkpoint=state['checkpoint'],
            saved_checkpoint_sha256=state['checkpoint_sha256'],saved_output=result['output'],pending=None)
        receipt = self._public(state) | {'output':result['output'],'output_sha256':result['candidate_sha256'],
            'save_request':pending['bridge_request']}
        state['requests'][pending['id']] = {'hash':pending['hash'],'receipt':receipt}
        self._write_state(folder,state)
        return receipt

    def _append_result(self,pending):
        result=self.bridge.status(pending['bridge_request'])
        if result['state'] in {'RUNNING','FAILED'}:
            from .native_append import reconcile_native_append
            result=reconcile_native_append(self.bridge,pending['bridge_request'])
        if result['state'] not in {'PREPARED','APPLIED'}:raise BridgeError('WORKSPACE_APPEND_INCOMPLETE')
        return result

    def _commit_append(self,folder,state,pending,result):
        for source in result['sources']:
            if sha(Path(source['file']).read_bytes())!=source['sha256']:raise BridgeError('STALE_SOURCE')
        candidate=self.bridge.owned_path(Path(result['stage'])/'files'/result['payload']['template']['name'])
        blob,reader,_=read_native(candidate);digest=sha(blob)
        if digest!=result['candidate_sha256'] or len(reader.geom)!=state['active']+len(pending['parameters']['record_ids']):
            raise BridgeError('WORKSPACE_APPEND_CHECKPOINT_CHANGED')
        checkpoint=folder/(pending['token']+'-append'+candidate.suffix.lower())
        if checkpoint.exists():
            if sha(checkpoint.read_bytes())!=digest:raise BridgeError('WORKSPACE_APPEND_CHECKPOINT_CONFLICT')
        else:
            temporary=checkpoint.with_suffix(checkpoint.suffix+'.tmp')
            with temporary.open('wb') as stream:stream.write(blob);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,checkpoint)
        next_state=dict(state);next_state['requests']=dict(state['requests'])
        next_state.update(checkpoint=str(checkpoint),checkpoint_sha256=digest,active=len(reader.geom),revision=state['revision']+1,dirty=True,pending=None)
        self._stop(state)
        try:
            # A fresh private native buffer adopts the checked CopyList result.
            # Until request+revision commit together, the old pending journal
            # remains authoritative if this parent is interrupted.
            self._start(folder,next_state,persist=False)
            receipt=self._public(next_state)|{'verification':result['verification'],'native_reopen':result['native_reopen'],
                'native_execution':result['native_execution'],'append_request':pending['bridge_request'],'buffer_restarted':True,
                'previous_native_pid':state.get('native_pid')}
            next_state['requests'][pending['id']]={'hash':pending['hash'],'receipt':receipt}
            self._write_state(folder,next_state)
        except Exception:self._stop(next_state);raise
        state.clear();state.update(next_state)
        return receipt

    def execute(self,action:str,workspace_id:str,parameters:dict,expected_revision:int|None=None,request_id:str|None=None):
        with self.lock:
            if not isinstance(parameters,dict):raise BridgeError('WORKSPACE_PARAMETERS')
            if action=='open':
                if set(parameters)!={'source','expected_sha256'}:raise BridgeError('WORKSPACE_SOURCE')
                source = Path(parameters['source']).resolve(strict=True)
                before,reader,_ = read_native(source)
                if sha(before)!=parameters['expected_sha256']:raise BridgeError('STALE_SOURCE')
                folder = self._folder(workspace_id)
                if (folder/'state.json').exists():
                    _,state = self._load(workspace_id)
                    if state['source']!=str(source) or state['source_sha256']!=sha(before):raise BridgeError('WORKSPACE_ID_CONFLICT')
                    result=self._public(state)
                    if state['state']=='CLOSED':self._release(workspace_id)
                    return result
                backend_configuration(self.bridge.root)
                folder.mkdir(parents=True,exist_ok=False)
                self._claim(folder,workspace_id)
                checkpoint = folder/source.name; checkpoint.write_bytes(before)
                state={'id':workspace_id,'source':str(source),'source_sha256':sha(before),
                    'checkpoint':str(checkpoint),'checkpoint_sha256':sha(before),'revision':0,'dirty':False,
                    'active':len(reader.geom),'native_type':reader.shapeType,'state':'LOST','requests':{},'pending':None,
                    'saved_revision':0,'saved_checkpoint':str(checkpoint),'saved_checkpoint_sha256':sha(before)}
                self._write_state(folder,state)
                self._start(folder,state)
                result=self._public(state)
                if state['state']=='CLOSED':self._release(workspace_id)
                return result
            folder,state = self._load(workspace_id)
            if action=='status':
                if parameters:raise BridgeError('WORKSPACE_PARAMETERS')
                if (state.get('pending') is None and workspace_id in self.buffers
                        and self.buffers[workspace_id].process.poll() is None):
                    observed = self.buffers[workspace_id].ask('status')
                    if observed['revision']!=state['revision'] or observed['active']!=state['active']:
                        raise BridgeError('WORKSPACE_STATE_MISMATCH')
                result=self._public(state)
                if state['state']=='CLOSED':self._release(workspace_id)
                return result
            if type(expected_revision) is not int or expected_revision!=state['revision']:
                # Completed request retries are checked before revision rejection below.
                if action not in ('edit','save') or not request_id or request_id not in state['requests']:
                    raise BridgeError('WORKSPACE_REVISION_CONFLICT')
            if action=='recover':
                if parameters:raise BridgeError('WORKSPACE_PARAMETERS')
                self._retire_orphan(state)
                pending = state.get('pending')
                resolution = 'last_complete_checkpoint'
                if pending:
                    try:
                        if pending['action']=='edit':
                            self._commit_edit(folder,state,pending,self._validate_checkpoint(state,pending))
                        elif pending['action']=='append':
                            self._commit_append(folder,state,pending,self._append_result(pending))
                        else:
                            result = self.bridge.status(pending['bridge_request'])
                            if result['state']=='PREPARED':result=self.bridge.apply(pending['bridge_request'])
                            if result['state']!='APPLIED' or not result.get('output_matches',Path(result['output']).exists()):
                                raise BridgeError('WORKSPACE_SAVE_INCOMPLETE')
                            self._commit_save(folder,state,pending,result)
                        resolution = 'reconciled_without_replay'
                    except (BridgeError,FileNotFoundError,ValueError) as exc:
                        state['requests'][pending['id']]={'hash':pending['hash'],'failure':str(exc)}
                        state.update(pending=None)
                        resolution = 'failed_request_rolled_back_to_checkpoint'
                if workspace_id not in self.buffers:self._start(folder,state)
                return self._public(state) | {'recovery':resolution}
            if state.get('pending'):raise BridgeError('WORKSPACE_PENDING: recover before another edit')
            if action=='close':
                if set(parameters)-{'discard'} or ('discard' in parameters and type(parameters['discard']) is not bool):
                    raise BridgeError('WORKSPACE_PARAMETERS')
                if state['dirty'] and not parameters.get('discard'):raise BridgeError('WORKSPACE_DIRTY: save or explicitly discard')
                self._stop(state)
                if state['dirty']:
                    state.update(checkpoint=state['saved_checkpoint'],checkpoint_sha256=state['saved_checkpoint_sha256'],
                        revision=state['revision']+1,dirty=False)
                    state['active']=len(read_native(Path(state['checkpoint']))[1].geom)
                state.update(state='CLOSED')
                self._write_state(folder,state)
                self._release(workspace_id)
                return self._public(state)
            if action not in ('edit','save'):raise BridgeError('WORKSPACE_ACTION: open/status/edit/save/close/recover')
            request_id = self.bridge.request_id(request_id)
            payload_hash = sha(json_text({'action':action,'parameters':parameters,'revision':expected_revision}).encode())
            existing = state['requests'].get(request_id)
            if existing:
                if existing['hash']!=payload_hash:raise BridgeError('REQUEST_ID_CONFLICT')
                if 'failure' in existing:raise BridgeError('WORKSPACE_REQUEST_FAILED: use a new request ID after recovery')
                return existing['receipt'] | {'reconciled_existing_request':True,'replayed':False,
                    'current_workspace':self._public(state),'receipt_scope':'historical completed request; current_workspace is live'}
            if expected_revision!=state['revision']:raise BridgeError('WORKSPACE_REVISION_CONFLICT')
            buffer = self.buffers.get(workspace_id)
            if buffer is None or buffer.process.poll() is not None:raise BridgeError('WORKSPACE_LOST: recover first')
            pending={'action':action,'id':request_id,'hash':payload_hash,'token':sha(request_id.encode())[:32]}
            if action=='edit':
                if set(parameters)!={'operation','parameters'}:raise BridgeError('WORKSPACE_PARAMETERS')
                operation = parameters['operation']
                if operation not in ('point.move','point.radius','point.style','point.text','line.replace','line.smooth','line.resize','line.extend_to','arc.replace','arc.smooth','node.move','feature.delete','feature.append'):raise BridgeError('WORKSPACE_EDIT_OPERATION')
                checked = _parameters(operation,parameters['parameters'],read_native(Path(state['checkpoint']))[1],Path(state['checkpoint']))
                if operation=='feature.append':
                    from .native_append import prepare_native_append
                    pending.update(action='append',operation=operation,parameters=checked,
                        bridge_request='workspace-append-'+sha((workspace_id+'\0'+request_id).encode())[:32])
                    state['pending']=pending;self._write_state(folder,state)
                    try:
                        prepared=prepare_native_append(self.bridge,checked['file'],checked['expected_sha256'],checked['record_ids'],
                            {'name':'appended'+Path(state['checkpoint']).suffix.lower()},pending['bridge_request'],
                            {'file':state['checkpoint'],'expected_sha256':state['checkpoint_sha256']})
                        return self._commit_append(folder,state,pending,prepared)
                    except Exception:
                        self._stop(state);state['state']='LOST';self._write_state(folder,state);raise
                pending.update(operation=operation,parameters=checked,native_pid=buffer.process.pid,
                    candidate=str(folder/pending['token']/Path(state['checkpoint']).name))
                command = _native_commands(operation, checked, Path(state['checkpoint']))
                state['pending']=pending;self._write_state(folder,state)
                try:
                    response=buffer.ask(_native_opcode(operation),pending['token'],command)
                    if response['revision']!=state['revision']+1:raise BridgeError('WORKSPACE_NATIVE_REVISION')
                    return self._commit_edit(folder,state,pending,self._validate_checkpoint(state,pending))
                except Exception:
                    self._stop(state);state['state']='LOST';self._write_state(folder,state);raise
            if parameters:raise BridgeError('WORKSPACE_PARAMETERS')
            pending['bridge_request']='workspace-'+sha((workspace_id+'\0'+request_id).encode())[:32]
            state['pending']=pending;self._write_state(folder,state)
            prepared=prepare_native_operation(self.bridge,'resave',state['checkpoint'],state['checkpoint_sha256'],{},pending['bridge_request'])
            if prepared['state']!='PREPARED' and prepared['state']!='APPLIED':raise BridgeError('WORKSPACE_SAVE_PENDING: recover')
            result=self.bridge.apply(pending['bridge_request'])
            return self._commit_save(folder,state,pending,result)

    def close_all(self):
        with self.lock:
            for identity,buffer in list(self.buffers.items()):
                try:buffer.close()
                finally:self.buffers.pop(identity,None)
            for identity in list(self.leases):self._release(identity)
