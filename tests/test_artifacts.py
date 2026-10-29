import json
import os
from pathlib import Path

import pytest

from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.artifacts import begin_bundle,finish_bundle,reconcile_bundle


def prepared(tmp_path,monkeypatch=None):
    bridge=Bridge(tmp_path);source=tmp_path/'source.txt';source.write_bytes(b'unchanged source')
    stage,receipt,new=begin_bundle(bridge,{'operation':'test.bundle'},'bundle',
        [{'file':str(source),'sha256':sha(source.read_bytes())}],'result.dat')
    assert new
    files=stage/'files';files.mkdir()
    (files/'result.dat').write_bytes(b'result');(files/'result.json').write_text('{"coordinate_system":"explicit"}',encoding='utf8')
    if monkeypatch:
        import mapgis_agent.artifacts as module
        monkeypatch.setattr(module,'reconcile_bundle',lambda *a:None)
    result=finish_bundle(bridge,'bundle',files,{'backend':'owned test producer'})
    return bridge,source,stage,receipt,result


def test_bundle_publication_reconciles_partial_set_and_detects_changed_secondary_member(tmp_path):
    bridge,source,stage,receipt,result=prepared(tmp_path)
    output=Path(receipt['output']);output.parent.parent.mkdir(parents=True)
    interrupted=output.parent.with_name('.bundle.publishing');interrupted.mkdir()
    (interrupted/'result.dat').write_bytes(b'result')
    applied=bridge.apply('bundle')
    assert applied['state']=='APPLIED' and bridge.status('bundle')['output_matches']
    assert (output.parent/'result.json').read_bytes()==(stage/'files/result.json').read_bytes()
    assert bridge.apply('bundle')['reconciled_existing_output']
    (output.parent/'result.json').write_text('changed',encoding='utf8')
    assert bridge.status('bundle')['state']=='OUTPUT_CHANGED'
    with pytest.raises(BridgeError,match='OUTPUT_CONFLICT'):bridge.apply('bundle')
    assert source.read_bytes()==b'unchanged source'


def test_completed_artifact_reconciles_after_parent_lost_before_ledger_commit(tmp_path,monkeypatch):
    bridge,source,stage,receipt,_=prepared(tmp_path,monkeypatch)
    assert bridge.status('bundle')['state']=='RUNNING'
    recovered=reconcile_bundle(Bridge(tmp_path),'bundle')
    assert recovered['state']=='PREPARED' and recovered['replayed'] is False
    assert bridge.apply('bundle')['state']=='APPLIED'


def test_bundle_stale_source_and_id_conflict_do_not_overwrite(tmp_path):
    bridge,source,stage,receipt,_=prepared(tmp_path)
    with pytest.raises(BridgeError,match='REQUEST_ID_CONFLICT'):
        begin_bundle(bridge,{'operation':'different'},'bundle',[{'file':str(source),'sha256':sha(source.read_bytes())}],'other.dat')
    source.write_bytes(b'new source')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('bundle')
    assert not Path(receipt['output']).exists()


@pytest.mark.skipif(os.name!='nt',reason='Windows case-insensitive native output names')
def test_native_uppercase_extension_keeps_exact_artifact_member_identity(tmp_path):
    bridge=Bridge(tmp_path);source=tmp_path/'input.txt';source.write_bytes(b'source')
    stage,_,_=begin_bundle(bridge,{'operation':'native-case'},'case',[{'file':str(source),'sha256':sha(source.read_bytes())}],'map.eps')
    files=stage/'files';files.mkdir();(files/'map.EPS').write_bytes(b'native output')
    result=finish_bundle(bridge,'case',files,{'backend':'native case test'})
    assert Path(result['output']).name=='map.EPS'
    assert bridge.apply('case')['state']=='APPLIED' and bridge.status('case')['output_matches']
