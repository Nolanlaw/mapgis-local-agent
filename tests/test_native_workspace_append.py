"""Append composes with dirty checkpoints, CAS and native process recovery."""
from pathlib import Path
import pytest
from mapgis_agent.core import BridgeError,read_native,native_ids
from mapgis_agent.raster import file_sha
from mapgis_agent.workspace import NativeWorkspaces
from mapgis_agent.native_backend import native_file_info
from test_native_append import case


def opened(tmp_path,kind='wt'):
    bridge,source,base=case(tmp_path,kind,aux=kind=='wp');work=NativeWorkspaces(bridge)
    state=work.execute('open','buffer',{'source':str(base),'expected_sha256':file_sha(base)})
    dirty=work.execute('edit','buffer',{'operation':'feature.delete','parameters':{'record_ids':[1]}},0,'delete')
    assert dirty['dirty'] and dirty['revision']==1 and dirty['active']==1
    arguments={'operation':'feature.append','parameters':{'file':str(source),'expected_sha256':file_sha(source),'record_ids':[2,4]}}
    return bridge,source,base,work,dirty,arguments


@pytest.mark.parametrize('kind',['wt','wl','wp'])
def test_native_workspace_append_keeps_previous_dirty_edits_and_physical_ids_save_reopen_and_retry(tmp_path,kind):
    bridge,source,base,work,old,args=opened(tmp_path,kind);protected={f:file_sha(f) for f in [source,base,Path(old['checkpoint'])]}
    try:
        result=work.execute('edit','buffer',args,1,'append')
        assert result['revision']==2 and result['dirty'] and result['active']==3 and result['buffer_restarted']
        assert result['native_pid']!=old['native_pid']
        _,reader,_=read_native(Path(result['checkpoint']));assert 2 in native_ids(reader)
        assert sorted(reader.data['value'])==[22,33,44]
        repeated=work.execute('edit','buffer',args,1,'append');assert repeated['reconciled_existing_request'] and repeated['active']==3
        assert repeated['current_workspace']['native_pid']==result['native_pid']
        assert all(file_sha(f)==digest for f,digest in protected.items())
        with pytest.raises(BridgeError,match='WORKSPACE_REVISION_CONFLICT'):work.execute('edit','buffer',args,1,'different')
        # The new dirty buffer remains recoverable if its own native child dies.
        work.buffers['buffer'].process.kill();work.buffers['buffer'].process.wait()
        recovered=work.execute('recover','buffer',{},2)
        assert recovered['revision']==2 and recovered['dirty'] and recovered['active']==3 and recovered['checkpoint_sha256']==result['checkpoint_sha256']
        saved=work.execute('save','buffer',{},2,'save');assert not saved['dirty']
        assert native_file_info(tmp_path,saved['output'],saved['output_sha256'])['active']==3
        work.execute('close','buffer',{},2)
        reopened=work.execute('open','again',{'source':saved['output'],'expected_sha256':saved['output_sha256']})
        assert reopened['active']==3 and not reopened['dirty'];work.execute('close','again',{},0)
    finally:work.close_all()


def test_workspace_append_discard_returns_to_saved_baseline(tmp_path):
    bridge,source,base,work,old,args=opened(tmp_path)
    try:
        added=work.execute('edit','buffer',args,1,'append')
        with pytest.raises(BridgeError,match='WORKSPACE_DIRTY'):work.execute('close','buffer',{},2)
        closed=work.execute('close','buffer',{'discard':True},2)
        assert closed['active']==2 and not closed['dirty'] and Path(closed['checkpoint']).read_bytes()==base.read_bytes()
    finally:work.close_all()


def test_workspace_append_reconciles_completed_native_copy_after_local_finisher_failure_without_replay(tmp_path,monkeypatch):
    import mapgis_agent.native_append as native
    bridge,source,base,work,old,args=opened(tmp_path,'wp');original=native.finish_native_append
    def fail(*args,**kwargs):raise BridgeError('INJECTED_AFTER_NATIVE_COPY')
    monkeypatch.setattr(native,'finish_native_append',fail)
    try:
        with pytest.raises(BridgeError,match='INJECTED_AFTER_NATIVE_COPY'):work.execute('edit','buffer',args,1,'append')
        folder=work._folder('buffer');import json
        pending=json.loads((folder/'state.json').read_text(encoding='utf8'))['pending'];receipt=bridge.status(pending['bridge_request'])
        stage=Path(receipt['stage']);files=[stage/'native/native-com.json',stage/'native/copied.wp']
        protected={f:(file_sha(f),f.stat().st_mtime_ns) for f in files}
        monkeypatch.setattr(native,'finish_native_append',original)
        recovered=work.execute('recover','buffer',{},1)
        assert recovered['revision']==2 and recovered['dirty'] and recovered['active']==3 and recovered['recovery']=='reconciled_without_replay'
        assert all((file_sha(f),f.stat().st_mtime_ns)==value for f,value in protected.items())
        retried=work.execute('edit','buffer',args,1,'append');assert retried['reconciled_existing_request'] and retried['active']==3
    finally:work.close_all()


def test_workspace_append_preflight_rejects_bad_source_without_stopping_native_buffer(tmp_path):
    bridge,source,base,work,old,args=opened(tmp_path)
    try:
        args['parameters']['record_ids']=[999]
        with pytest.raises(BridgeError,match='APPEND_RECORD_IDS'):work.execute('edit','buffer',args,1,'invalid')
        current=work.execute('status','buffer',{})
        assert current['native_pid']==old['native_pid'] and current['revision']==1 and current['pending_request'] is None
    finally:work.close_all()


def test_workspace_append_failed_state_commit_recovers_same_frozen_result_once(tmp_path,monkeypatch):
    bridge,source,base,work,old,args=opened(tmp_path);original=work._write_state;interrupted=[]
    def fail(folder,state):
        if state['revision']==2 and not state['pending'] and not interrupted:
            interrupted.append(state['native_pid']);raise OSError('INJECTED_STATE_COMMIT')
        return original(folder,state)
    monkeypatch.setattr(work,'_write_state',fail)
    try:
        with pytest.raises(OSError,match='INJECTED_STATE_COMMIT'):work.execute('edit','buffer',args,1,'append')
        assert 'buffer' not in work.buffers
        recovered=work.execute('recover','buffer',{},1)
        assert recovered['revision']==2 and recovered['active']==3 and recovered['dirty']
        assert work.execute('edit','buffer',args,1,'append')['reconciled_existing_request']
    finally:work.close_all()


def test_workspace_append_stale_input_before_commit_rolls_back_to_prior_dirty_checkpoint(tmp_path,monkeypatch):
    bridge,source,base,work,old,args=opened(tmp_path);original=work._commit_append
    def change_source(*arguments):
        source.write_bytes(source.read_bytes()+b'changed after preparation')
        return original(*arguments)
    monkeypatch.setattr(work,'_commit_append',change_source)
    try:
        with pytest.raises(BridgeError,match='STALE_SOURCE'):work.execute('edit','buffer',args,1,'append')
        monkeypatch.setattr(work,'_commit_append',original)
        recovered=work.execute('recover','buffer',{},1)
        assert recovered['revision']==1 and recovered['active']==1 and recovered['dirty'] and recovered['checkpoint_sha256']==old['checkpoint_sha256']
        assert recovered['recovery']=='failed_request_rolled_back_to_checkpoint'
    finally:work.close_all()
