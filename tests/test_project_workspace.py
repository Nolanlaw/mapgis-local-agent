from pathlib import Path
import shutil

import pytest

from mapgis_agent.core import BridgeError,read_native,sha
from mapgis_agent.project_workspace import ProjectWorkspaces
from mapgis_agent.workspace import NativeWorkspaces
from mapgis_agent.native_project import native_project_info
from test_native_project_edit import project_case


def test_project_workspace_binds_multilayer_selection_dirty_edits_and_atomic_native_save(tmp_path):
    bridge,source,_=project_case(tmp_path);before=source.read_bytes()
    leaves=NativeWorkspaces(bridge);projects=ProjectWorkspaces(bridge,leaves)
    try:
        opened=projects.execute('open','map',{'file':str(source),'expected_sha256':sha(before)})
        assert opened['revision']==0 and not opened['dirty'] and len(opened['layers'])==3
        with pytest.raises(BridgeError,match='PROJECT_SELECTION_STALE'):
            projects.execute('select','map',{'mode':'replace','layers':[{'index':1,'record_ids':[1],'expected_checkpoint_sha256':'0'*64}]},0,'stale-query')
        selected=projects.execute('select','map',{'mode':'replace','layers':[
            {'index':1,'record_ids':[1],'expected_checkpoint_sha256':opened['layers'][0]['checkpoint_sha256']},
            {'index':2,'record_ids':[1]}]},0,'select')
        assert selected['selected_count']==2
        projects.execute('select','map',{'mode':'remove','layers':[{'index':2,'record_ids':[1]}]},1,'remove')
        projects.execute('set_layer','map',{'index':1,'state':2},2,'editable')
        edited=projects.execute('edit','map',{'index':1,'use_selection':True,'operation':'point.move',
            'parameters':{'updates':[{'record_id':1,'x':8,'y':9}]}},3,'move')
        assert edited['dirty'] and edited['selection']=={'1':[1]}
        assert edited['layers'][0]['native_pid']==opened['layers'][0]['native_pid']
        saved=projects.execute('save','map',{},4,'save-map')
        assert not saved['dirty'] and saved['revision']==5
        actual=native_project_info(tmp_path,saved['saved_project']['file'],saved['saved_project']['sha256'])
        assert actual['layers'][0]['state']==2
        point=read_native(Path(actual['layers'][0]['file']))[1].geom[0]
        assert (point.x,point.y)==(8,9) and source.read_bytes()==before
        assert projects.execute('save','map',{},4,'save-map')['reconciled_existing_request']
        closed=projects.execute('close','map',{},5)
        recovered=projects.execute('recover','map',{},closed['revision'])
        assert not recovered['dirty'] and recovered['saved_project']==saved['saved_project']
        projects.execute('close','map',{},recovered['revision'])
    finally:leaves.close_all()


def test_project_workspace_distinguishes_identical_names_hashes_and_selection_contexts(tmp_path):
    bridge,source,_=project_case(tmp_path)
    other=tmp_path/'work/other';shutil.copytree(source.parent,other);twin=other/source.name
    assert twin.name==source.name and twin.read_bytes()==source.read_bytes()
    leaves=NativeWorkspaces(bridge);projects=ProjectWorkspaces(bridge,leaves)
    try:
        first=projects.execute('open','first',{'file':str(source),'expected_sha256':sha(source.read_bytes())})
        second=projects.execute('open','second',{'file':str(twin),'expected_sha256':sha(twin.read_bytes())})
        assert first['opened_source']['file']!=second['opened_source']['file']
        with pytest.raises(BridgeError,match='PROJECT_SESSION_ID_CONFLICT'):
            projects.execute('open','first',{'file':str(twin),'expected_sha256':sha(twin.read_bytes())})
        projects.execute('select','first',{'mode':'replace','layers':[{'index':1,'record_ids':[1]}]},0,'selected')
        assert projects.execute('status','second',{})['selected_count']==0
        with pytest.raises(BridgeError,match='PROJECT_SESSION_REVISION'):
            projects.execute('select','first',{'mode':'replace','layers':[]},0,'stale')
        projects.execute('close','first',{},1);projects.execute('close','second',{},0)
    finally:leaves.close_all()


def test_project_environment_dirty_save_discard_and_recover(tmp_path):
    bridge,source,_=project_case(tmp_path);before=source.read_bytes()
    leaves=NativeWorkspaces(bridge);projects=ProjectWorkspaces(bridge,leaves)
    try:
        opened=projects.execute('open','environment',{'file':str(source),'expected_sha256':sha(before)})
        original=opened['environment']
        changed=projects.execute('set_environment','environment',
            {'translation':[4,5],'scale':[2,1],'rotation_degrees':30},0,'layout')
        assert changed['dirty'] and changed['environment']['translation']==[4,5]
        with pytest.raises(BridgeError,match='PROJECT_SESSION_DIRTY'):
            projects.execute('close','environment',{},1)
        closed=projects.execute('close','environment',{'discard':True},1)
        assert not closed['dirty'] and closed['environment']==original
        reopened=projects.execute('recover','environment',{},2)
        projects.execute('set_environment','environment',{'translation':[7,8]},3,'retained')
        saved=projects.execute('save','environment',{},4,'save-environment')
        assert not saved['dirty'] and saved['environment']['translation']==[7,8]
        actual=native_project_info(tmp_path,saved['saved_project']['file'],saved['saved_project']['sha256'])
        assert actual['environment']==saved['environment']
        projects.execute('close','environment',{},5)
        recovered=projects.execute('recover','environment',{},6)
        assert recovered['environment']==saved['environment'] and not recovered['dirty']
        assert source.read_bytes()==before
        projects.execute('close','environment',{},7)
    finally:leaves.close_all()


def test_project_native_child_loss_preserves_dirty_checkpoint_and_selection_binding(tmp_path):
    bridge,source,_=project_case(tmp_path);before=source.read_bytes()
    leaves=NativeWorkspaces(bridge);projects=ProjectWorkspaces(bridge,leaves)
    try:
        projects.execute('open','loss',{'file':str(source),'expected_sha256':sha(before)})
        projects.execute('select','loss',{'mode':'replace','layers':[{'index':1,'record_ids':[1]}]},0,'select')
        projects.execute('set_layer','loss',{'index':1,'state':2},1,'editable')
        with pytest.raises(BridgeError,match='PROJECT_SELECTION_BINDING'):
            projects.execute('edit','loss',{'index':1,'use_selection':True,'operation':'point.move',
                'parameters':{'updates':[{'record_id':99,'x':8,'y':9}]}},2,'wrong')
        edited=projects.execute('edit','loss',{'index':1,'use_selection':True,'operation':'point.move',
            'parameters':{'updates':[{'record_id':1,'x':8,'y':9}]}},2,'move')
        state,_=projects._read('loss');child=leaves.buffers[state['layers'][0]['workspace_id']].process
        old_pid=child.pid;child.kill();child.wait(timeout=5)
        recovered=projects.execute('recover','loss',{},3)
        assert recovered['dirty'] and recovered['selection']=={'1':[1]}
        assert recovered['layers'][0]['native_pid']!=old_pid
        point=read_native(Path(recovered['layers'][0]['checkpoint']))[1].geom[0]
        assert (point.x,point.y)==(8,9)
        closed=projects.execute('close','loss',{'discard':True},4)
        restored=projects.execute('recover','loss',{},closed['revision'])
        assert not restored['dirty'] and restored['selected_count']==0
        point=read_native(Path(restored['layers'][0]['checkpoint']))[1].geom[0]
        assert (point.x,point.y)==(1,2) and source.read_bytes()==before
        projects.execute('close','loss',{},restored['revision'])
    finally:leaves.close_all()


def test_partial_project_open_can_close_owned_buffers_and_recover_without_leaking_children(tmp_path,monkeypatch):
    bridge,source,_=project_case(tmp_path);before=source.read_bytes()
    leaves=NativeWorkspaces(bridge);projects=ProjectWorkspaces(bridge,leaves);real=leaves.execute;opened=0
    def fail_second(action,*args,**kwargs):
        nonlocal opened
        if action=='open':
            opened+=1
            if opened==2:raise BridgeError('owned fixture second native launch unavailable')
        return real(action,*args,**kwargs)
    monkeypatch.setattr(leaves,'execute',fail_second)
    try:
        with pytest.raises(BridgeError,match='second native launch'):
            projects.execute('open','partial',{'file':str(source),'expected_sha256':sha(before)})
        assert projects.execute('status','partial',{})['state']=='OPENING' and len(leaves.buffers)==1
        closed=projects.execute('close','partial',{'discard':True},0)
        assert closed['state']=='CLOSED' and not closed['dirty'] and not leaves.buffers
        monkeypatch.setattr(leaves,'execute',real)
        recovered=projects.execute('recover','partial',{},closed['revision'])
        assert len(recovered['layers'])==3 and not recovered['dirty'] and source.read_bytes()==before
        projects.execute('close','partial',{},recovered['revision'])
    finally:leaves.close_all()


def test_project_save_parent_interruption_recovers_published_branch_without_replaying_edit(tmp_path,monkeypatch):
    import mapgis_agent.project_workspace as module
    bridge,source,_=project_case(tmp_path);before=source.read_bytes()
    leaves=NativeWorkspaces(bridge);projects=ProjectWorkspaces(bridge,leaves)
    try:
        projects.execute('open','recover',{'file':str(source),'expected_sha256':sha(before)})
        projects.execute('set_layer','recover',{'index':1,'state':2},0,'editable')
        edited=projects.execute('edit','recover',{'index':1,'operation':'point.move',
            'parameters':{'updates':[{'record_id':1,'x':8,'y':9}]}},1,'move')
        with pytest.raises(BridgeError,match='PROJECT_SESSION_DIRTY'):
            projects.execute('close','recover',{},2)
        original=projects._rebind
        monkeypatch.setattr(projects,'_rebind',lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('owned test parent interrupted after publish')))
        with pytest.raises(RuntimeError,match='after publish'):
            projects.execute('save','recover',{},2,'save')
        monkeypatch.setattr(projects,'_rebind',original)
        monkeypatch.setattr(module,'prepare_project_edit',lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('save replayed')))
        recovered=projects.execute('recover','recover',{},2)
        assert not recovered['dirty'] and recovered['revision']==3
        assert projects.execute('save','recover',{},2,'save')['reconciled_existing_request']
        actual=native_project_info(tmp_path,recovered['saved_project']['file'],recovered['saved_project']['sha256'])
        point=read_native(Path(actual['layers'][0]['file']))[1].geom[0]
        assert (point.x,point.y)==(8,9) and source.read_bytes()==before
        projects.execute('close','recover',{},3)
    finally:leaves.close_all()
