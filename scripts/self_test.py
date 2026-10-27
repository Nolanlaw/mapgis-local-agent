"""Offline synthetic-file acceptance through real stdio; never controls MapGIS UI."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


async def run() -> dict:
    import geopandas as gpd
    from pymapgis import Writer
    from shapely.geometry import LineString
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    (ROOT / 'work').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='self-test-', dir=ROOT / 'work') as temporary:
        owned = Path(temporary)
        source = owned / 'synthetic.WL'
        Writer(gpd.GeoDataFrame({'ID': [7, 7], '名称': ['甲', '乙']},
            geometry=[LineString([(0, 0), (10, 10)]), LineString([(0, 10), (10, 0)])])).to_file(source)
        baseline = source.read_bytes()
        params = StdioServerParameters(command=sys.executable, args=['-s', '-B', '-X', 'utf8',
            str(ROOT / 'scripts/launch_mcp.py'), '--root', str(owned)])
        count = 0
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                names = [x.name for x in (await session.list_tools()).tools]
                expected = {'mapgis_get_capabilities', 'mapgis_get_live_state', 'mapgis_inspect_file',
                    'mapgis_make_work_copy', 'mapgis_prepare_patch', 'mapgis_apply_patch', 'mapgis_get_request',
                    'mapgis_cancel_request', 'mapgis_prepare_recovery'}
                assert len(names) == 9 and set(names) == expected

                async def call(name, **arguments):
                    nonlocal count
                    reply = await session.call_tool(name, arguments)
                    assert not reply.isError, reply.content
                    count += 1
                    return reply.structuredContent or json.loads(''.join(x.text for x in reply.content if hasattr(x, 'text')))

                await call('mapgis_get_capabilities')
                copied = await call('mapgis_make_work_copy', source=str(source), request_id='copy')
                inspected = await call('mapgis_inspect_file', file=copied['path'])
                assert inspected['count'] == 2
                await call('mapgis_prepare_patch', file=copied['path'], expected_sha256=inspected['sha256'],
                    record_ids=[1], request_id='edit', style={'color': 2})
                applied = await call('mapgis_apply_patch', request_id='edit')
                reread = await call('mapgis_inspect_file', file=applied['output'])
                assert reread['rows'][0]['style']['color'] == 2
                duplicate = await call('mapgis_apply_patch', request_id='edit')
                assert duplicate['reconciled_existing_output']
                await call('mapgis_get_request', request_id='edit')
                await call('mapgis_prepare_recovery', applied_request='edit', later_file=applied['output'],
                    expected_sha256=reread['sha256'], request_id='recover')
                recovered = await call('mapgis_apply_patch', request_id='recover')
                assert Path(recovered['output']).read_bytes() == baseline
                await call('mapgis_prepare_patch', file=copied['path'], expected_sha256=inspected['sha256'],
                    record_ids=[1], request_id='cancel', style={'color': 3})
                await call('mapgis_cancel_request', request_id='cancel')
                invalid = await session.call_tool('mapgis_get_live_state', {'pid': 0})
                assert invalid.isError
                assert source.read_bytes() == baseline
    return {'status': 'PASS', 'stdio_tools': 9, 'successful_calls': count, 'expected_live_error': True,
        'synthetic_records': 2, 'source_preserved': True, 'recovery_byte_identical': True,
        'temporary_files_removed': True, 'desktop_ui_test': 'not performed by this self-test'}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
