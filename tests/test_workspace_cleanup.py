import json
import pytest
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.workspace import NativeWorkspaces
from mapgis_agent.native_paths import NativePathAlias


def test_failed_load_releases_a_new_controller_lease(tmp_path):
    registry=NativeWorkspaces(Bridge(tmp_path));folder=registry._folder('broken');folder.mkdir(parents=True)
    (folder/'state.json').write_text(json.dumps({'id':'another-id'}),encoding='utf8')
    with pytest.raises(BridgeError,match='WORKSPACE_IDENTITY'):registry._load('broken')
    assert 'broken' not in registry.leases


def test_startup_orphan_without_pid_retires_its_recorded_link(tmp_path):
    registry=NativeWorkspaces(Bridge(tmp_path));folder=registry._folder('startup');folder.mkdir(parents=True)
    alias=NativePathAlias(folder)
    try:
        registry._claim(folder,'startup');registry._retire_orphan({'id':'startup'})
        assert not alias.path.exists()
        assert json.loads(alias.record.read_text(encoding='utf8'))['state']=='CLOSED'
    finally:
        alias.close();registry.close_all()
