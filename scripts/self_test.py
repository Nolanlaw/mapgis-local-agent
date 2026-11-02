"""Offline synthetic-file acceptance through real stdio; never controls MapGIS UI."""
from __future__ import annotations

import ast
import asyncio
import hashlib
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
                expected |= {'mapgis_find_operations', 'mapgis_describe_operation',
                    'mapgis_register_scan', 'mapgis_scan_window', 'mapgis_prepare_georeference',
                    'mapgis_write_georeferenced_window', 'mapgis_extract_candidates', 'mapgis_merge_candidate_tiles',
                    'mapgis_prepare_creation', 'mapgis_prepare_vector_operation', 'mapgis_search_native_help',
                    'mapgis_prepare_delete', 'mapgis_read_features', 'mapgis_prepare_file_operation',
                    'mapgis_compare_files', 'mapgis_verify_request_output',
                    'mapgis_native_file_info', 'mapgis_prepare_native_operation', 'mapgis_prepare_native_plot', 'mapgis_reconcile_native_request'}
                expected |= {'mapgis_native_workspace', 'mapgis_query_attributes', 'mapgis_prepare_export', 'mapgis_prepare_import',
                    'mapgis_analyze_terrain', 'mapgis_prepare_terrain', 'mapgis_analyze_network', 'mapgis_prepare_network',
                    'mapgis_prepare_native_raster', 'mapgis_prepare_raster_warp', 'mapgis_reconcile_artifact',
                    'mapgis_native_project_info', 'mapgis_prepare_native_project', 'mapgis_prepare_project_plot',
                    'mapgis_projection_points', 'mapgis_prepare_map_frame', 'mapgis_prepare_annotations', 'mapgis_prepare_seams',
                    'mapgis_prepare_attribute_report', 'mapgis_prepare_plot_export'}
                definitions=ast.parse((ROOT/'src/mapgis_agent/server.py').read_text(encoding='utf8'))
                declared={node.name for node in ast.walk(definitions)
                          if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))and node.name.startswith('mapgis_')}
                assert expected<=declared  # Preserve every original core contract.
                assert len(names)==len(declared)and set(names)==declared

                async def call(name, **arguments):
                    nonlocal count
                    reply = await session.call_tool(name, arguments)
                    assert not reply.isError, reply.content
                    count += 1
                    return reply.structuredContent or json.loads(''.join(x.text for x in reply.content if hasattr(x, 'text')))

                await call('mapgis_get_capabilities')
                help_result = await call('mapgis_search_native_help', module='editor', query='拓扑')
                assert help_result['status'] == 'NOT_CONFIGURED' and help_result['execution_verified'] is False
                copied = await call('mapgis_make_work_copy', source=str(source), request_id='copy')
                inspected = await call('mapgis_inspect_file', file=copied['path'])
                assert inspected['count'] == 2
                await call('mapgis_prepare_patch', file=copied['path'], expected_sha256=inspected['sha256'],
                    record_ids=[1], request_id='edit', style={'color': 2})
                applied = await call('mapgis_apply_patch', request_id='edit')
                reread = await call('mapgis_inspect_file', file=applied['output'])
                assert reread['rows'][0]['style']['color'] == 2
                page = await call('mapgis_read_features', file=copied['path'],
                    expected_sha256=inspected['sha256'], offset=0, limit=1)
                assert page['returned'] == 1 and page['next_offset'] == 1
                assert page['collection']['features'][0]['id'] == '1'
                assert page['collection']['features'][0]['geometry']['coordinates'] == [[0.0,0.0],[10.0,10.0]]
                assert page['collection']['features'][0]['properties']['ID'] == 7
                diff = await call('mapgis_compare_files', before=copied['path'], after=applied['output'])
                assert diff['counts']['changed'] == 1 and diff['changed_ids'] == [1]
                verification = await call('mapgis_verify_request_output', request_id='edit', file=applied['output'])
                assert verification['matches_candidate_records']
                mismatch = await call('mapgis_verify_request_output', request_id='edit', file=copied['path'])
                assert not mismatch['matches_candidate_records']
                duplicate = await call('mapgis_apply_patch', request_id='edit')
                assert duplicate['reconciled_existing_output']
                await call('mapgis_get_request', request_id='edit')
                await call('mapgis_prepare_recovery', applied_request='edit', later_file=applied['output'],
                    expected_sha256=reread['sha256'], request_id='recover')
                recovered = await call('mapgis_apply_patch', request_id='recover')
                assert Path(recovered['output']).read_bytes() == baseline
                await call('mapgis_prepare_delete', file=copied['path'], expected_sha256=inspected['sha256'],
                    record_ids=[1], request_id='delete')
                deleted = await call('mapgis_apply_patch', request_id='delete')
                deletion_read = await call('mapgis_read_features', file=deleted['output'],
                    expected_sha256=hashlib.sha256(Path(deleted['output']).read_bytes()).hexdigest())
                assert [f['id'] for f in deletion_read['collection']['features']] == ['2']
                import struct
                assert struct.unpack_from('<I', Path(deleted['output']).read_bytes(), 260)[0] == 1
                removed = await call('mapgis_compare_files', before=copied['path'], after=deleted['output'])
                assert removed['removed_ids'] == [1] and removed['counts']['changed'] == 0
                await call('mapgis_prepare_recovery', applied_request='delete', later_file=deleted['output'],
                    expected_sha256=hashlib.sha256(Path(deleted['output']).read_bytes()).hexdigest(), request_id='undelete')
                undeleted = await call('mapgis_apply_patch', request_id='undelete')
                assert Path(undeleted['output']).read_bytes() == baseline
                await call('mapgis_prepare_patch', file=copied['path'], expected_sha256=inspected['sha256'],
                    record_ids=[1], request_id='cancel', style={'color': 3})
                await call('mapgis_cancel_request', request_id='cancel')
                catalog = await call('mapgis_find_operations')
                assert catalog['count'] >= 34
                file_detail = await call('mapgis_describe_operation', operation_id='vector.process_file')
                assert file_detail['execution_status'] == 'MCP_TOOL_AVAILABLE'
                assert 'expected_sha256' in file_detail['input_schema']['properties']
                detail = await call('mapgis_describe_operation', operation_id='scan.interactive_trace')
                assert detail['executable_by_mcp'] is False
                for operation, workflow in [('projection.point','projection'),
                                             ('terrain.contours','terrain'),
                                             ('analysis.buffer','analysis')]:
                    detail = await call('mapgis_describe_operation',operation_id=operation)
                    assert detail['executable_by_mcp'] is True
                    assert detail['execution_status']=='MCP_TOOL_AVAILABLE' and detail['input_schema']
                    assert detail['workflow_knowledge']['id'] == workflow
                    assert detail['workflow_knowledge']['steps']
                import cv2
                import numpy as np
                scan_file = owned / 'scan.png'
                rgb = np.full((64,64,3),(200,180,220),np.uint8)
                cv2.imencode('.png',cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR))[1].tofile(str(scan_file))
                scan_hash = hashlib.sha256(scan_file.read_bytes()).hexdigest()
                scan = await call('mapgis_register_scan',source=str(scan_file),request_id='scan')
                assert scan['width'] == 64 and scan['source_sha256'] == scan_hash
                tile = await call('mapgis_scan_window',scan_id='scan',window=[0,0,64,64],request_id='tile',max_dimension=64)
                assert tile['plan']['pixel_to_source'] == [1,0,0,0,1,0]
                crs = {'kind':'local','name':'Synthetic metre grid','unit':'metre',
                       'axis_order':'xy','reference_sha256':scan_hash}
                fit = await call('mapgis_prepare_georeference',scan_id='scan',crs=crs,
                    gcps=[{'pixel':[0,0],'world':[1000,2000]},
                          {'pixel':[64,0],'world':[1064,2000]},
                          {'pixel':[0,64],'world':[1000,1936]}],
                    checkpoints=[{'pixel':[32,32],'world':[1032,1968]}],
                    max_error=0.001,request_id='fit')
                assert fit['accepted'] and fit['checkpoints'][0]['error'] < 0.001
                geotiff = await call('mapgis_write_georeferenced_window',scan_id='scan',window_id='tile',georeference_id='fit',request_id='geotiff')
                import rasterio
                with rasterio.open(geotiff['output']) as reader:
                    assert reader.shape == (64,64) and len(reader.gcps[0]) == 4
                    assert abs(reader.gcps[0][0].x-1000) < 1e-8, reader.gcps
                    assert abs(reader.gcps[0][2].y-1936) < 1e-8, reader.gcps
                candidates = await call('mapgis_extract_candidates',scan_id='scan',window_id='tile',
                    config={'mode':'areas','georeference_id':'fit',
                            'palette':[{'id':'one','rgb':[200,180,220]}],
                            'color_tolerance':0,'min_area_px':1,'max_fill_px':0},request_id='areas')
                features = json.loads(Path(candidates['features']).read_text(encoding='utf8'))
                assert len(features['features']) == 1
                tile_config={'mode':'areas','palette':[{'id':'one','rgb':[200,180,220]}],
                             'color_tolerance':0,'min_area_px':1}
                for side,window in [('left',[0,0,40,64]),('right',[24,0,40,64])]:
                    await call('mapgis_scan_window',scan_id='scan',window=window,request_id=side,max_dimension=64)
                    await call('mapgis_extract_candidates',scan_id='scan',window_id=side,
                               config=tile_config,request_id=side+'-areas')
                merged=await call('mapgis_merge_candidate_tiles',scan_id='scan',tiles=[
                    {'request_id':'left-areas','core':[0,0,32,64]},
                    {'request_id':'right-areas','core':[32,0,32,64]}],request_id='merged',georeference_id='fit')
                assert merged['qa']['overlap_disagreement_px2']==0
                assert merged['qa']['candidate_count']==1
                from shapely.geometry import shape
                merged_features=json.loads(Path(merged['features']).read_text(encoding='utf8'))['features']
                assert abs(shape(merged_features[0]['geometry']).area-4096)<1e-6
                await call('mapgis_prepare_creation',features=features,
                    template={'name':'regions.wp','crs':crs,'map_scale':50000,'style':{'fill_color':8}},request_id='new-areas')
                created = await call('mapgis_apply_patch',request_id='new-areas')
                reread = await call('mapgis_inspect_file',file=created['output'])
                assert reread['count'] == 1 and reread['rows'][0]['style']['fill_color'] == 8
                await call('mapgis_prepare_vector_operation',operation_id='geometry.buffer',features=features,
                    template={'name':'buffer.wp','crs':crs,'map_scale':50000},
                    parameters={'distance':2},request_id='buffer')
                buffered = await call('mapgis_apply_patch',request_id='buffer')
                from pymapgis import Reader
                assert Reader(buffered['output']).geom[0].area > Reader(created['output']).geom[0].area
                await call('mapgis_prepare_file_operation', operation_id='geometry.buffer', file=created['output'],
                    expected_sha256=reread['sha256'], record_ids=[1],
                    template={'name':'file-buffer.wp','crs':crs,'map_scale':50000},
                    parameters={'distance':2}, request_id='file-buffer')
                file_buffered = await call('mapgis_apply_patch', request_id='file-buffer')
                actual_buffer = Reader(file_buffered['output']).geom[0]
                expected_buffer = Reader(created['output']).geom[0].buffer(2)
                assert actual_buffer.equals_exact(expected_buffer, 1e-9, normalize=True)
                assert file_buffered['qa']['native_source']['physical_record_ids'] == [1]
                invalid = await session.call_tool('mapgis_get_live_state', {'pid': 0})
                assert invalid.isError
                assert source.read_bytes() == baseline
    return {'status': 'PASS', 'stdio_tools': len(names), 'successful_calls': count, 'expected_live_error': True,
        'scan_affine_geotiff_candidates_native_creation_buffer': 'PASS',
        'independent_tile_merge_no_duplicate_area': 'PASS',
        'native_features_processing_comparison_delete_recovery_and_saved_verification': 'PASS',
        'synthetic_records': 2, 'source_preserved': True, 'recovery_byte_identical': True,
        'temporary_files_removed': True, 'desktop_ui_test': 'not performed by this self-test'}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
