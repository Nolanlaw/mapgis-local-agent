"""Native buffer loss/recovery must preserve dirty work and avoid edit replay."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import geopandas as gpd
from pymapgis import Writer
import pytest
from shapely.geometry import Point

from mapgis_agent.core import Bridge,BridgeError,read_native,sha
from mapgis_agent.workspace import NativeWorkspaces

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def buffer_case():
    if sys.platform!='win32' or not (ROOT/'config.local.json').exists():
        pytest.skip('Windows native backend configuration is required')
    config=json.loads((ROOT/'config.local.json').read_text(encoding='utf8'))
    if not config.get('native_backend_legacy_dep_compatibility'):
        pytest.skip('Explicit native compatibility opt-in is required')
    (ROOT/'work/headless-060').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ws-',dir=ROOT/'work/headless-060') as tmp:
        root=Path(tmp)
        (root/'config.local.json').write_text(json.dumps({k:config[k] for k in
            ('mapgis_installation','native_backend_legacy_dep_compatibility')}),encoding='utf8')
        source=root/'points.wt'
        Writer(gpd.GeoDataFrame({'ID':[7,7]},geometry=[Point(10,10),Point(20,10)])).to_file(source)
        registry=NativeWorkspaces(Bridge(root))
        yield registry,root,source
        registry.close_all()


def open_buffer(registry,source):
    return registry.execute('open','buffer',{'source':str(source),'expected_sha256':sha(source.read_bytes())})


def move_buffer(registry):
    return registry.execute('edit','buffer',{'operation':'point.move','parameters':{
        'updates':[{'record_id':1,'x':12.5,'y':13.75}]}},0,'move')


def test_native_child_loss_reports_lost_and_recovers_dirty_checkpoint(buffer_case):
    registry,_,source=buffer_case
    original=source.read_bytes()
    opened=open_buffer(registry,source)
    moved=move_buffer(registry)
    child=registry.buffers['buffer'].process
    child.kill();child.wait(timeout=5)
    lost=registry.execute('status','buffer',{})
    assert lost['state']=='LOST' and lost['dirty'] and not lost['running']
    recovered=registry.execute('recover','buffer',{},1)
    assert recovered['revision']==1 and recovered['dirty'] and recovered['native_pid']!=opened['native_pid']
    _,reader,_=read_native(Path(recovered['checkpoint']))
    assert (reader.geom[0].x,reader.geom[0].y)==(12.5,13.75)
    repeated=registry.execute('edit','buffer',{'operation':'point.move','parameters':{
        'updates':[{'record_id':1,'x':12.5,'y':13.75}]}},0,'move')
    assert repeated['revision']==1 and repeated['replayed'] is False
    assert source.read_bytes()==original


def test_another_controller_cannot_steal_a_running_workspace(buffer_case):
    registry,root,source=buffer_case
    opened=open_buffer(registry,source)
    other=NativeWorkspaces(Bridge(root))
    try:
        with pytest.raises(BridgeError,match='WORKSPACE_BUSY'):
            other.execute('recover','buffer',{},0)
        assert registry.buffers['buffer'].process.poll() is None
        assert registry.execute('status','buffer',{})['native_pid']==opened['native_pid']
    finally:other.close_all()


def test_closed_workspace_status_does_not_keep_an_exclusive_controller_lease(buffer_case):
    registry,root,source=buffer_case
    open_buffer(registry,source)
    registry.execute('close','buffer',{},0)
    assert registry.execute('status','buffer',{})['state']=='CLOSED'
    other=NativeWorkspaces(Bridge(root))
    try:
        assert other.execute('status','buffer',{})['state']=='CLOSED'
        reopened=registry.execute('recover','buffer',{},0)
        assert reopened['running'] and reopened['active']==2
    finally:other.close_all()


@pytest.mark.parametrize('phase,exit_code',[('edit',93),('save',94)])
def test_parent_death_reconciles_complete_native_work_without_replay(buffer_case,phase,exit_code):
    registry,root,source=buffer_case
    original=source.read_bytes()
    context="import sys,os;from pathlib import Path;sys.path.insert(0,"+repr(str(ROOT/'src'))+");from mapgis_agent.core import Bridge,sha;from mapgis_agent.workspace import NativeWorkspaces;root=Path("+repr(str(root))+");source=Path("+repr(str(source))+");reg=NativeWorkspaces(Bridge(root));reg.execute('open','buffer',{'source':str(source),'expected_sha256':sha(source.read_bytes())});"
    if phase=='edit':
        script=context+"real=reg._validate_checkpoint\ndef crash(*a):\n result=real(*a)\n os._exit(93)\nreg._validate_checkpoint=crash\nreg.execute('edit','buffer',{'operation':'point.move','parameters':{'updates':[{'record_id':1,'x':12.5,'y':13.75}]}},0,'move')"
    else:
        script=context+"reg.execute('edit','buffer',{'operation':'point.move','parameters':{'updates':[{'record_id':1,'x':12.5,'y':13.75}]}},0,'move');reg._commit_save=lambda *a:os._exit(94);reg.execute('save','buffer',{},1,'save')"
    crashed=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',script],capture_output=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
    assert crashed.returncode==exit_code,crashed.stderr
    before=registry.execute('status','buffer',{})
    assert before['pending_request']==('move' if phase=='edit' else 'save')
    saved_pid=None
    if phase=='save':
        _,state=registry._load('buffer')
        saved_pid=registry.bridge.status(state['pending']['bridge_request'])['native_execution']['pid']
    recovered=registry.execute('recover','buffer',{},before['revision'])
    assert recovered['recovery']=='reconciled_without_replay' and recovered['replayed'] is False
    assert recovered['revision']==1 and recovered['dirty']==(phase=='edit')
    _,reader,_=read_native(Path(recovered['checkpoint']))
    assert (reader.geom[0].x,reader.geom[0].y)==(12.5,13.75)
    if phase=='save':
        assert registry.bridge.status(recovered['save_request'] if 'save_request' in recovered else registry._load('buffer')[1]['requests']['save']['receipt']['save_request'])['native_execution']['pid']==saved_pid
    assert source.read_bytes()==original
