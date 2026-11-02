"""Real MCP acceptance for the headless modules; --native also exercises installed 6.7."""
from __future__ import annotations

import argparse,asyncio,json,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


async def run(native=False,keep_work=False):
    import numpy as np
    import rasterio
    from rasterio.control import GroundControlPoint
    from shapely.geometry import Point,LineString,Polygon,box,mapping
    from mapgis_agent.core import sha,read_native,annotation_text,native_ids
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    local={'kind':'local','name':'analytic MCP grid','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}
    tm={'definition':'+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}
    called=[];summary={};originals={}
    (ROOT/'work').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='modules-',dir=ROOT/'work',delete=not keep_work) as temporary:
        root=Path(temporary)
        if keep_work:print(json.dumps({'diagnostic_work':str(root)}),file=sys.stderr,flush=True)
        if native:
            native_config=json.loads((ROOT/'config.local.json').read_text(encoding='utf8'))
            assert native_config.get('native_backend_legacy_dep_compatibility') is True
            (root/'config.local.json').write_text(json.dumps({k:native_config[k] for k in ('mapgis_installation','native_backend_legacy_dep_compatibility')}),encoding='utf8')
        params=StdioServerParameters(command=sys.executable,args=['-s','-B','-X','utf8',str(ROOT/'scripts/launch_mcp.py'),'--root',str(root)])
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize();names={t.name for t in (await session.list_tools()).tools}
                async def call(name,error=False,**arguments):
                    response=await session.call_tool(name,arguments)
                    assert bool(response.isError)==error,(name,arguments.get('request_id'),response.content)
                    if error:return None
                    called.append(name)
                    return response.structuredContent or json.loads(''.join(c.text for c in response.content if hasattr(c,'text')))
                async def apply(identity):
                    result=await call('mapgis_apply_patch',request_id=identity)
                    assert (await call('mapgis_get_request',request_id=identity))['output_matches']
                    return result
                async def create(identity,geometries,properties,suffix,styles=None):
                    await call('mapgis_prepare_creation',features={'type':'FeatureCollection','features':[
                        {'type':'Feature','id':str(i),'geometry':mapping(g),'properties':p,
                         **({'style':styles[i-1]} if styles else {})}
                        for i,(g,p) in enumerate(zip(geometries,properties),1)]},
                        template={'name':identity+'.'+suffix,'crs':local,'map_scale':1000},request_id=identity)
                    result=await apply(identity);originals[result['output']]=Path(result['output']).read_bytes();return result
                def template(name,crs=local):return {'name':name,'crs':crs,'map_scale':1000}
                points=[Point(x,y) for y in range(0,101,25) for x in range(0,101,25)]
                nodes=await create('nodes',points,[{'ID':7,'ELEV':100+.1*p.x+.2*p.y,'code':'A','label':'点'+str(i)} for i,p in enumerate(points)],'wt')
                query=await call('mapgis_query_attributes',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],sql='SELECT COUNT(*) AS n,SUM(ELEV) AS z FROM features')
                assert query['rows']==[[25,2875.0]]
                await call('mapgis_query_attributes',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],sql='DROP TABLE features',error=True)
                terrain=await call('mapgis_analyze_terrain',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],height_field='ELEV',base_height=100)
                assert abs(terrain['planimetric_hull_area']-10000)<1e-7 and abs(terrain['cut_volume']-150000)<1e-6
                terrain_results={}
                settings={'tin':{},'grid':{'bounds':[0,0,100,100],'step':10},
                    'contours':{'grid':{'bounds':[0,0,100,100],'step':10},'levels':[105,110,115,120,125]}}
                for operation,suffix in [('tin','wp'),('grid','wt'),('contours','wl')]:
                    await call('mapgis_prepare_terrain',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],height_field='ELEV',
                        parameters={'operation':operation}|settings[operation],template=template(operation+'.'+suffix),request_id=operation)
                    terrain_results[operation]=await apply(operation)
                _,grid,_=read_native(Path(terrain_results['grid']['output']));assert len(grid.geom)==121
                _,contours,_=read_native(Path(terrain_results['contours']['output']));assert sorted(set(contours.data['elevation']))==[105,110,115,120,125]
                await call('mapgis_prepare_annotations',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],field='label',template=template('labels.wt'),request_id='labels')
                labels=await apply('labels');blob,reader,sections=read_native(Path(labels['output']))
                assert annotation_text(blob,sections,1)=='点0' and len(reader.geom)==25
                await call('mapgis_prepare_attribute_report',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],sql='SELECT code,COUNT(*) AS n FROM features GROUP BY code',request_id='report')
                report=await apply('report');assert json.loads(Path(report['output']).read_text(encoding='utf8'))['rows']==[['A',25]]
                editable=await create('editable',[LineString([(0,0),(10,0)]),LineString([(10,0),(20,0)])],[{'ID':7,'code':'A'}]*2,'wl')
                donut=await create('donut',[Polygon([(0,0),(10,0),(10,10),(0,10)],
                    [[(1,1),(2,1),(2,2),(1,2)]])],[{'ID':7,'code':'A'}],'wp')
                edits=[('geometry.replace',editable,{'updates':[{'feature_id':'1','geometry':mapping(LineString([(0,0),(5,2),(10,0)]))}]},'wl',2),
                    ('line.split',editable,{'splitter':mapping(Point(3,0))},'wl',3),
                    ('polygon.split',donut,{'splitter':mapping(LineString([(5,-1),(5,11)]))},'wp',2),
                    ('line.merge',editable,{},'wl',1),('line.reverse',editable,{},'wl',2),
                    ('line.offset',editable,{'distance':-2},'wl',2),
                    ('line.substring',editable,{'start':2,'end':7},'wl',2),
                    ('geometry.densify',editable,{'max_segment_length':3},'wl',2)]
                for operation,source_edit,options,suffix,expected_count in edits:
                    identity='edit-'+operation.replace('.','-')
                    await call('mapgis_prepare_file_operation',operation_id=operation,file=source_edit['output'],
                        expected_sha256=source_edit['candidate_sha256'],template=template(identity+'.'+suffix),parameters=options,request_id=identity)
                    edited=await apply(identity);reader=read_native(Path(edited['output']))[1]
                    assert len(reader.geom)==expected_count and set(reader.data['code'])=={'A'}
                    if operation=='polygon.split':assert sorted(g.area for g in reader.geom)==[49,50]
                    if operation=='line.offset':assert all(g.bounds[1]==g.bounds[3]==-2 for g in reader.geom)
                    if operation=='line.merge':assert list(reader.geom[0].coords)==[(0,0),(10,0),(20,0)]
                    if operation=='line.substring':assert all(g.length==5 for g in reader.geom)
                    if operation=='geometry.densify':assert all(len(g.coords)==5 for g in reader.geom)
                    if native:
                        native_edit=await call('mapgis_prepare_native_operation',operation='resave',file=edited['output'],
                            expected_sha256=edited['candidate_sha256'],parameters={},request_id=identity+'-native')
                        assert native_edit['native_execution']['pid']!=native_edit['native_reopen']['pid']
                        assert native_edit['comparison']['records_equal']
                        await apply(identity+'-native')
                summary['geometry_editing_operations']=len(edits)
                for label,schema_source in [('points',nodes),('lines',terrain_results['contours']),('areas',terrain_results['tin'])]:
                    identity='schema-'+label
                    source_description=await call('mapgis_inspect_file',file=schema_source['output'],limit=1)
                    id_field=({'name':'ID','source':'ID'} if any(f[0]=='ID' for f in source_description['fields'])
                              else {'name':'ID','type':'int','default':7})
                    schema_result=await call('mapgis_prepare_attribute_schema',file=schema_source['output'],expected_sha256=schema_source['candidate_sha256'],
                        fields=[id_field,{'name':'岩性','type':'string','length':8,'default':'砂岩'},
                                {'name':'value','type':'double','default':1},
                                {'name':'日期','type':'date','default':'2026-10-29'},
                                {'name':'时间','type':'time','default':'15:10:20.125'}],request_id=identity)
                    assert schema_result['non_attribute_sections_byte_identical']
                    schema_file=await apply(identity)
                    calculated=await call('mapgis_query_attributes',file=schema_file['output'],expected_sha256=schema_file['candidate_sha256'],
                        sql='SELECT __record_id,value+__record_id AS value FROM features WHERE __record_id IN (1,2) ORDER BY __record_id')
                    await call('mapgis_prepare_patch',file=schema_file['output'],expected_sha256=schema_file['candidate_sha256'],record_ids=[1,2],
                        attributes={'日期':'2024-02-29','时间':'00:00:00.000001'},
                        attribute_updates={str(row[0]):{'value':row[1]} for row in calculated['rows']},request_id=identity+'-calculate')
                    calculated_file=await apply(identity+'-calculate')
                    assert read_native(Path(calculated_file['output']))[1].data['value'].tolist()[:2]==[2,3]
                    row=read_native(Path(calculated_file['output']))[1].data.iloc[0]
                    assert row['日期'].isoformat()=='2024-02-29' and row['时间'].isoformat()=='00:00:00.000001'
                    if native:
                        saved_schema=await call('mapgis_prepare_native_operation',operation='resave',file=calculated_file['output'],
                            expected_sha256=calculated_file['candidate_sha256'],parameters={},request_id=identity+'-native')
                        assert saved_schema['comparison']['records_equal']
                        await apply(identity+'-native')
                summary['native_attribute_schema_and_calculation_formats']=3
                summary['native_date_time_patch_formats']=3
                for driver in ('GeoJSON','GPKG','ESRI Shapefile'):
                    identity='export-'+driver.split()[0].lower()
                    await call('mapgis_prepare_export',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],driver=driver,request_id=identity)
                    exported=await apply(identity)
                    if driver!='ESRI Shapefile':
                        imported=identity+'-import'
                        await call('mapgis_prepare_import',file=exported['output'],expected_sha256=exported['candidate_sha256'],template=template(imported+'.wt'),request_id=imported)
                        restored=await apply(imported);_,r,_=read_native(Path(restored['output']))
                        assert len(r.geom)==25 and r.data['ID'].tolist()==[7]*25
                    else:
                        source_info=await call('mapgis_vector_source_info',file=exported['output'])
                        imported=identity+'-import'
                        await call('mapgis_prepare_import',file=exported['output'],expected_sha256=exported['candidate_sha256'],
                            expected_members=source_info['members'],template=template(imported+'.wt'),request_id=imported)
                        restored=await apply(imported);_,r,_=read_native(Path(restored['output']))
                        assert len(r.geom)==25 and r.data['ID'].tolist()==[7]*25
                        summary['shapefile_multifile_import']='MEMBER_HASHES_AND_ATTRIBUTES_PRESERVED'
                lines=[LineString([(0,0),(2,2)]),LineString([(0,0),(1,0)]),LineString([(1,0),(2,2)]),LineString([(2,2),(3,2)]),LineString([(1,0),(3,2)])]
                network=await create('network',lines,[{'ID':7,'cost':cost} for cost in (4.,2.,1.,5.,8.)],'wl')
                route_parameters={'operation':'route','source':[0,0],'target':[3,2],'directed':True,'cost_field':'cost'}
                route=await call('mapgis_analyze_network',file=network['output'],expected_sha256=network['candidate_sha256'],parameters=route_parameters)
                assert route['summary']['cost']==8 and route['summary']['source_record_ids']==[2,3,4]
                await call('mapgis_prepare_network',file=network['output'],expected_sha256=network['candidate_sha256'],parameters=route_parameters,template=template('route.wl'),request_id='route');await apply('route')
                parts=[]
                for i,geometry in enumerate([box(0,0,10.004,10),box(10.006,0,20,10)]):
                    part=await create('tile'+str(i),[geometry],[{'code':'A','ID':7}],'wp');parts.append({'file':part['output'],'expected_sha256':part['candidate_sha256']})
                await call('mapgis_prepare_seams',files=parts,parameters={'field':'code','grid_size':.1},template=template('joined.wp'),request_id='seams')
                joined=await apply('seams');assert abs(read_native(Path(joined['output']))[1].geom[0].area-200)<1e-8
                projected=await call('mapgis_projection_points',points=[[117,0]],source_crs={'definition':'EPSG:4326','axis_order':'xy'},target_crs=tm)
                assert np.allclose(projected['points'],[[500000,0]],atol=1e-7,rtol=0)
                await call('mapgis_prepare_map_frame',part='border',parameters={'geographic_bounds':[116,39,118,40],'grid_step_degrees':.5,'subdivisions':8},
                    template=template('frame.wl',tm),request_id='frame');await apply('frame')
                source=root/'scan.tif';array=np.zeros((3,24,32),np.uint8);array[0,:,:16]=255;array[2,:,16:]=255
                gcps=[GroundControlPoint(col=x,row=y,x=X,y=Y) for x,y,X,Y in [(0,0,400,900),(32,0,464,900),(32,24,464,852),(0,24,400,852)]]
                with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=3,dtype='uint8',gcps=gcps,crs='EPSG:32650') as raster:raster.write(array)
                originals[str(source)]=source.read_bytes()
                await call('mapgis_prepare_raster_warp',file=str(source),expected_sha256=sha(source.read_bytes()),parameters={'name':'rectified.tif',
                    'target_crs':{'definition':'EPSG:32650','axis_order':'xy'},'bounds':[400,852,464,900],'resolution':[2,2],'resampling':'nearest'},request_id='warp')
                rectified=await apply('warp')
                with rasterio.open(rectified['output']) as raster:assert np.array_equal(raster.read(),array)
                if native:
                    import cv2
                    library_folder=Path(native_config['mapgis_installation'])/'Slib'
                    library_info=await call('mapgis_system_library_info',folder=str(library_folder))
                    for member in library_info['members']:
                        path=library_folder/member['name'];originals[str(path)]=path.read_bytes()
                    symbol_primitives=[{'type':'polyline','parts':[[[-.4,-.4],[.4,.4]]],'color':1},
                                       {'type':'polyline','parts':[[[-.4,.4],[.4,-.4]]],'color':1}]
                    pattern_primitives=[{'type':'polyline','parts':[[[0,0],[1,1]]],'color':1},
                                        {'type':'polyline','parts':[[[0,1],[1,0]]],'color':1}]
                    library_edit=await call('mapgis_prepare_system_library',folder=str(library_folder),
                        expected_manifest_sha256=library_info['manifest_sha256'],changes=[
                            {'kind':'symbol','action':'append','primitives':symbol_primitives},
                            {'kind':'line','action':'append','dash_fraction':.5},
                            {'kind':'pattern','action':'append','primitives':pattern_primitives}],request_id='system-library')
                    library_output=await apply('system-library')
                    binding={'folder':str(Path(library_output['output']).parent),'expected_manifest_sha256':library_edit['manifest_sha256']}
                    checked_library=await call('mapgis_system_library_info',folder=binding['folder'],expected_manifest_sha256=binding['expected_manifest_sha256'],
                        kind='pattern',ids=[library_edit['changes'][2]['id']])
                    assert checked_library['records'][0]['primitives'][1]['parts']==pattern_primitives[1]['parts']
                    library_layers=[]
                    for tag,geometry,suffix,style in [
                        ('library-symbol',Point(10,10),'wt',{'point_type':1,'symbol_no':library_edit['changes'][0]['id'],'height':40,'width':40,'color':1}),
                        ('library-line',LineString([(0,10),(20,10)]),'wl',{'linetype':library_edit['changes'][1]['id'],'color':1,'width':.2}),
                        ('library-pattern',box(0,0,20,20),'wp',{'fill_color':2,'pattern':library_edit['changes'][2]['id'],'pattern_color':6,'pattern_height':5,'pattern_width':5})]:
                        layer=await create(tag,[geometry],[{'ID':7}],suffix,styles=[style]);library_layers.insert(0,{'file':layer['output'],'expected_sha256':layer['candidate_sha256']})
                        plotted=await call('mapgis_prepare_native_plot',layers=[{'file':layer['output'],'expected_sha256':layer['candidate_sha256']}],
                            view={'native_bounds':[0,0,20,20],'paper_mm':[100,100],'coordinate_frame':'native'},system_library=binding,request_id=tag+'-plot')
                        await apply(tag+'-plot')
                        assert plotted['system_library']['manifest_sha256']==library_edit['manifest_sha256']
                        pixels=cv2.imdecode(np.frombuffer(Path(plotted['render_check']['preview']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
                        if tag=='library-pattern':
                            _,counts=np.unique(pixels.reshape(-1,3),axis=0,return_counts=True)
                            assert sorted(counts,reverse=True)[1]>200
                    await call('mapgis_prepare_native_project',layers=library_layers,parameters={'name':'symbols.mpj','coordinate_frame':'native'},request_id='library-project')
                    library_project=await apply('library-project')
                    await call('mapgis_prepare_project_plot',file=library_project['output'],expected_sha256=library_project['candidate_sha256'],
                        view={'native_bounds':[0,0,20,20],'paper_mm':[100,100],'coordinate_frame':'native'},system_library=binding,request_id='library-project-plot')
                    library_project_plot=await apply('library-project-plot')
                    assert library_project_plot['system_library']['manifest_sha256']==library_edit['manifest_sha256']
                    summary['native_system_library']='APPEND_STABLE_IDS_PRESERVE_OLD_PAYLOADS_REOPEN_DRAW_SYMBOL_DASH_PATTERN_AND_PROJECT'
                    arc_source=await create('arc-regions',[box(0,0,10,10),box(10,0,20,10)],[{'ID':7,'code':'A'}]*2,'wp',
                        styles=[{'fill_color':3},{'fill_color':4}])
                    arc_report=await call('mapgis_read_wp_arcs',file=arc_source['output'],expected_sha256=arc_source['candidate_sha256'],limit=100)
                    shared=next(arc for arc in arc_report['arcs'] if arc['owner_region_ids']==[1,2])
                    arc_update={'arc_id':shared['arc_id'],'coordinates':[shared['coordinates'][0],[12,5],shared['coordinates'][-1]]}
                    arc_edit=await call('mapgis_prepare_native_operation',operation='arc.replace',file=arc_source['output'],expected_sha256=arc_source['candidate_sha256'],
                        parameters={'updates':[arc_update]},request_id='native-arc')
                    arc_file=await apply('native-arc')
                    assert [g.area for g in read_native(Path(arc_file['output']))[1].geom]==[110,90]
                    assert arc_edit['comparison']['changed_ids']==[1,2]
                    arc_info=await call('mapgis_native_file_info',file=arc_file['output'],expected_sha256=arc_file['candidate_sha256'])
                    await call('mapgis_prepare_native_plot',layers=[{'file':arc_file['output'],'expected_sha256':arc_file['candidate_sha256']}],
                        view={'native_bounds':arc_info['native_bounds'],'paper_mm':[160,80],'coordinate_frame':'native'},request_id='arc-plot')
                    arc_plot=await apply('arc-plot');assert arc_plot['render_check']['ink_pixels']>10000
                    node_coordinates={n['node_id']:n['coordinates'] for n in arc_report['nodes']}
                    node_updates=[{'node_id':nid,'x':12,'y':node_coordinates[nid][1]} for nid in shared['node_ids']]
                    node_edit=await call('mapgis_prepare_native_operation',operation='node.move',file=arc_source['output'],expected_sha256=arc_source['candidate_sha256'],
                        parameters={'updates':node_updates},request_id='native-nodes')
                    node_file=await apply('native-nodes')
                    assert [g.area for g in read_native(Path(node_file['output']))[1].geom]==[120,80]
                    assert node_edit['comparison']['edited_node_ids']==sorted(shared['node_ids'])
                    node_info=await call('mapgis_native_file_info',file=node_file['output'],expected_sha256=node_file['candidate_sha256'])
                    await call('mapgis_prepare_native_plot',layers=[{'file':node_file['output'],'expected_sha256':node_file['candidate_sha256']}],
                        view={'native_bounds':node_info['native_bounds'],'paper_mm':[160,80],'coordinate_frame':'native'},request_id='node-plot')
                    await apply('node-plot')
                    await call('mapgis_native_workspace',action='open',workspace_id='node-edit',parameters={'source':arc_source['output'],'expected_sha256':arc_source['candidate_sha256']})
                    node_buffer=await call('mapgis_native_workspace',action='edit',workspace_id='node-edit',parameters={'operation':'node.move','parameters':{'updates':node_updates}},
                        expected_revision=0,request_id='node-buffer')
                    assert node_buffer['dirty']
                    node_saved=await call('mapgis_native_workspace',action='save',workspace_id='node-edit',parameters={},expected_revision=1,request_id='node-save')
                    assert not node_saved['dirty']
                    await call('mapgis_native_workspace',action='close',workspace_id='node-edit',parameters={},expected_revision=1)
                    summary['native_shared_nodes']='ALL_INCIDENT_ENDPOINTS_SAVED_REOPENED_RENDERED_AND_DIRTY_BUFFER_SAVED'
                    await call('mapgis_native_workspace',action='open',workspace_id='arc-edit',parameters={'source':arc_source['output'],'expected_sha256':arc_source['candidate_sha256']})
                    arc_buffer=await call('mapgis_native_workspace',action='edit',workspace_id='arc-edit',parameters={
                        'operation':'arc.replace','parameters':{'updates':[arc_update]}},expected_revision=0,request_id='arc-buffer')
                    assert arc_buffer['dirty'] and arc_buffer['active']==2
                    arc_saved=await call('mapgis_native_workspace',action='save',workspace_id='arc-edit',parameters={},expected_revision=1,request_id='arc-buffer-save')
                    assert not arc_saved['dirty']
                    await call('mapgis_native_workspace',action='close',workspace_id='arc-edit',parameters={},expected_revision=1)
                    summary['native_shared_arc']='OWNERS_PRESERVED_SAVED_REOPENED_RENDERED'
                    await call('mapgis_prepare_native_project',layers=[
                        {'file':arc_file['output'],'expected_sha256':arc_file['candidate_sha256']},
                        {'file':editable['output'],'expected_sha256':editable['candidate_sha256']}],
                        parameters={'name':'edit.mpj','coordinate_frame':'native'},request_id='project-edit-source')
                    project_source=await apply('project-edit-source')
                    await call('mapgis_prepare_project_edit',file=project_source['output'],expected_sha256=project_source['candidate_sha256'],
                        layers=[{'source_index':2,'state':3,'display_limits':[0,0]},{'source_index':1,'state':0,'display_limits':[3,6]},
                                {'file':arc_source['output'],'expected_sha256':arc_source['candidate_sha256'],'state':2}],request_id='project-edit')
                    edited_project=await apply('project-edit')
                    edited_info=await call('mapgis_native_project_info',file=edited_project['output'],expected_sha256=edited_project['candidate_sha256'])
                    assert [l['kind'] for l in edited_info['layers']]==[0,2,2]
                    assert [l['state'] for l in edited_info['layers']]==[3,0,2]
                    assert [l['display_limits'] for l in edited_info['layers']]==[[0,0],[3,6],[0,0]]
                    await call('mapgis_prepare_project_plot',file=edited_project['output'],expected_sha256=edited_project['candidate_sha256'],
                        view={'native_bounds':arc_info['native_bounds'],'paper_mm':[160,80],'coordinate_frame':'native'},request_id='edited-project-plot')
                    edited_plot=await apply('edited-project-plot');assert edited_plot['render_check']['ink_pixels']>10000
                    summary['native_project_edit']='ORDER_STATE_APPEND_SAVED_REOPENED_RENDERED'
                    await call('mapgis_prepare_native_project',layers=[
                        {'file':nodes['output'],'expected_sha256':nodes['candidate_sha256']},
                        {'file':editable['output'],'expected_sha256':editable['candidate_sha256']},
                        {'file':arc_source['output'],'expected_sha256':arc_source['candidate_sha256']}],
                        parameters={'name':'session.mpj','coordinate_frame':'native'},request_id='session-source')
                    session_source=await apply('session-source');originals[session_source['output']]=Path(session_source['output']).read_bytes()
                    session_open=await call('mapgis_project_workspace',action='open',workspace_id='project-session',
                        parameters={'file':session_source['output'],'expected_sha256':session_source['candidate_sha256']})
                    selected=await call('mapgis_project_workspace',action='select',workspace_id='project-session',
                        parameters={'mode':'replace','layers':[{'index':1,'record_ids':[1,2]},{'index':2,'record_ids':[1]}]},
                        expected_revision=0,request_id='session-select')
                    assert selected['selected_count']==3
                    await call('mapgis_project_workspace',action='set_layer',workspace_id='project-session',
                        parameters={'index':1,'state':2},expected_revision=1,request_id='session-editable')
                    session_edit=await call('mapgis_project_workspace',action='edit',workspace_id='project-session',
                        parameters={'index':1,'use_selection':True,'operation':'point.move','parameters':{'updates':[{'record_id':1,'x':12,'y':13}]}},
                        expected_revision=2,request_id='session-move')
                    assert session_edit['dirty'] and session_edit['layers'][0]['native_pid']==session_open['layers'][0]['native_pid']
                    session_saved=await call('mapgis_project_workspace',action='save',workspace_id='project-session',parameters={},expected_revision=3,request_id='session-save')
                    assert not session_saved['dirty'] and session_saved['revision']==4
                    saved_info=await call('mapgis_native_project_info',file=session_saved['saved_project']['file'],expected_sha256=session_saved['saved_project']['sha256'])
                    assert list(read_native(Path(saved_info['layers'][0]['file']))[1].geom[0].coords)==[(12,13)]
                    await call('mapgis_project_workspace',action='close',workspace_id='project-session',parameters={},expected_revision=4)
                    session_recovered=await call('mapgis_project_workspace',action='recover',workspace_id='project-session',parameters={},expected_revision=5)
                    assert not session_recovered['dirty'] and session_recovered['selection']=={'1':[1,2],'2':[1]}
                    await call('mapgis_project_workspace',action='close',workspace_id='project-session',parameters={},expected_revision=session_recovered['revision'])
                    summary['native_project_workspace']='MULTILAYER_SELECTION_DIRTY_EDIT_ATOMIC_PROJECT_SAVE_AND_RECOVERY'
                    await call('mapgis_native_workspace',action='open',workspace_id='line-edit',parameters={'source':editable['output'],'expected_sha256':editable['candidate_sha256']})
                    line_edit=await call('mapgis_native_workspace',action='edit',workspace_id='line-edit',parameters={
                        'operation':'line.replace','parameters':{'updates':[{'record_id':1,'coordinates':[[0,0],[5,2],[10,0]]}]}},expected_revision=0,request_id='line-vertices')
                    assert line_edit['dirty'] and line_edit['revision']==1
                    line_save=await call('mapgis_native_workspace',action='save',workspace_id='line-edit',parameters={},expected_revision=1,request_id='line-save')
                    assert not line_save['dirty']
                    await call('mapgis_native_workspace',action='close',workspace_id='line-edit',parameters={},expected_revision=1)
                    summary['native_line_workspace']='SAVED_AND_REOPENED'
                    wp=terrain_results['tin']
                    opened=await call('mapgis_native_workspace',action='open',workspace_id='wp',parameters={'source':wp['output'],'expected_sha256':wp['candidate_sha256']})
                    edited=await call('mapgis_native_workspace',action='edit',workspace_id='wp',parameters={'operation':'feature.delete','parameters':{'record_ids':[1,2]}},expected_revision=0,request_id='delete')
                    assert edited['dirty'] and edited['active']==30 and edited['native_pid']==opened['native_pid']
                    saved=await call('mapgis_native_workspace',action='save',workspace_id='wp',parameters={},expected_revision=1,request_id='save')
                    assert not saved['dirty']
                    await call('mapgis_native_workspace',action='edit',workspace_id='wp',parameters={'operation':'feature.delete','parameters':{'record_ids':[3]}},expected_revision=1,request_id='delete2')
                    await call('mapgis_native_workspace',action='close',workspace_id='wp',parameters={},expected_revision=2,error=True)
                    closed=await call('mapgis_native_workspace',action='close',workspace_id='wp',parameters={'discard':True},expected_revision=2)
                    recovered=await call('mapgis_native_workspace',action='recover',workspace_id='wp',parameters={},expected_revision=closed['revision'])
                    assert recovered['active']==30 and not recovered['dirty']
                    await call('mapgis_native_workspace',action='close',workspace_id='wp',parameters={},expected_revision=recovered['revision'])
                    await call('mapgis_prepare_native_raster',operation='to_msi',file=rectified['output'],expected_sha256=rectified['candidate_sha256'],parameters={},request_id='msi')
                    msi=await apply('msi')
                    await call('mapgis_prepare_native_raster',operation='to_tiff',file=msi['output'],expected_sha256=msi['candidate_sha256'],parameters={},request_id='tiff')
                    tiff=await apply('tiff')
                    with rasterio.open(tiff['output']) as raster:assert np.array_equal(raster.read(),array) and raster.crs.to_epsg()==32650
                    await call('mapgis_prepare_native_project',layers=[{'file':msi['output'],'expected_sha256':msi['candidate_sha256']}],parameters={'name':'map.mpj','coordinate_frame':'native'},request_id='project')
                    project=await apply('project')
                    info=await call('mapgis_native_project_info',file=project['output'],expected_sha256=project['candidate_sha256'])
                    assert info['layers'][0]['native_bounds']==[400,852,464,900]
                    await call('mapgis_prepare_project_plot',file=project['output'],expected_sha256=project['candidate_sha256'],view={'native_bounds':[400,852,464,900],'paper_mm':[160,120],'coordinate_frame':'native'},request_id='plot')
                    plot=await apply('plot');assert plot['render_check']['ink_pixels']>100000
                    await call('mapgis_reconcile_artifact',request_id='plot')
                    await call('mapgis_prepare_plot_export',file=plot['output'],expected_sha256=plot['candidate_sha256'],
                        parameters={'formats':['pdf','tiff'],'dpi':144},request_id='print-exports')
                    print_exports=await apply('print-exports')
                    assert print_exports['checks']['pdf']['preview_matches_eps']
                    assert print_exports['checks']['tiff']['size_px']==[907,680]
                    summary.update(native_wp_dirty_save_recover=True,native_msi_pixels='EXACT',native_project_relocation='PASS',native_project_eps='RENDERED')
                assert all(Path(file).read_bytes()==blob for file,blob in originals.items())
                summary.update(tools=len(names),successful_calls=len(called),distinct_tools=sorted(set(called)),source_preserved=True,native=native)
    print(json.dumps({'status':'PASS',**summary},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--native',action='store_true');parser.add_argument('--keep-work',action='store_true');options=parser.parse_args()
    asyncio.run(run(options.native,options.keep_work))
