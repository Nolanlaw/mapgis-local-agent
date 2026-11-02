"""Real stdio + BAS60 acceptance; runs only with an explicitly enabled legacy host.

Uses generated WT/WL/WP, a private test root and the configured installation.
Never controls the desktop or edits installation files.
"""
from __future__ import annotations
import asyncio,json,sys,tempfile,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

async def run():
    from mapgis_agent.core import sha,read_native
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    import geopandas as gpd
    from pymapgis import Writer
    from shapely.geometry import Point,LineString,Polygon,mapping
    config=json.loads((ROOT/'config.local.json').read_text(encoding='utf8'))
    if config.get('native_backend_legacy_dep_compatibility') is not True:
        raise RuntimeError('Explicit isolated legacy host opt-in is required in config.local.json')
    total=0
    receipts=[]
    plot_layers=[]
    ROOT.joinpath('work').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-test-',dir=ROOT/'work') as temporary:
        root=Path(temporary)
        (root/'config.local.json').write_text(json.dumps({'mapgis_installation':config['mapgis_installation'],
            'native_backend_legacy_dep_compatibility':True}),encoding='utf8')
        fixtures={'wt':[Point(10,10),Point(20,10)],
                  'wl':[LineString([(0,0),(10,10)]),LineString([(0,10),(10,0)])],
                  'wp':[Polygon([(0,0),(10,0),(10,10),(0,10)])]}
        params=StdioServerParameters(command=sys.executable,args=['-s','-B','-X','utf8',str(ROOT/'scripts/launch_mcp.py'),'--root',str(root)])
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                names={x.name for x in (await session.list_tools()).tools}
                assert {'mapgis_native_file_info','mapgis_prepare_native_operation'}<=names
                async def call(name,**args):
                    nonlocal total
                    response=await session.call_tool(name,args)
                    if response.isError:
                        diagnostics={'response':response.model_dump(mode='json'),'logs':{
                            str(p.relative_to(root)):p.read_text(encoding='utf8',errors='replace')
                            for p in root.rglob('*') if p.is_file() and p.suffix in ('.json','.stdout','.stderr')}}
                        failure=ROOT/'evidence/native-interop/headless-native-selftest-failure.json'
                        failure.parent.mkdir(parents=True,exist_ok=True)
                        failure.write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2),encoding='utf8')
                        raise RuntimeError(response.content)
                    total+=1
                    return response.structuredContent or json.loads(''.join(x.text for x in response.content if hasattr(x,'text')))
                for ext,geometries in fixtures.items():
                    if ext=='wp':
                        # The plain upstream WP writer omits the native drawing
                        # walk. Use the production native-compatible creation route.
                        await call('mapgis_prepare_creation',features={'type':'FeatureCollection','features':[
                            {'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{'ID':9}}
                            for i,g in enumerate(geometries,1)]},template={'name':'region.wp',
                                'crs':{'kind':'local','name':'Synthetic native coordinates','unit':'metre','axis_order':'xy','reference_sha256':'0'*64},
                                'map_scale':1,'style':{'fill_color':8}},request_id='native-wp-fixture')
                        original=Path((await call('mapgis_apply_patch',request_id='native-wp-fixture'))['output'])
                    else:
                        original=root/('original.'+ext)
                        Writer(gpd.GeoDataFrame({'ID':[9]*len(geometries)},geometry=geometries)).to_file(original)
                    original_bytes=original.read_bytes()
                    copy=await call('mapgis_make_work_copy',source=str(original),request_id='copy-'+ext)
                    initial=await call('mapgis_inspect_file',file=copy['path'])
                    info=await call('mapgis_native_file_info',file=copy['path'],expected_sha256=initial['sha256'])
                    assert info['active']==len(geometries)
                    prepared=await call('mapgis_prepare_native_operation',operation='resave',file=copy['path'],
                        expected_sha256=initial['sha256'],parameters={},request_id='save-'+ext)
                    assert prepared['state']=='PREPARED' and prepared['comparison']['records_equal']
                    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
                    saved=await call('mapgis_apply_patch',request_id='save-'+ext)
                    fresh=await call('mapgis_native_file_info',file=saved['output'],expected_sha256=saved['candidate_sha256'])
                    comparison=await call('mapgis_compare_files',before=copy['path'],after=saved['output'])
                    assert comparison['records_equal'] and not comparison['metadata_changes']
                    repeated=await call('mapgis_prepare_native_operation',operation='resave',file=copy['path'],
                        expected_sha256=initial['sha256'],parameters={},request_id='save-'+ext)
                    assert repeated['state']=='APPLIED' and repeated['native_execution']['pid']==prepared['native_execution']['pid']
                    receipts.append({'type':ext,'native_reopen':fresh,'records_equal':True,'no_replay':True})
                    if ext in ('wt','wl','wp'):
                        deleted=await call('mapgis_prepare_native_operation',operation='feature.delete',file=copy['path'],
                            expected_sha256=initial['sha256'],parameters={'record_ids':[1]},request_id='delete-'+ext)
                        assert deleted['comparison']['removed_ids']==[1] and deleted['comparison']['counts']['changed']==0
                        deletion=await call('mapgis_apply_patch',request_id='delete-'+ext)
                        live=await call('mapgis_native_file_info',file=deletion['output'],expected_sha256=deletion['candidate_sha256'])
                        assert live['active']==len(geometries)-1
                        reread=await call('mapgis_inspect_file',file=deletion['output'])
                        remaining=[2] if len(geometries)==2 else []
                        assert [row['record_id'] for row in reread['rows']]==remaining
                        await call('mapgis_prepare_recovery',applied_request='delete-'+ext,later_file=deletion['output'],expected_sha256=deletion['candidate_sha256'],request_id='recover-delete-'+ext)
                        restored_delete=await call('mapgis_apply_patch',request_id='recover-delete-'+ext)
                        assert Path(restored_delete['output']).read_bytes()==original_bytes
                        receipts.append({'native_delete':ext,'physical_ids':remaining,'recovery':'BYTE_IDENTICAL'})

                    if ext=='wt':
                        moved=await call('mapgis_prepare_native_operation',operation='point.move',file=copy['path'],
                            expected_sha256=initial['sha256'],parameters={'updates':[{'record_id':1,'x':12.5,'y':13.75},{'record_id':2,'x':23,'y':14}]},request_id='move')
                        output=await call('mapgis_apply_patch',request_id='move')
                        check=await call('mapgis_native_file_info',file=output['output'],expected_sha256=output['candidate_sha256'])
                        disk=await call('mapgis_inspect_file',file=output['output'])
                        assert check['active']==2 and moved['comparison']['changed_ids']==[1,2]
                        from mapgis_agent.core import read_native
                        actual=read_native(Path(output['output']))[1].geom
                        assert actual[0].x==12.5 and actual[0].y==13.75 and actual[1].x==23 and actual[1].y==14
                        await call('mapgis_prepare_recovery',applied_request='move',later_file=output['output'],expected_sha256=output['candidate_sha256'],request_id='recover-native')
                        restored=await call('mapgis_apply_patch',request_id='recover-native')
                        assert Path(restored['output']).read_bytes()==original_bytes
                        receipts.append({'native_multi_point_move':'PASS','recovery':'BYTE_IDENTICAL'})
                    plot_file=copy['path']
                    plot_hash=initial['sha256']
                    plot_layers.insert(0,{'file':plot_file,'expected_sha256':plot_hash})
                    assert original.read_bytes()==Path(copy['path']).read_bytes()==original_bytes
                plotted=await call('mapgis_prepare_native_plot',layers=plot_layers,
                    view={'native_bounds':[0,0,30,20],'paper_mm':[120,80],'coordinate_frame':'native'},request_id='native-plot')
                assert plotted['state']=='PREPARED' and plotted['render_check']['ink_pixels']>10000
                changed_layer=Path(plot_layers[0]['file']);frozen_layer=changed_layer.read_bytes()
                try:
                    changed_layer.write_bytes(frozen_layer+b'stale')
                    rejected=await session.call_tool('mapgis_apply_patch',{'request_id':'native-plot'})
                    assert rejected.isError and not Path(plotted['output']).exists()
                finally:
                    changed_layer.write_bytes(frozen_layer)
                published=await call('mapgis_apply_patch',request_id='native-plot')
                assert Path(published['output']).read_bytes().startswith(b'%!PS-Adobe-3.0 EPSF-3.0')
                assert Path(published['output']).read_bytes().rstrip().endswith(b'%%EOF')
                repeated_plot=await call('mapgis_prepare_native_plot',layers=plot_layers,
                    view={'native_bounds':[0,0,30,20],'paper_mm':[120,80],'coordinate_frame':'native'},request_id='native-plot')
                assert repeated_plot['state']=='APPLIED' and repeated_plot['native_execution']['pid']==plotted['native_execution']['pid']
                receipts.append({'native_layer_composition_eps':'PASS','page_mm':[120,80],
                    'render_check':plotted['render_check'],'native_process':plotted['native_execution']['pid'],'no_replay':True})
                # Kill only a purpose-built test parent, after its native child
                # finished but before SQLite completion. No production fault hook.
                point_source=next(x for x in plot_layers if x['file'].lower().endswith('.wt'))
                context='import sys,os;from pathlib import Path;sys.path.insert(0,'+repr(str(ROOT/'src'))+');from mapgis_agent.core import Bridge;root=Path('+repr(str(root))+');'
                crash_file=context+"from mapgis_agent import native_backend as n;real=n._invoke\ndef crash(*a,**k):\n real(*a,**k)\n os._exit(91)\nn._invoke=crash\nn.prepare_native_operation(Bridge(root),'point.move',"+repr(point_source['file'])+','+repr(point_source['expected_sha256'])+",{'updates':[{'record_id':1,'x':12.5,'y':13.75}]},'crash-file')"
                stopped=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',crash_file],capture_output=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
                assert stopped.returncode==91,stopped.stderr
                pending=await call('mapgis_get_request',request_id='crash-file');assert pending['state']=='RUNNING'
                reconciled=await call('mapgis_reconcile_native_request',request_id='crash-file')
                assert reconciled['state']=='PREPARED' and reconciled['replayed'] is False
                recovered=await call('mapgis_apply_patch',request_id='crash-file')
                await call('mapgis_native_file_info',file=recovered['output'],expected_sha256=recovered['candidate_sha256'])
                crash_plot=context+"from mapgis_agent import native_plot as n;real=n._render_check\ndef crash(*a,**k):\n real(*a,**k)\n os._exit(92)\nn._render_check=crash\nn.prepare_native_plot(Bridge(root),"+repr(plot_layers)+",{'native_bounds':[0,0,30,20],'paper_mm':[120,80],'coordinate_frame':'native'},'crash-plot')"
                stopped_plot=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',crash_plot],capture_output=True,timeout=45,creationflags=subprocess.CREATE_NO_WINDOW)
                assert stopped_plot.returncode==92,stopped_plot.stderr
                pending_plot=await call('mapgis_get_request',request_id='crash-plot');assert pending_plot['state']=='RUNNING'
                reconciled_plot=await call('mapgis_reconcile_native_request',request_id='crash-plot')
                assert reconciled_plot['state']=='PREPARED' and reconciled_plot['replayed'] is False
                assert reconciled_plot['render_check']['ink_pixels']>10000
                await call('mapgis_apply_patch',request_id='crash-plot')
                receipts.append({'interrupted_native_file_and_plot_parents':'PASS','parent_exit_codes':[91,92],'edit_replayed':False})
                await call('mapgis_prepare_native_project',layers=plot_layers,
                    parameters={'name':'recovery.mpj','coordinate_frame':'native'},request_id='crash-project-source')
                project=await call('mapgis_apply_patch',request_id='crash-project-source')
                point_index=next(i for i,layer in enumerate(plot_layers,1) if layer['file'].lower().endswith('.wt'))
                crash_project=(context+"from mapgis_agent.workspace import NativeWorkspaces;from mapgis_agent.project_workspace import ProjectWorkspaces;"
                    +"b=Bridge(root);leaves=NativeWorkspaces(b);p=ProjectWorkspaces(b,leaves);"
                    +"p.execute('open','crash-project',{'file':"+repr(project['output'])+",'expected_sha256':"+repr(project['candidate_sha256'])+"});"
                    +"p.execute('set_layer','crash-project',{'index':"+str(point_index)+",'state':2},0,'editable');"
                    +"p.execute('edit','crash-project',{'index':"+str(point_index)+",'operation':'point.move','parameters':{'updates':[{'record_id':1,'x':42,'y':43}]}},1,'move');"
                    +"p._rebind=lambda *a,**k:os._exit(93);p.execute('save','crash-project',{},2,'save')")
                stopped_project=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',crash_project],capture_output=True,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
                assert stopped_project.returncode==93,stopped_project.stderr
                from mapgis_agent.core import Bridge
                with Bridge(root).connect() as db:
                    state=json.loads(db.execute("SELECT state FROM project_workspaces WHERE id='crash-project'").fetchone()['state'])
                prior_buffers=[]
                for layer in state['layers']:
                    if layer.get('workspace_id'):
                        metadata=root/'work/native-workspaces'/sha(layer['workspace_id'].encode())[:24]/'state.json'
                        prior_buffers.append(json.loads(metadata.read_text(encoding='utf8')))
                saved_request=state['pending']['result']['id']
                before_recovery=await call('mapgis_get_request',request_id=saved_request)
                restored_project=await call('mapgis_project_workspace',action='recover',workspace_id='crash-project',parameters={},expected_revision=2)
                after_recovery=await call('mapgis_get_request',request_id=saved_request)
                assert restored_project['revision']==3 and not restored_project['dirty']
                assert before_recovery['native_execution']['pid']==after_recovery['native_execution']['pid']
                actual_project=await call('mapgis_native_project_info',file=restored_project['saved_project']['file'],expected_sha256=restored_project['saved_project']['sha256'])
                actual_point=read_native(Path(actual_project['layers'][point_index-1]['file']))[1].geom[0]
                assert (actual_point.x,actual_point.y)==(42,43)
                import psutil
                for prior in prior_buffers:
                    try:
                        process=psutil.Process(prior['native_pid'])
                        assert abs(process.create_time()-prior['native_start'])>=.001 or Path(process.exe()).resolve()!=Path(prior['native_executable']).resolve()
                    except psutil.NoSuchProcess:pass
                await call('mapgis_project_workspace',action='close',workspace_id='crash-project',parameters={},expected_revision=3)
                receipts.append({'interrupted_multilayer_project_parent':'PASS','parent_exit_code':93,'published_save_native_pid_unchanged':True,
                                 'prior_owned_buffers_ended_or_retired_by_verified_identity':True,'edit_replayed':False})

    return {'status':'PASS','tools':len(names),'successful_calls':total,'receipts':receipts,
            'mouse_used':False,'system_dep_changed':False,'originals_preserved':True,'temporary_test_root_removed':True}

if __name__=='__main__':print(json.dumps(asyncio.run(run()),ensure_ascii=False))
