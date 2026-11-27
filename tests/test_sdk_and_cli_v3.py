import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

import geopandas as gpd
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pymapgis import Writer
import pytest
from shapely.geometry import Point
from starlette.testclient import TestClient

from mapgis_agent import MapGISClient, MapGISConfig, MapPlan, Workspace
from mapgis_agent.core import BridgeError, sha
from mapgis_agent.jobs import JobManager
from mapgis_agent.server import (build_server, streamable_http_app, validate_http_request_headers,
                                 validate_streamable_http_config)


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scripts/launch_mcp.py'


def _points(path):
    Writer(gpd.GeoDataFrame({'ID': [21]}, geometry=[Point(120, 30)], crs='EPSG:4326')).to_file(path)
    return sha(path.read_bytes())


def _cli(*args):
    return subprocess.run([sys.executable, '-s', '-B', '-X', 'utf8', str(ROOT / 'scripts/mapgis_cli.py'),
                           *map(str, args)], capture_output=True, encoding='utf-8', check=True, timeout=45)


def test_python_sdk_discovery_edit_epoch_and_job(tmp_path):
    source = tmp_path / 'source.wt'
    original = _points(source)
    client = MapGISClient(MapGISConfig(tmp_path))
    assert client.discover('inspect WT')['operations']
    assert client.describe('file.inspect')['execute_via']['op_id'] == 'file.inspect'
    assert client.inspect_file(source.name)['sha256'] == original
    copy = client.execute('mapgis_make_work_copy', {'source': str(source)}, request_id='sdk-copy')
    preview = client.execute('mapgis_prepare_patch', {'file': copy['path'], 'expected_sha256': copy['sha256'],
                                                      'record_ids': [1], 'style': {'color': 6}}, request_id='sdk-edit')
    assert preview['state'] == 'PREPARED'
    old_epoch = client.current_epoch()
    epoch = client.advance_epoch()
    with pytest.raises(BridgeError, match='STALE_PUBLISHER_EPOCH'):
        client.commit('sdk-edit', old_epoch)
    saved = client.commit('sdk-edit', epoch)
    assert saved['state'] == 'APPLIED'
    assert client.inspect_file(saved['output'])['rows'][0]['style']['color'] == 6
    assert client.commit('sdk-edit', epoch)['output'] == saved['output']
    assert sha(source.read_bytes()) == original

    async def from_notebook():
        assert client.execute('file.inspect', {'file': str(source)})['sha256'] == original

    asyncio.run(from_notebook())
    arguments = {'file': str(source)}
    job = client.execute('file.inspect', arguments, async_job=True, request_id='sdk-inspect')
    assert client.execute('file.inspect', arguments, async_job=True, request_id='sdk-inspect')['job_id'] == job['job_id']
    observer = JobManager(tmp_path)
    try:
        deadline = time.monotonic() + 8
        while (result := observer.status(job['job_id']))['state'] in {'QUEUED', 'RUNNING'} and time.monotonic() < deadline:
            time.sleep(0.02)
        assert result['state'] == 'SUCCEEDED' and result['result']['sha256'] == original
    finally:
        observer.close()


def test_plan_builder_preserves_inputs_and_runs_engine(tmp_path):
    first, second = tmp_path / 'first.wt', tmp_path / 'second.wt'
    hashes = [_points(first), _points(second)]
    sources = [{'role': 'layers', 'source_file': str(path), 'expected_sha256': digest}
               for path, digest in zip((second, first), hashes[::-1], strict=True)]
    plan = MapPlan('sdk-map', 'EPSG:4326', 'degree', 1).add_node('layers', 'preview.render', inputs=sources)
    assert [entry['source_file'] for entry in plan.to_spec()['nodes'][0]['inputs']] == [str(second), str(first)]
    spec = MapPlan('sdk-inspect', 'EPSG:4326', 'degree', 1).add_node(
        'inspect', 'file.inspect', inputs=[dict(sources[0], role='file')])
    client = MapGISClient(tmp_path)
    assert client.run_plan('plan', spec)['topological_order'] == ['inspect']
    result = client.run_plan('apply', spec, expected_epoch=client.current_epoch())
    assert result['executed_nodes'] == ['inspect']
    assert client.run_plan('resume', 'sdk-inspect')['reused_nodes'] == ['inspect']
    assert client.export_fdr('deliverables/readback.zip', plan_id='sdk-inspect')['verified']


def test_workspace_context_closes_native_buffer_and_discards_unsaved_edits(tmp_path, monkeypatch):
    from mapgis_agent import workspace

    calls = []

    class NativeBufferDouble:
        def __init__(self, bridge):
            self.revision = 0
            self.state = 'OPEN'

        def execute(self, action, identity, params, expected_revision=None, request_id=None):
            calls.append((action, identity, params, expected_revision, request_id))
            if action == 'edit':
                self.revision += 1
            if action == 'close':
                self.state = 'CLOSED'
            return {'state': self.state, 'revision': self.revision, 'running': self.state == 'OPEN'}

        def close_all(self):
            calls.append(('release',))

    monkeypatch.setattr(workspace, 'NativeWorkspaces', NativeBufferDouble)
    client = MapGISClient(tmp_path)
    with pytest.raises(RuntimeError, match='caller failure'):
        with client.open_workspace('input.wt', 'a' * 64, workspace_id='sdk-window') as ws:
            assert isinstance(ws, Workspace)
            ws.edit('point.move', {'updates': []}, request_id='sdk-move')
            assert ws.revision == 1
            raise RuntimeError('caller failure')
    assert calls[-2:] == [('close', 'sdk-window', {'discard': True}, 1, None), ('release',)]


def test_sdk_and_cli_drc_render_interchange_and_setup_vectors(tmp_path):
    root = tmp_path / '中文 role'
    for host in ['auto', 'claude', 'cursor', 'codex', 'antigravity']:
        completed = _cli('setup-snippet', '--root', root, '--client', host, '--surface', 'hybrid')
        config = json.loads(completed.stdout)
        command = config['mcp_servers' if host == 'codex' else 'mcpServers']['mapgis_local']
        assert Path(command['command']) == Path(sys.executable).resolve()
        assert command['args'][-6:] == ['--root', str(root), '--surface', 'hybrid', '--transport', 'stdio']
        assert not root.exists() and not completed.stderr

    client = MapGISClient(root)
    assert client.probe_abi()['tier'] == json.loads(_cli('doctor', '--root', root).stdout)['abi']['tier']
    assert client.detect_providers() == json.loads(_cli('providers', '--root', root).stdout)['providers']
    assert json.loads(_cli('discover', '--root', root, '--operation-id', 'file.inspect').stdout)['execute_via']
    feature = {'type': 'Feature', 'id': 'unit-1', 'geometry': {'type': 'Point', 'coordinates': [500001, 2800001]},
               'properties': {'CODE': 'A'}, 'text': '灰岩', 'style': {'point_type': 0, 'height': 2, 'width': 2}}
    client.execute('mapgis_prepare_modern_crs', {'features': {'type': 'FeatureCollection', 'features': [feature]},
                   'template': {'name': 'source.wt', 'map_scale': 50000,
                                'crs': {'definition': 'EPSG:4549', 'axis_order': 'xy'}}}, request_id='sdk-create')
    published = client.commit('sdk-create', client.current_epoch())
    source = Path(published['output'])
    digest = sha(source.read_bytes())
    binding = {'file': str(source), 'expected_sha256': digest}
    assert client.place_labels(source, digest)['solver'] == 'greedy_local'
    assert client.render_preview([binding], 'work/preview.png')['path'] == str(root / 'work/preview.png')
    assert (root / 'work/preview.png').read_bytes().startswith(b'\x89PNG')
    drc = root / 'drc.json'
    drc.write_text(json.dumps({'files': [binding], 'reference_crs': 'EPSG:4549'}), encoding='utf-8')
    assert json.loads(_cli('drc', '--root', root, '--arguments', drc).stdout)['files'][0]['sha256'] == digest
    client.export_interchange([binding], 'arcgis_pro', 'deliverables/arcgis')
    assert client.verify_interchange('deliverables/arcgis')['feature_count'] == 1
    drc.write_text(json.dumps({'package_dir': 'deliverables/arcgis'}), encoding='utf-8')
    assert json.loads(_cli('interchange', 'verify', '--root', root, '--arguments', drc).stdout)['feature_count'] == 1
    plan = MapPlan('cli-plan', 'EPSG:4549', 'm', 1000).add_node('inspect', 'file.inspect', inputs=[
        {'role': 'file', 'source_file': str(source), 'expected_sha256': digest}])
    drc.write_text(json.dumps(plan.to_spec()), encoding='utf-8')
    assert json.loads(_cli('plan', 'plan', '--root', root, '--arguments', drc).stdout)['topological_order'] == ['inspect']


def test_stdio_guard_captures_python_print_and_fd_one_during_import(tmp_path):
    injection = '''
import os, runpy, sys
class DiagnosticImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'numpy':
            print('diagnostic-python-stdout', flush=True)
            os.write(1, b'diagnostic-fd-one\\n')
            sys.meta_path.remove(self)
sys.meta_path.insert(0, DiagnosticImport())
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''

    async def check(log):
        params = StdioServerParameters(command=sys.executable, args=['-s', '-B', '-X', 'utf8', '-c', injection,
            str(LAUNCHER), '--root', str(tmp_path / 'protocol'), '--surface', 'hybrid'])
        async with stdio_client(params, errlog=log) as streams, ClientSession(*streams) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 21

    diagnostics = tmp_path / 'diagnostics.txt'
    with diagnostics.open('w', encoding='utf-8') as log:
        asyncio.run(check(log))
    stderr = diagnostics.read_text(encoding='utf-8')
    assert 'diagnostic-python-stdout' in stderr and 'diagnostic-fd-one' in stderr


def test_streamable_http_real_asgi_protocol_and_request_guards(tmp_path):
    assert validate_streamable_http_config('127.0.0.42') == '127.0.0.42'
    assert validate_streamable_http_config('[::1]') == '::1'
    assert validate_streamable_http_config('0.0.0.0', True) == '0.0.0.0'
    for host in ('0.0.0.0', 'example.invalid', '::', 'localhost:8765'):
        with pytest.raises(ValueError):
            validate_streamable_http_config(host, False)
    allowed = {'localhost:8765', '127.0.0.1:8765'}
    assert validate_http_request_headers('LOCALHOST:8765', 'http://localhost:8765', allowed)
    assert not validate_http_request_headers('127.0.0.1:8765', 'http://localhost:8765', allowed)
    assert not validate_http_request_headers('localhost:8765', 'http://localhost:8765/', allowed)
    assert not validate_http_request_headers('localhost:8765', 'https://localhost:8765', allowed)
    assert not validate_http_request_headers('localhost:8765', '', allowed)

    app = streamable_http_app(build_server(tmp_path, 'hybrid'), '127.0.0.1', 8765)
    init = {'jsonrpc': '2.0', 'method': 'initialize', 'id': 1,
            'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                       'clientInfo': {'name': 'test-client', 'version': '1'}}}
    accept = {'accept': 'application/json, text/event-stream'}
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        assert client.post('/mcp', json=init, headers=accept | {'host': 'example.invalid:8765'}).status_code == 421
        assert client.post('/mcp', json=init, headers=accept | {'origin': 'http://example.invalid'}).status_code == 403
        duplicate = [(key, value) for key, value in accept.items()] + [('host', '127.0.0.1:8765'), ('host', 'localhost:8765')]
        assert client.post('/mcp', json=init, headers=duplicate).status_code == 421
        response = client.post('/mcp', json=init, headers=accept | {'origin': 'http://127.0.0.1:8765'})
        assert response.status_code == 200 and response.json()['result']['capabilities']['tools'] is not None
        headers = accept | {'mcp-session-id': response.headers['mcp-session-id'], 'mcp-protocol-version': '2025-11-25'}
        ready = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        assert client.post('/mcp', json=ready, headers=headers).status_code == 202
        reply = client.post('/mcp', json={'jsonrpc': '2.0', 'method': 'tools/list', 'id': 2}, headers=headers)
        assert reply.status_code == 200 and len(reply.json()['result']['tools']) == 21
