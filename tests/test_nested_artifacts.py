"""Native DEM companion directories must publish atomically with their header."""
import json
import os
from pathlib import Path
import pytest
from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.artifacts import begin_bundle,finish_bundle,reconcile_bundle

def prepared_tree(tmp_path,monkeypatch=None):
    bridge=Bridge(tmp_path);source=tmp_path/'source.tif';source.write_bytes(b'protected source')
    stage,receipt,_=begin_bundle(bridge,{'operation':'native.scene'},'scene',
        [{'file':str(source),'sha256':sha(source.read_bytes())}],'map.scene.json')
    files=stage/'files';(files/'terrain').mkdir(parents=True)
    (files/'map.scene.json').write_text('{"native_dem":"terrain.dem"}')
    (files/'terrain.dem').write_bytes(b'DEM_INFO header')
    (files/'terrain/terrain.Hdr').write_bytes(b'native pyramid header')
    (files/'terrain/terrain.Dem').write_bytes(b'native elevations')
    if monkeypatch:
        import mapgis_agent.artifacts as module
        monkeypatch.setattr(module,'reconcile_bundle',lambda *args:None)
    result=finish_bundle(bridge,'scene',files,{'backend':'native directory fixture'})
    return bridge,source,stage,receipt,result

def test_native_dependency_tree_resumes_partial_publication_and_detects_data_change(tmp_path):
    bridge,source,stage,receipt,prepared=prepared_tree(tmp_path)
    assert {m['name'] for m in prepared['members']}=={'map.scene.json','terrain.dem','terrain/terrain.Hdr','terrain/terrain.Dem'}
    output=Path(receipt['output']);output.parent.parent.mkdir(parents=True)
    partial=output.parent.with_name('.scene.publishing');(partial/'terrain').mkdir(parents=True)
    (partial/'terrain/terrain.Hdr').write_bytes(b'native pyramid header')
    result=bridge.apply('scene');assert result['state']=='APPLIED' and bridge.status('scene')['output_matches']
    assert (output.parent/'terrain/terrain.Dem').read_bytes()==b'native elevations'
    (output.parent/'terrain/terrain.Dem').write_bytes(b'changed native values')
    assert bridge.status('scene')['state']=='OUTPUT_CHANGED'
    with pytest.raises(BridgeError,match='OUTPUT_CONFLICT'):bridge.apply('scene')
    assert source.read_bytes()==b'protected source'

def test_native_tree_recovers_sealed_files_without_replaying_producer(tmp_path,monkeypatch):
    bridge,source,stage,receipt,_=prepared_tree(tmp_path,monkeypatch)
    before={str(p.relative_to(stage/'files')):(sha(p.read_bytes()),p.stat().st_mtime_ns) for p in (stage/'files').rglob('*') if p.is_file()}
    result=reconcile_bundle(Bridge(tmp_path),'scene');assert result['state']=='PREPARED' and result['replayed'] is False
    assert bridge.apply('scene')['state']=='APPLIED'
    after={str(p.relative_to(stage/'files')):(sha(p.read_bytes()),p.stat().st_mtime_ns) for p in (stage/'files').rglob('*') if p.is_file()}
    assert after==before and source.read_bytes()==b'protected source'

@pytest.mark.parametrize('unsafe',['../outside.dat','terrain/../../outside.dat','/outside.dat','terrain\\outside.dat'])
def test_sealed_tree_refuses_escaping_member_names(tmp_path,monkeypatch,unsafe):
    bridge,source,stage,receipt,_=prepared_tree(tmp_path,monkeypatch)
    outside=tmp_path/'outside.dat';outside.write_bytes(b'untouched')
    checkpoint=stage/'complete.json';record=json.loads(checkpoint.read_text())
    record['members'][0]['name']=unsafe;checkpoint.write_text(json.dumps(record))
    with pytest.raises(BridgeError):reconcile_bundle(bridge,'scene')
    assert outside.read_bytes()==b'untouched' and not Path(receipt['output']).exists()

def test_unexpected_empty_directory_is_detected_after_publication(tmp_path):
    bridge,source,stage,receipt,_=prepared_tree(tmp_path);bridge.apply('scene')
    (Path(receipt['output']).parent/'unexpected').mkdir()
    assert bridge.status('scene')['state']=='OUTPUT_CHANGED'

def test_partial_tree_refuses_wrong_existing_secondary_file(tmp_path):
    bridge,source,stage,receipt,_=prepared_tree(tmp_path)
    output=Path(receipt['output']);output.parent.parent.mkdir(parents=True)
    partial=output.parent.with_name('.scene.publishing');(partial/'terrain').mkdir(parents=True)
    (partial/'terrain/terrain.Dem').write_bytes(b'conflicting data')
    with pytest.raises(BridgeError,match='PUBLICATION_CONFLICT'):bridge.apply('scene')
    assert not output.parent.exists() and (partial/'terrain/terrain.Dem').read_bytes()==b'conflicting data'

@pytest.mark.skipif(os.name!='nt',reason='Windows junction boundary')
def test_native_tree_never_follows_a_junction_to_other_data(tmp_path):
    from mapgis_agent.native_paths import NativePathAlias
    bridge=Bridge(tmp_path);source=tmp_path/'source.txt';source.write_bytes(b'source')
    stage,_,_=begin_bundle(bridge,{'operation':'test.tree'},'junction',[{'file':str(source),'sha256':sha(source.read_bytes())}],'result.dat')
    files=stage/'files';files.mkdir();(files/'result.dat').write_bytes(b'result')
    protected=tmp_path/'other-data';protected.mkdir();sentinel=protected/'keep.dat';sentinel.write_bytes(b'never read into artifact')
    link=files/'terrain'
    with NativePathAlias(protected,record_folder=stage) as alias:
        os.rename(alias.path,link)
        try:
            with pytest.raises(BridgeError,match='ARTIFACT_MEMBER_TYPE'):finish_bundle(bridge,'junction',files,{})
            assert sentinel.read_bytes()==b'never read into artifact'
        finally:
            assert link.is_junction() and os.path.samefile(link,protected)
            os.rmdir(link)  # Own junction only; target files are never traversed/deleted.
