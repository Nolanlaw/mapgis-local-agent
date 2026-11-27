import asyncio
import json
from pathlib import Path
import subprocess
import sys

import geopandas as gpd
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ListToolsResult
from pymapgis import Writer
import pytest
from shapely.geometry import LineString

from mapgis_agent.core import sha
from mapgis_agent.operations import _EXAMPLES
from mapgis_agent.server import build_server


REPO = Path(__file__).resolve().parents[1]
COMPACT = {
    'mapgis_discover', 'mapgis_environment', 'mapgis_inspect', 'mapgis_execute',
    'mapgis_sdk_flow', 'mapgis_private_ui', 'mapgis_workspace', 'mapgis_job_manage',
    'mapgis_tx_commit', 'mapgis_tx_manage', 'mapgis_tx_recovery', 'mapgis_export_plot',
    'mapgis_audit_check', 'mapgis_compare', 'mapgis_run_plan',
}
DIRECT = {
    'mapgis_read_features', 'mapgis_make_work_copy', 'mapgis_prepare_patch',
    'mapgis_prepare_file_operation', 'mapgis_prepare_native_project', 'mapgis_prepare_project_plot',
}


def _payload(result):
    return result.structuredContent if result.structuredContent is not None else json.loads(result.content[0].text)


def test_all_surfaces_and_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv('MAPGIS_MCP_SURFACE', raising=False)

    async def check():
        default = build_server(tmp_path / 'default')
        legacy = {tool.name: tool for tool in await default.list_tools()}
        assert len(legacy) == 123
        monkeypatch.setenv('MAPGIS_MCP_SURFACE', 'compact')
        compact = build_server(tmp_path / 'compact')
        hybrid = build_server(tmp_path / 'hybrid', 'hybrid')
        for server, expected, budget in ((compact, COMPACT, 10500), (hybrid, COMPACT | DIRECT, 15500)):
            tools = await server.list_tools()
            assert {tool.name for tool in tools} == expected
            assert len(ListToolsResult(tools=tools).model_dump_json(exclude_none=True).encode()) <= budget
            assert all(server._tool_manager.get_tool(tool.name).is_async for tool in tools)
        for server in (default, compact, hybrid):
            tools = await server.list_tools()
            assert all(set(tool.annotations.model_dump(exclude_none=True)) >= {
                'readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint'} for tool in tools)
            assert len(await server.list_resources()) == len(await server.list_resource_templates()) == 2
            assert len(await server.list_prompts()) == 5
        assert legacy['mapgis_inspect_file'].annotations.readOnlyHint
        assert legacy['mapgis_cancel_request'].annotations.destructiveHint
        assert not legacy['mapgis_prepare_delete'].annotations.destructiveHint
        for tool in await hybrid.list_tools():
            if tool.name in DIRECT:
                assert len(tool.description) <= 280
                assert tool.inputSchema == legacy[tool.name].inputSchema
        for op_id in _EXAMPLES:
            [raw] = await default.call_tool('mapgis_describe_operation', {'operation_id': op_id})
            detail = json.loads(raw.text)
            assert Draft202012Validator(detail['input_schema']).is_valid(detail['example_arguments']), op_id

    asyncio.run(check())
    with pytest.raises(ValueError, match='INVALID_SURFACE'):
        build_server(tmp_path / 'invalid', 'unknown')


def _line(path, crs='EPSG:4326', x=120):
    Writer(gpd.GeoDataFrame({'ID': [21]}, geometry=[LineString([(x, 30), (x + 1, 31)])],
                           crs=crs)).to_file(path)
    return sha(path.read_bytes())


def _help_fixture(root):
    program = root / 'mapgis' / 'program'
    program.mkdir(parents=True)
    source = program / 'Mpedit60.hlp'
    source.write_bytes(b'fixture help')
    (root / 'config.local.json').write_text(json.dumps({'mapgis_installation': str(program.parent)}), encoding='utf-8')
    index = root / 'work' / 'native-help'
    index.mkdir(parents=True)
    (index / 'editor.json').write_text(json.dumps({
        'source': str(source), 'source_sha256': sha(source.read_bytes()), 'parse_errors': [],
        'topics': [{'id': '0', 'title': '拓扑造区', 'text': '独立帮助正文'}],
    }, ensure_ascii=False), encoding='utf-8')


def test_real_stdio_publication_and_schema_failures(tmp_path):
    original = tmp_path / 'original.wl'
    original_digest = _line(original)
    root = tmp_path / 'project'
    _help_fixture(root)
    params = StdioServerParameters(command=sys.executable, args=[
        '-s', '-B', '-X', 'utf8', str(REPO / 'scripts' / 'launch_mcp.py'),
        '--root', str(root), '--surface', 'hybrid',
    ], env={'MAPGIS_MCP_SURFACE': 'legacy'})

    async def run():
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            initialized = await client.initialize()
            assert initialized.capabilities.tools is not None
            assert {tool.name for tool in (await client.list_tools()).tools} == COMPACT | DIRECT
            catalog = await client.read_resource('mapgis://operations/catalog')
            assert json.loads(catalog.contents[0].text)['count'] == 177
            detail = await client.read_resource('mapgis://operations/file.features')
            assert json.loads(detail.contents[0].text)['execute_via']['op_id'] == 'file.features'
            help_result = await client.read_resource('mapgis://help/editor/0')
            assert json.loads(help_result.contents[0].text)['topic']['text'] == '独立帮助正文'
            standards = (await client.read_resource('mapgis://standards/cartography')).contents[0].text
            assert all(token in standards for token in ('GB/T 958-2015', 'DZ/T 0179-2025', '40,000,000'))
            prompt = await client.get_prompt('drillhole-section-column', {'task': 'Verify survey depths'})
            assert 'Verify survey depths' in prompt.messages[0].content.text

            async def ok(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.isError, result.content
                assert result.structuredContent is not None
                return _payload(result)

            async def error(name, arguments, code):
                result = await client.call_tool(name, arguments)
                assert result.isError and result.structuredContent['error_code'] == code
                assert 'fake-private-token' not in result.content[0].text
                return _payload(result)

            await error('mapgis_execute', {'op_id': 'file.inspect', 'arguments': {'file': 4}},
                        'INVALID_TOOL_ARGUMENTS')
            await error('mapgis_job_manage', {'action': 'fake-private-token'}, 'INVALID_TOOL_ARGUMENTS')
            await error('mapgis_execute', {'op_id': 'scan.interactive_trace', 'arguments': {}},
                        'HOST_OBSERVATION_REQUIRED')
            copy = await ok('mapgis_make_work_copy', {'source': str(original), 'request_id': 'copy'})
            prepared = await ok('mapgis_prepare_patch', {'file': copy['path'], 'expected_sha256': copy['sha256'],
                                'record_ids': [1], 'request_id': 'patch', 'style': {'color': 6}})
            assert prepared['state'] == 'PREPARED'
            epoch = (await ok('mapgis_tx_manage', {'action': 'advance_epoch'}))['epoch']
            await error('mapgis_execute', {'op_id': 'mapgis_apply_patch', 'arguments': {'request_id': 'patch'}},
                        'PUBLISHER_EPOCH_REQUIRED')
            await error('mapgis_tx_commit', {'request_id': 'patch', 'expected_epoch': epoch - 1},
                        'STALE_PUBLISHER_EPOCH')
            assert not Path(prepared['output']).exists()
            kwargs = {'request_id': 'patch', 'expected_epoch': epoch}
            applied = await ok('mapgis_tx_commit', kwargs)
            assert applied['state'] == 'APPLIED' and Path(applied['output']).is_file()
            assert (await ok('mapgis_tx_commit', kwargs))['reconciled_existing_output']
            saved = await ok('mapgis_inspect', {'op_id': 'file.inspect', 'arguments': {'file': applied['output']}})
            assert saved['rows'][0]['style']['color'] == 6
            audited = await ok('mapgis_audit_check', {'files': [{'file': applied['output'],
                                       'expected_sha256': applied['candidate_sha256']}]})
            assert audited['files'][0]['attributes']['aligned'] and audited['files'][0]['topology']['invalid'] == 0
            await error('mapgis_audit_check', {'reference_crs': 'EPSG:4528', 'files': [
                {'file': applied['output'], 'expected_sha256': applied['candidate_sha256']}]},
                'AUDIT_UNPROJECTED_MIX')

    asyncio.run(asyncio.wait_for(run(), timeout=30))
    assert sha(original.read_bytes()) == original_digest


def test_compact_plan_actions_and_registered_sprint2_routes(tmp_path):
    from mapgis_agent.drc import read_layer
    server = build_server(tmp_path, 'compact')
    file = tmp_path / 'vector.wl'
    digest = _line(file)
    data = {'file': str(file), 'expected_sha256': digest}
    document = {'plan_id': 'mcp', 'crs': 'EPSG:4326', 'unit': 'degree', 'scale': read_layer(file).scale,
                'nodes': [{'id': 'copy', 'op': 'work.copy', 'inputs': [
                    {'role': 'file', 'source_file': str(file), 'expected_sha256': digest}]},
                    {'id': 'inspect', 'op': 'file.inspect', 'inputs': [
                        {'role': 'file', 'from_node': 'copy', 'output_key': 'file'}]}]}

    async def run():
        for op in ('drc.audit', 'preview.render', 'fdr.export', 'labels.place', 'abi.probe'):
            detail = await server.call_tool('mapgis_discover', {'operation_id': op})
            assert detail['execute_via'] == {'tool': 'mapgis_execute', 'op_id': op}
            assert detail['execution_status'] == 'MCP_TOOL_AVAILABLE'
            assert Draft202012Validator(detail['input_schema']).is_valid(detail['example_arguments'])
        report = await server.call_tool('mapgis_audit_check', {'files': [data], 'before_files': [data]})
        assert report['summary']['accounting']['record_delta'] == 0
        planned = await server.call_tool('mapgis_run_plan', {'action': 'plan', 'arguments': {'spec': document}})
        assert planned['topological_order'] == ['copy', 'inspect']
        interrupted = await server.call_tool('mapgis_run_plan', {'action': 'apply', 'arguments': {
            'spec': document, 'fault_inject_node': 'inspect', 'expected_epoch': 0}})
        assert interrupted['status'] == 'INTERRUPTED'
        resumed = await server.call_tool('mapgis_run_plan', {'action': 'resume', 'arguments': {
            'plan_id': 'mcp', 'expected_epoch': 0}})
        assert resumed['status'] == 'COMPLETE' and resumed['reused_nodes'] == ['copy']
        new = await server.call_tool('mapgis_run_plan', {'action': 'diff', 'arguments': {
            'current_spec': document, 'previous_state_or_spec': resumed}})
        assert new['unchanged_cached'] == ['copy', 'inspect']
        preview = await server.call_tool('mapgis_execute', {'op_id': 'preview.render', 'arguments': {
            'files': [data], 'output_png': 'work/preview.png'}})
        assert Path(preview['path']).is_file()
        bundle = await server.call_tool('mapgis_execute', {'op_id': 'fdr.export', 'arguments': {
            'output_zip': 'work/package.mapgis-fdr.zip', 'options': {'plan_id': 'mcp'}}})
        assert bundle['verified'] and Path(bundle['path']).is_file()
        abi = await server.call_tool('mapgis_inspect', {'op_id': 'abi.probe', 'arguments': {}})
        assert abi['tier'] == 'D'

    asyncio.run(run())


def test_reference_coordinates_and_jobs_through_compact(tmp_path):
    server = build_server(tmp_path, 'compact')
    file = tmp_path / 'projected.wl'
    digest = _line(file, 'EPSG:4549', x=500_000)

    async def check():
        result = await server.call_tool('mapgis_audit_check', {'reference_crs': 'EPSG:4528', 'files': [
            {'file': str(file), 'expected_sha256': digest}]})
        assert result.isError and result.structuredContent['error_code'] == 'AUDIT_FALSE_EASTING'
        arguments = {'file': str(file)}
        job_call = {'op_id': 'file.inspect', 'arguments': arguments, 'async_job': True, 'request_id': 'inspect-1'}
        job = await server.call_tool('mapgis_execute', job_call)
        assert 'job_id' in job and (await server.call_tool('mapgis_execute', job_call))['job_id'] == job['job_id']
        async with asyncio.timeout(5):
            while True:
                status = await server.call_tool('mapgis_job_manage', {'action': 'status', 'job_id': job['job_id']})
                if status['state'] not in {'QUEUED', 'RUNNING'}:
                    break
                await asyncio.sleep(0.02)
        assert status['state'] == 'SUCCEEDED' and status['result']['sha256'] == digest
        conflict = await server.call_tool('mapgis_execute', job_call | {'arguments': arguments | {'limit': 2}})
        assert conflict.isError and conflict.structuredContent['error_code'] == 'JOB_REQUEST_CONFLICT'

    asyncio.run(check())


def test_sprint3_discovery_and_native_probe_share_the_existing_execute_surface(tmp_path):
    from test_native_raster import native_settings

    server = build_server(tmp_path, 'compact')
    operations = ('crs.modern_create', 'section.drillhole_workflow', 'geology.section_workflow', 'ui.macro_run',
                  'ui.verified_macro', 'dbs.multisheet_validate', 'dbs.multisheet_edge_match',
                  'dbs.multisheet_build', 'dbs.multisheet_window', 'native.l2_probe', 'native.survey_wt_normalize')

    async def run():
        assert {tool.name for tool in await server.list_tools()} == COMPACT
        for op in operations:
            detail = await server.call_tool('mapgis_discover', {'operation_id': op})
            assert detail['execution_status'] == 'MCP_TOOL_AVAILABLE'
            assert detail['execute_via'] == {'tool': 'mapgis_execute', 'op_id': op}
            assert Draft202012Validator(detail['input_schema']).is_valid(detail['example_arguments']), op
        bad = await server.call_tool('mapgis_execute', {'op_id': 'dbs.multisheet_validate',
                                     'arguments': {'sheets': []}})
        assert bad.isError and bad.structuredContent['error_code'] == 'DBS_SCHEMA'
        failure = await server.call_tool('mapgis_execute', {'op_id': 'native.l2_probe',
                                         'arguments': {'format': '4x', 'parameter_block_hex': '00'*56}})
        assert failure.isError and failure.structuredContent['error_code'] == 'L2_PARAMETER_BLOCK'
        native_settings(tmp_path)
        detail = await server.call_tool('mapgis_discover', {'operation_id': 'native.l2_probe'})
        receipt = await server.call_tool('mapgis_execute', {'op_id': 'native.l2_probe',
                                         'arguments': detail['example_arguments']})
        assert receipt['library'] == 'read_4x.dll' and receipt['windows_created'] == 0
        assert receipt['items'][1] == {'index': 2, 'value': 1.}

    asyncio.run(run())


def test_cli_surface_and_structured_exit_code(tmp_path):
    base = [sys.executable, '-s', '-B', '-X', 'utf8', str(REPO / 'scripts' / 'mapgis_cli.py'),
            '--root', str(tmp_path), '--surface', 'compact']
    listed = subprocess.run([*base, 'tools'], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert listed.returncode == 0 and {tool['name'] for tool in json.loads(listed.stdout)['tools']} == COMPACT
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'tool': 'mapgis_execute', 'arguments': {'op_id': 'missing', 'arguments': {}}}),
                       encoding='utf-8')
    failed = subprocess.run([*base, 'call', '--arguments', str(request)], capture_output=True,
                            text=True, encoding='utf-8', timeout=20)
    assert failed.returncode == 1 and json.loads(failed.stdout)['error_code'] == 'UNKNOWN_OPERATION'


def test_sprint4_catalog_and_handoff_lifecycle_through_existing_generic_tools(tmp_path):
    source = tmp_path / 'fault.wl'
    expected = _line(source)
    server = build_server(tmp_path, 'compact')

    async def run():
        assert {tool.name for tool in await server.list_tools()} == COMPACT
        for op in ('interchange.export', 'interchange.verify', 'provider.inspect'):
            detail = await server.call_tool('mapgis_discover', {'operation_id': op})
            assert detail['execute_via'] == {'tool': 'mapgis_execute', 'op_id': op}
            assert Draft202012Validator(detail['input_schema']).is_valid(detail['example_arguments'])
        route = await server.call_tool('mapgis_inspect', {'op_id': 'provider.inspect',
                                       'arguments': {'operation_id': 'interchange.export'}})
        headless = next(state for state in route['providers'] if state['provider_id'] == 'open_headless')
        assert headless['supports_operation'] and headless['available']
        args = {'sources': [{'file': str(source), 'expected_sha256': expected}], 'target': 'qgis_gpkg',
                'crs': 'EPSG:4326', 'output_dir': 'deliverables/qgis'}
        result = await server.call_tool('mapgis_execute', {'op_id': 'interchange.export', 'arguments': args})
        assert result['verified'] and result['feature_count'] == 1
        check = {'op_id': 'interchange.verify', 'arguments': {'package_dir': result['package_dir']}}
        verified = await server.call_tool('mapgis_inspect', check)
        assert {'fault.qml', 'field-dictionary.json', 'layers.gpkg'} <= set(verified['verified_outputs'])
        style = Path(result['package_dir']) / 'fault.qml'
        style.write_bytes(style.read_bytes() + b'\n')
        bad = await server.call_tool('mapgis_inspect', check)
        assert bad.isError and bad.structuredContent['error_code'] == 'INTERCHANGE_INTEGRITY'

    asyncio.run(run())
