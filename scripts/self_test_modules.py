"""Real MCP acceptance for the headless modules; --native also exercises installed 6.7."""
from __future__ import annotations

import argparse,asyncio,json,subprocess,sys,tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


async def run(native=False):
    import numpy as np
    import rasterio
    from rasterio.control import GroundControlPoint
    from shapely.geometry import Point,LineString,box,mapping
    from mapgis_agent.core import sha,read_native,annotation_text,native_ids
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    local={'kind':'local','name':'analytic MCP grid','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}
    tm={'definition':'+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}
    called=[];summary={};originals={}
    (ROOT/'work').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='modules-',dir=ROOT/'work') as temporary:
        root=Path(temporary)
        if native:
            settings=json.loads((ROOT/'config.local.json').read_text(encoding='utf8'))
            assert settings.get('native_backend_legacy_dep_compatibility') is True
            (root/'config.local.json').write_text(json.dumps({k:settings[k] for k in ('mapgis_installation','native_backend_legacy_dep_compatibility')}),encoding='utf8')
        params=StdioServerParameters(command=sys.executable,args=['-s','-B','-X','utf8',str(ROOT/'scripts/launch_mcp.py'),'--root',str(root)])
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize();names={t.name for t in (await session.list_tools()).tools}
                async def call(name,error=False,**arguments):
                    response=await session.call_tool(name,arguments)
                    assert bool(response.isError)==error,(name,response.content)
                    if error:return None
                    called.append(name)
                    return response.structuredContent or json.loads(''.join(c.text for c in response.content if hasattr(c,'text')))
                async def apply(identity):
                    result=await call('mapgis_apply_patch',request_id=identity)
                    assert (await call('mapgis_get_request',request_id=identity))['output_matches']
                    return result
                async def create(identity,geometries,properties,suffix):
                    await call('mapgis_prepare_creation',features={'type':'FeatureCollection','features':[
                        {'type':'Feature','id':str(i),'geometry':mapping(g),'properties':p}
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
                for driver in ('GeoJSON','GPKG','ESRI Shapefile'):
                    identity='export-'+driver.split()[0].lower()
                    await call('mapgis_prepare_export',file=nodes['output'],expected_sha256=nodes['candidate_sha256'],driver=driver,request_id=identity)
                    exported=await apply(identity)
                    if driver!='ESRI Shapefile':
                        imported=identity+'-import'
                        await call('mapgis_prepare_import',file=exported['output'],expected_sha256=exported['candidate_sha256'],template=template(imported+'.wt'),request_id=imported)
                        restored=await apply(imported);_,r,_=read_native(Path(restored['output']))
                        assert len(r.geom)==25 and r.data['ID'].tolist()==[7]*25
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
    parser=argparse.ArgumentParser();parser.add_argument('--native',action='store_true');options=parser.parse_args()
    asyncio.run(run(options.native))
