import asyncio
import json
from pathlib import Path

import pytest
from mapgis_agent.core import Bridge, BridgeError
from mapgis_agent.private_control import private_control, _relative
from mapgis_agent.native_ui_core import NativeUIClient, encoded, digest, display_text


def test_legacy_control_lone_surrogate_is_retained_as_json_escape():
    value={'text':'中文\ud9d8'}
    assert json.loads(encoded(value))==value
    assert encoded(value).encode('utf8')
    assert digest(value)==digest(json.loads(encoded(value)))
    assert display_text(value['text'])=='中文\\ud9d8'
    assert display_text(value['text']).encode('utf8')


def test_candidate_reports_exact_nonserver_entries_without_launch(tmp_path):
    result=private_control(Bridge(tmp_path),'policy')
    assert result['enabled']is False and len(result['modules'])==20
    assert result['modules']['terrain_3d']['entry_role']=='archive_extractor'
    assert result['modules']['terrain_flight']['executable']=='DTM3DFly.exe'
    assert 'database_setup'not in result['modules'] and 'utilities'not in result['modules']
    assert result['attach_existing_process']is False and result['desktop_input_used']is False


def test_general_open_rejected_before_any_process_launch_without_exception(tmp_path,monkeypatch):
    def forbidden(*a,**k):raise AssertionError('no process before authorization')
    monkeypatch.setattr(NativeUIClient,'start',forbidden)
    with pytest.raises(BridgeError,match='AUTHORIZATION_REQUIRED'):
        private_control(Bridge(tmp_path),'open',module='reports',session_id='not-authorized')
    assert not(tmp_path/'work/private-controls').exists()


def test_server_module_stays_excluded_even_when_general_local_opt_in_is_true(tmp_path):
    b=Bridge(tmp_path)
    (tmp_path/'config.local.json').write_text(json.dumps({'native_general_private_ui':True}),encoding='utf8')
    with pytest.raises(BridgeError,match='MODULE_NOT_ALLOWED'):
        private_control(b,'open',module='database_setup',session_id='server-excluded')


@pytest.mark.parametrize('name',['../original.wt','C:/original.wt','\\\\host\\share\\x.wt','\\rooted.wt','.','folder/../../original.wt','bad:name.wt','CON','trailing.','bad?name',None])
def test_copy_members_cannot_escape_or_use_device_names(name):
    with pytest.raises(BridgeError,match='RELATIVE_PATH'):_relative(name)


def test_real_registered_tool_schema_and_discovery_keep_the_gate_visible(tmp_path):
    from mapgis_agent.server import build_server
    from mapgis_agent.operations import describe_operation
    server=build_server(tmp_path);tools={t.name:t for t in asyncio.run(server.list_tools())}
    assert 'mapgis_private_control'in tools
    desc=describe_operation(tmp_path,'native.private_control')
    assert desc['availability']=='PRIVATE_UI_AUTHORIZATION_REQUIRED'
    assert desc['validation']=='NATIVE_LOCAL_WORKFLOWS_SAVE_FRESH_REOPEN_VERIFIED'


def test_private_ui_discovery_uses_its_own_opt_in(tmp_path):
    from mapgis_agent.operations import describe_operation
    (tmp_path/'config.local.json').write_text(json.dumps({'native_general_private_ui':True,
        'mapgis_installation':str(tmp_path/'installed')}),encoding='utf8')
    desc=describe_operation(tmp_path,'native.private_control')
    assert desc['availability']=='EXACT_BUILD_CHECKED_PER_OPEN'
    trace=describe_operation(tmp_path,'scan.interactive_trace')
    assert trace['execution']['tool']=='mapgis_private_control'
    assert trace['execution']['module']=='editor' and trace['executable_by_mcp']
    assert trace['reference_execution']['kind']=='host_sky'
    assert trace['validation']=='DISCOVERED'


def test_capabilities_publish_authorized_route_without_old_blanket_gaps(tmp_path):
    from mapgis_agent.environment import capabilities
    (tmp_path/'config.local.json').write_text(json.dumps({'native_general_private_ui':True}),encoding='utf8')
    result=capabilities(tmp_path)
    assert result['modules']['reports']['private_process_route']['enabled']is True
    assert result['modules']['symbols']['private_process_route']['module']=='editor'
    assert result['private_process_calling']['modules']['terrain_flight']['executable']=='DTM3DFly.exe'
    assert 'proprietary_map_and_image_databases'not in result['not_connected']
    assert 'server_dependent_features'in result['not_connected']
