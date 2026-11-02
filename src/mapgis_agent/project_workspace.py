"""Agent-owned MPJ context composed from the existing native file buffers.

Project identity, selections and revisions are durable in the existing SQLite
database. Vector edits reuse NativeWorkspaces. A save publishes one portable MPJ
artifact set, then rebinds clean buffers to that verified publication. No external
editor memory, global current project, new daemon or input automation is used.
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
import threading

from .core import Bridge,BridgeError,json_text,native_ids,read_native,sha
from .artifacts import reconcile_bundle
from .native_project import native_project_info,prepare_project_edit,_environment
from .workspace import NativeWorkspaces
from .native_backend import _parameters


class ProjectWorkspaces:
    def __init__(self,bridge:Bridge,buffers:NativeWorkspaces):
        self.bridge=bridge;self.buffers=buffers;self.lock=threading.RLock()
        with bridge.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS project_workspaces (id TEXT PRIMARY KEY,state TEXT NOT NULL)')

    def _read(self,identity):
        self.bridge.request_id(identity)
        with self.bridge.connect() as db:row=db.execute('SELECT state FROM project_workspaces WHERE id=?',(identity,)).fetchone()
        if row is None:raise BridgeError('PROJECT_SESSION_UNKNOWN')
        state=json.loads(row['state'])
        if 'environment' not in state:
            project=state['saved_project']
            info=native_project_info(self.bridge.root,project['file'],project['sha256'])
            state.update(environment=info['environment'],saved_environment=info['environment'])
        return state,row['state']

    def _store(self,state,previous=None):
        raw=json_text(state)
        with self.bridge.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if previous is None:
                try:db.execute('INSERT INTO project_workspaces VALUES (?,?)',(state['id'],raw))
                except sqlite3.IntegrityError as exc:raise BridgeError('PROJECT_SESSION_CONCURRENT_OPEN') from exc
            elif db.execute('UPDATE project_workspaces SET state=? WHERE id=? AND state=?',(raw,state['id'],previous)).rowcount!=1:
                raise BridgeError('PROJECT_SESSION_CHANGED: read status before another mutation')
        return raw

    @staticmethod
    def _settings(layers):return [[x['index'],x['state'],x['display_limits']] for x in layers]

    @staticmethod
    def _buffer_id(state,index):return 'pw-'+sha(json_text([state['id'],state['generation'],index]).encode())[:24]

    def _public(self,state):
        layers=[];dirty=(self._settings(state['layers'])!=state['saved_settings'] or
                        state['environment']!=state['saved_environment'])
        for layer in state['layers']:
            item={key:layer[key] for key in ('index','kind','file','sha256','state','display_limits')}
            if layer.get('workspace_id'):
                current=self.buffers.execute('status',layer['workspace_id'],{})
                item.update(checkpoint=current['checkpoint'],checkpoint_sha256=current['checkpoint_sha256'],
                            buffer_revision=current['revision'],dirty=current['dirty'],native_pid=current['native_pid'],native_state=current['state'])
                dirty|=current['dirty']
            layers.append(item)
        return {'id':state['id'],'state':state['state'],'revision':state['revision'],'dirty':bool(dirty),
                'opened_source':state['opened_source'],'saved_project':state['saved_project'],
                'layers':layers,'selection':state['selection'],'environment':state['environment'],
                'selected_count':sum(len(ids) for ids in state['selection'].values()),
                'pending_request':(state.get('pending') or {}).get('id'),
                'scope':'agent-owned multi-layer project and buffers; external GUI state is independent','replayed':False}

    def _open_buffers(self,state,info):
        layers=[]
        for layer in info['layers']:
            item=dict(layer)
            if layer['kind'] in (0,1,2):
                identity=self._buffer_id(state,layer['index'])
                current=self.buffers.execute('open',identity,{'source':layer['file'],'expected_sha256':layer['sha256']})
                if current['state']=='LOST':current=self.buffers.execute('recover',identity,{},current['revision'])
                if current['state']=='CLOSED':raise BridgeError('PROJECT_SESSION_BUFFER_CLOSED')
                item.update(workspace_id=identity,buffer_revision=current['revision'])
            layers.append(item)
        return layers

    def _close_buffers(self,layers):
        for layer in layers:
            identity=layer.get('workspace_id')
            if identity is None:continue
            current=self.buffers.execute('status',identity,{})
            if current['state']=='CLOSED':continue
            if current['state']=='LOST':current=self.buffers.execute('recover',identity,{},current['revision'])
            self.buffers.execute('close',identity,{'discard':True},current['revision'])

    def _prune_selection(self,state):
        selection={}
        for layer in state['layers']:
            old=state['selection'].get(str(layer['index']),[])
            if old and layer.get('workspace_id'):
                current=self.buffers.execute('status',layer['workspace_id'],{})
                active=set(native_ids(read_native(Path(current['checkpoint']))[1]))
                keep=sorted(active.intersection(old))
                if keep:selection[str(layer['index'])]=keep
        state['selection']=selection

    def _rebind(self,state,result):
        published=self.bridge.status(result['id'])
        if published['state']!='APPLIED' or not published.get('output_matches'):raise BridgeError('PROJECT_SESSION_PUBLICATION_CHANGED')
        info=native_project_info(self.bridge.root,result['output'],result['candidate_sha256'])
        self._close_buffers(state['layers'])
        rebound={**state,'generation':state['generation']+1}
        layers=self._open_buffers(rebound,info)
        state.update(generation=rebound['generation'],layers=layers,
                     saved_project={'file':result['output'],'sha256':result['candidate_sha256']},
                     saved_settings=self._settings(layers),saved_selection=state['selection'],state='OPEN',
                     environment=info['environment'],saved_environment=info['environment'])
        self._prune_selection(state)

    def _commit(self,state,previous):
        pending=state['pending'];state['pending']=None;state['revision']=pending['revision']+1
        result=self._public(state)
        state['requests'][pending['id']]={'hash':pending['hash'],'receipt':result}
        self._store(state,previous)
        return result

    def _resume(self,state,previous):
        pending=state['pending'];parameters=pending['parameters']
        if pending['action']=='edit':
            layer=state['layers'][parameters['index']-1]
            current=self.buffers.execute('status',layer['workspace_id'],{})
            if current['state']=='LOST' or current['pending_request']:
                self.buffers.execute('recover',layer['workspace_id'],{},current['revision'])
            result=self.buffers.execute('edit',layer['workspace_id'],
                {'operation':parameters['operation'],'parameters':pending['edit_parameters']},
                pending['buffer_revision'],pending['native_request'])
            layer['buffer_revision']=result['revision'];self._prune_selection(state)
            return self._commit(state,previous)
        if 'result' not in pending:
            plan=[]
            for layer in state['layers']:
                choice={'source_index':layer['index'],'state':layer['state'],'display_limits':layer['display_limits']}
                if layer.get('workspace_id'):
                    current=self.buffers.execute('status',layer['workspace_id'],{})
                    if current['revision']!=layer['buffer_revision'] or current['pending_request']:raise BridgeError('PROJECT_LAYER_REVISION_CHANGED')
                    choice['replacement']={'file':current['checkpoint'],'expected_sha256':current['checkpoint_sha256']}
                plan.append(choice)
            project=state['saved_project']
            prepared=prepare_project_edit(self.bridge,project['file'],project['sha256'],plan,pending['native_request'],environment=state['environment'])
            if prepared['state'] in ('RUNNING','FAILED'):
                prepared=reconcile_bundle(self.bridge,pending['native_request'])
            if prepared['state'] not in ('PREPARED','APPLIED'):raise BridgeError('PROJECT_SAVE_INCOMPLETE')
            result=self.bridge.apply(pending['native_request'])
            pending['result']=result;previous=self._store(state,previous)
        self._rebind(state,pending['result'])
        return self._commit(state,previous)

    def execute(self,action:str,workspace_id:str,parameters:dict,expected_revision:int|None=None,request_id:str|None=None):
        with self.lock:
            self.bridge.request_id(workspace_id)
            if not isinstance(parameters,dict):raise BridgeError('PROJECT_SESSION_PARAMETERS')
            if action=='open':
                if set(parameters)!={'file','expected_sha256'}:raise BridgeError('PROJECT_SESSION_SOURCE')
                source=self.bridge.owned_path(parameters['file']);binding={'file':str(source),'sha256':parameters['expected_sha256']}
                with self.bridge.connect() as db:exists=db.execute('SELECT 1 FROM project_workspaces WHERE id=?',(workspace_id,)).fetchone()
                if exists:
                    state,previous=self._read(workspace_id)
                    if state['opened_source']!=binding:raise BridgeError('PROJECT_SESSION_ID_CONFLICT')
                    return self._public(state)
                info=native_project_info(self.bridge.root,str(source),binding['sha256'])
                state={'id':workspace_id,'state':'OPENING','revision':0,'generation':0,'opened_source':binding,
                       'saved_project':binding,'selection':{},'saved_selection':{},'layers':info['layers'],
                       'saved_settings':self._settings(info['layers']),'environment':info['environment'],
                       'saved_environment':info['environment'],'pending':None,'requests':{}}
                previous=self._store(state)
                state['layers']=self._open_buffers(state,info);state['saved_settings']=self._settings(state['layers']);state['state']='OPEN'
                self._store(state,previous);return self._public(state)
            state,previous=self._read(workspace_id)
            if action=='status':
                if parameters:raise BridgeError('PROJECT_SESSION_PARAMETERS')
                return self._public(state)
            if action=='recover':
                if parameters or type(expected_revision) is not int or expected_revision!=state['revision']:raise BridgeError('PROJECT_SESSION_REVISION')
                if state.get('pending'):
                    pending_id=state['pending']['id']
                    try:return self._resume(state,previous)
                    except BridgeError as exc:
                        state,previous=self._read(workspace_id);pending=state['pending']
                        if pending is None or pending['id']!=pending_id:raise
                        # A verified published bundle must finish rebinding.
                        # Earlier failures leave the last complete native
                        # checkpoints intact and permit a new request ID.
                        if pending.get('result'):raise
                        for layer in state['layers']:
                            if layer.get('workspace_id'):
                                current=self.buffers.execute('status',layer['workspace_id'],{})
                                if current['state']=='LOST' or current['pending_request']:
                                    current=self.buffers.execute('recover',layer['workspace_id'],{},current['revision'])
                                layer['buffer_revision']=current['revision']
                        state['requests'][pending['id']]={'hash':pending['hash'],'failure':str(exc)}
                        state['pending']=None;state['revision']+=1;self._prune_selection(state);self._store(state,previous)
                        return self._public(state)|{'failed_request':pending['id'],'recovery':'last_complete_buffers_and_last_published_project','failure':str(exc)}
                if state['state'] in ('CLOSED','OPENING'):
                    if state['state']=='CLOSED':
                        info=native_project_info(self.bridge.root,state['saved_project']['file'],state['saved_project']['sha256']);state['generation']+=1
                    else:
                        if sha(Path(state['saved_project']['file']).read_bytes())!=state['saved_project']['sha256']:raise BridgeError('STALE_SOURCE')
                        info={'layers':state['layers']}
                    state['layers']=self._open_buffers(state,info);state['selection']=state['saved_selection'];state['saved_settings']=self._settings(state['layers'])
                else:
                    for layer in state['layers']:
                        if layer.get('workspace_id'):
                            current=self.buffers.execute('status',layer['workspace_id'],{})
                            recovered=self.buffers.execute('recover',layer['workspace_id'],{},current['revision'])
                            layer['buffer_revision']=recovered['revision']
                    self._prune_selection(state)
                state['state']='OPEN';state['revision']+=1;self._store(state,previous);return self._public(state)
            if action in ('edit','save','select','set_layer','set_environment'):
                request_id=self.bridge.request_id(request_id)
                fingerprint=sha(json_text({'action':action,'parameters':parameters,'revision':expected_revision}).encode())
                old=state['requests'].get(request_id)
                if old:
                    if old['hash']!=fingerprint:raise BridgeError('REQUEST_ID_CONFLICT')
                    if 'failure' in old:raise BridgeError('PROJECT_SESSION_REQUEST_FAILED: use a new ID after recovery; '+old['failure'])
                    return old['receipt']|{'reconciled_existing_request':True,'current_workspace':self._public(state)}
            if state.get('pending'):raise BridgeError('PROJECT_SESSION_PENDING: recover the durable pending request')
            if type(expected_revision) is not int or expected_revision!=state['revision']:raise BridgeError('PROJECT_SESSION_REVISION')
            if state['state']=='OPENING' and action=='close' and parameters.get('discard') is True:
                if set(parameters)!={'discard'}:raise BridgeError('PROJECT_SESSION_PARAMETERS')
                for layer in state['layers']:
                    if layer['kind'] not in (0,1,2):continue
                    identity=self._buffer_id(state,layer['index'])
                    if (self.buffers._folder(identity)/'state.json').is_file():layer['workspace_id']=identity
                self._close_buffers(state['layers']);state['state']='CLOSED';state['revision']+=1
                self._store(state,previous);return self._public(state)
            if state['state']!='OPEN':raise BridgeError('PROJECT_SESSION_NOT_OPEN')
            if action=='close':
                if set(parameters)-{'discard'} or ('discard' in parameters and type(parameters['discard']) is not bool):raise BridgeError('PROJECT_SESSION_PARAMETERS')
                if self._public(state)['dirty'] and not parameters.get('discard'):raise BridgeError('PROJECT_SESSION_DIRTY')
                self._close_buffers(state['layers']);state['state']='CLOSED';state['revision']+=1
                # Closed state represents the last published project, including
                # layer settings/selection when unsaved work was discarded.
                state['selection']=state['saved_selection']
                state['environment']=state['saved_environment']
                for layer,settings in zip(state['layers'],state['saved_settings']):layer['state'],layer['display_limits']=settings[1:]
                self._store(state,previous);return self._public(state)
            if action not in ('edit','save','select','set_layer','set_environment'):raise BridgeError('PROJECT_SESSION_ACTION')
            pending={'id':request_id,'hash':fingerprint,'revision':state['revision'],'action':action,'parameters':parameters,
                     'native_request':'ps-'+sha(json_text([workspace_id,request_id,action]).encode())[:28]}
            if action=='save':
                if parameters:raise BridgeError('PROJECT_SESSION_PARAMETERS')
            elif action=='set_environment':
                if not parameters:raise BridgeError('PROJECT_ENVIRONMENT_PARAMETERS')
                state['environment']=_environment(parameters,state['environment'])
            elif action=='select':
                if set(parameters)!={'mode','layers'} or parameters['mode'] not in ('replace','add','remove','intersect','xor') or not isinstance(parameters['layers'],list):
                    raise BridgeError('PROJECT_SELECTION_PARAMETERS')
                chosen={};seen=set()
                for selection in parameters['layers']:
                    if not isinstance(selection,dict) or not {'index','record_ids'}<=set(selection) or set(selection)-{'index','record_ids','expected_checkpoint_sha256'}:raise BridgeError('PROJECT_SELECTION_PARAMETERS')
                    index=selection['index'];ids=selection['record_ids']
                    if type(index) is not int or not 1<=index<=len(state['layers']) or index in seen:raise BridgeError('PROJECT_LAYER_INDEX')
                    seen.add(index);layer=state['layers'][index-1]
                    if not layer.get('workspace_id') or layer['state']==0:raise BridgeError('PROJECT_SELECTION_LAYER')
                    current=self.buffers.execute('status',layer['workspace_id'],{})
                    if ('expected_checkpoint_sha256' in selection
                            and selection['expected_checkpoint_sha256']!=current['checkpoint_sha256']):raise BridgeError('PROJECT_SELECTION_STALE: query snapshot differs from current layer buffer')
                    active=set(native_ids(read_native(Path(current['checkpoint']))[1]))
                    if not isinstance(ids,list) or any(type(i) is not int for i in ids) or len(ids)!=len(set(ids)) or not set(ids)<=active:
                        raise BridgeError('PROJECT_SELECTION_IDS')
                    chosen[str(index)]=set(ids)
                old={key:set(value) for key,value in state['selection'].items()};mode=parameters['mode']
                if mode=='replace':combined=chosen
                else:
                    combined={}
                    for key in old.keys()|chosen.keys():
                        a,b=old.get(key,set()),chosen.get(key,set())
                        combined[key]={'add':a|b,'remove':a-b,'intersect':a&b,'xor':a^b}[mode]
                state['selection']={key:sorted(value) for key,value in combined.items() if value}
            else:
                index=parameters.get('index')
                if type(index) is not int or not 1<=index<=len(state['layers']):raise BridgeError('PROJECT_LAYER_INDEX')
                layer=state['layers'][index-1]
                if action=='set_layer':
                    if not {'state','display_limits'}.intersection(parameters) or set(parameters)-{'index','state','display_limits'}:raise BridgeError('PROJECT_LAYER_PARAMETERS')
                    value=parameters.get('state',layer['state']);limits=parameters.get('display_limits',layer['display_limits'])
                    if type(value) is not int or not 0<=value<=(1 if layer['kind']==11 else 3):raise BridgeError('PROJECT_NATIVE_STATE')
                    if not isinstance(limits,list) or len(limits)!=2 or any(type(x) not in (int,float) or not math.isfinite(x) for x in limits) or limits[1]>0 and limits[0]>limits[1]:raise BridgeError('PROJECT_DISPLAY_LIMITS')
                    if value==3:
                        for sibling in state['layers']:
                            if sibling['kind']==layer['kind'] and sibling['state']==3:sibling['state']=2
                    layer.update(state=value,display_limits=limits)
                    if value==0:state['selection'].pop(str(index),None)
                else:
                    if set(parameters)-{'index','operation','parameters','use_selection'} or not {'operation','parameters'}<=set(parameters):raise BridgeError('PROJECT_EDIT_PARAMETERS')
                    if not layer.get('workspace_id') or layer['state']<2:raise BridgeError('PROJECT_LAYER_NOT_EDITABLE')
                    if 'use_selection' in parameters and type(parameters['use_selection']) is not bool:raise BridgeError('PROJECT_EDIT_PARAMETERS')
                    if not isinstance(parameters['parameters'],dict):raise BridgeError('PROJECT_EDIT_PARAMETERS')
                    edit=dict(parameters['parameters']);operation=parameters['operation']
                    if parameters.get('use_selection'):
                        selected=set(state['selection'].get(str(index),[]))
                        if not selected:raise BridgeError('PROJECT_SELECTION_EMPTY')
                        if operation=='feature.delete':
                            if edit:raise BridgeError('PROJECT_SELECTION_DELETE_PARAMETERS: use an empty parameter object')
                            edit={'record_ids':sorted(selected)}
                        elif operation in ('point.move','line.replace'):
                            if not {u.get('record_id') for u in edit.get('updates',[])}<=selected:raise BridgeError('PROJECT_SELECTION_BINDING')
                        else:raise BridgeError('PROJECT_SELECTION_BINDING: arc/node edits use their explicit topology IDs')
                    current=self.buffers.execute('status',layer['workspace_id'],{})
                    if current['revision']!=layer['buffer_revision'] or current['pending_request'] or current['state']!='OPEN':raise BridgeError('PROJECT_LAYER_REVISION_CHANGED: recover first')
                    _parameters(operation,edit,read_native(Path(current['checkpoint']))[1],Path(current['checkpoint']))
                    pending.update(edit_parameters=edit,buffer_revision=layer['buffer_revision'])
            state['pending']=pending
            if action in ('select','set_layer','set_environment'):return self._commit(state,previous)
            previous=self._store(state,previous)
            return self._resume(state,previous)
