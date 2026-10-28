"""Native subprocess boundary and request lifecycle acceptance."""
import json
from pathlib import Path
import geopandas as gpd
import pytest
from pymapgis import Writer
from shapely.geometry import Point
from mapgis_agent.core import Bridge, BridgeError, sha
from mapgis_agent.native_backend import prepare_native_operation, native_file_info

@pytest.fixture
def case(tmp_path):
    bridge=Bridge(tmp_path/'project')
    original=tmp_path/'source.wt'
    Writer(gpd.GeoDataFrame({'ID':[9,9]},geometry=[Point(10,10),Point(20,10)])).to_file(original)
    work=Path(bridge.copy(str(original),'copy')['path'])
    return bridge,original,work

def test_native_requires_explicit_legacy_host_opt_in(case):
    bridge,original,work=case
    before=original.read_bytes()
    with pytest.raises(BridgeError,match='LEGACY_HOST_DISABLED'):
        native_file_info(bridge.root,str(work),sha(before))
    assert original.read_bytes()==work.read_bytes()==before

def test_native_rejects_stale_input_before_launch(case):
    bridge,_,work=case
    with pytest.raises(BridgeError,match='STALE_SOURCE'):
        prepare_native_operation(bridge,'resave',str(work),'0'*64,{},'stale')
    with bridge.connect() as db:
        assert db.execute('SELECT 1 FROM commands WHERE id=?',('stale',)).fetchone() is None

@pytest.mark.parametrize('parameters',[{'updates':[]},{'updates':[{'record_id':True,'x':1,'y':2}]},{'updates':[{'record_id':3,'x':1,'y':2}]},{'updates':[{'record_id':1,'x':float('nan'),'y':2}]},{'updates':[{'record_id':1,'x':1,'y':2},{'record_id':1,'x':3,'y':4}]}])
def test_native_rejects_ambiguous_or_invalid_point_updates(case,parameters):
    bridge,_,work=case
    with pytest.raises(BridgeError,match='POINT_UPDATES'):
        prepare_native_operation(bridge,'point.move',str(work),sha(work.read_bytes()),parameters,'bad')

def test_native_requires_owned_copy(case):
    bridge,original,_=case
    with pytest.raises(BridgeError,match='owned'):
        prepare_native_operation(bridge,'resave',str(original),sha(original.read_bytes()),{},'original')

def test_running_or_failed_request_cannot_be_applied(case):
    bridge,_,work=case
    for state in ['RUNNING','FAILED']:
        receipt={'id':state,'output':str(bridge.root/'deliverables'/state/'output.wt')}
        with bridge.connect() as db:
            db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)',(state,'hash',state,str(work),work.read_bytes(),b'', '[]',json.dumps(receipt)))
        with pytest.raises(BridgeError,match='not prepared'):
            bridge.apply(state)
        assert not Path(receipt['output']).exists()

def test_native_failure_is_persistent_and_same_request_does_not_replay(case,monkeypatch):
    from mapgis_agent import native_backend as backend
    bridge,original,work=case
    before=original.read_bytes()
    calls=[]
    monkeypatch.setattr(backend,'backend_configuration',lambda root:(Path('worker'),Path('bas60.dll')))
    def fail(*args,**kwargs):
        calls.append(1)
        raise BridgeError('NATIVE_CHILD_FAILED: injected boundary failure')
    monkeypatch.setattr(backend,'_invoke',fail)
    with pytest.raises(BridgeError,match='NATIVE_CHILD_FAILED'):
        prepare_native_operation(bridge,'resave',str(work),sha(before),{},'failure')
    assert Bridge(bridge.root).status('failure')['state']=='FAILED'
    repeated=prepare_native_operation(bridge,'resave',str(work),sha(before),{},'failure')
    assert repeated['state']=='FAILED' and len(calls)==1
    with pytest.raises(BridgeError,match='REQUEST_ID_CONFLICT'):
        prepare_native_operation(bridge,'point.move',str(work),sha(before),{'updates':[{'record_id':1,'x':5,'y':6}]},'failure')
    assert original.read_bytes()==work.read_bytes()==before


def test_reconcile_never_terminates_an_unrelated_executable(case):
    import os
    import psutil
    from mapgis_agent.native_backend import reconcile_native_request
    bridge,_,work=case
    stage=bridge.root/'work'/'uncertain';stage.mkdir()
    own_test=psutil.Process(os.getpid())
    (stage/'invoke.json.started.json').write_text(json.dumps({'pid':os.getpid(),
        'process_start':own_test.create_time(),'executable':own_test.exe()}),encoding='utf8')
    receipt={'id':'uncertain','kind':'NATIVE','source':str(work),'stage':str(stage),
             'output':str(bridge.root/'deliverables'/'uncertain'/'result.wt')}
    with bridge.connect() as db:
        db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)',('uncertain','hash','RUNNING',str(work),work.read_bytes(),b'','[]',json.dumps(receipt)))
    with pytest.raises(FileNotFoundError):
        reconcile_native_request(bridge,'uncertain')
    assert own_test.is_running()
    assert bridge.status('uncertain')['state']=='FAILED'
