"""Real MCP/native session acceptance. Uses generated files, never desktop input."""
from __future__ import annotations
import asyncio,json,sys,tempfile,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


async def run():
    import geopandas as gpd
    from pymapgis import Writer
    from shapely.geometry import Point
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mapgis_agent.core import sha,read_native,native_ids
    config=json.loads((ROOT/'config.local.json').read_text(encoding='utf8'))
    assert config.get('native_backend_legacy_dep_compatibility') is True
    (ROOT/'work').mkdir(exist_ok=True)
    total=0
    with tempfile.TemporaryDirectory(prefix='workspace-test-',dir=ROOT/'work') as temporary:
        root=Path(temporary)
        (root/'config.local.json').write_text(json.dumps({k:config[k] for k in
            ('mapgis_installation','native_backend_legacy_dep_compatibility')}),encoding='utf8')
        source=root/'points.wt'
        Writer(gpd.GeoDataFrame({'ID':[7,7]},geometry=[Point(10,10),Point(20,10)])).to_file(source)
        original=source.read_bytes()
        params=StdioServerParameters(command=sys.executable,args=['-s','-B','-X','utf8',str(ROOT/'scripts/launch_mcp.py'),'--root',str(root)])
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                names={t.name for t in (await session.list_tools()).tools}
                async def call(action,parameters=None,revision=None,request=None,error=False):
                    nonlocal total
                    args={'action':action,'workspace_id':'buffer','parameters':parameters or {}}
                    if revision is not None:args['expected_revision']=revision
                    if request is not None:args['request_id']=request
                    response=await session.call_tool('mapgis_native_workspace',args)
                    assert bool(response.isError)==error,response.content
                    if error:return
                    total+=1
                    return response.structuredContent or json.loads(''.join(c.text for c in response.content if hasattr(c,'text')))
                opened=await call('open',{'source':str(source),'expected_sha256':sha(original)})
                assert opened['revision']==0 and opened['dirty'] is False and opened['active']==2
                native_pid=opened['native_pid']
                edit={'operation':'point.move','parameters':{'updates':[{'record_id':1,'x':12.5,'y':13.75}]}}
                moved=await call('edit',edit,0,'move')
                assert moved['revision']==1 and moved['dirty'] and moved['native_pid']==native_pid
                assert source.read_bytes()==original
                repeated=await call('edit',edit,0,'move')
                assert repeated['revision']==1 and repeated['replayed'] is False
                await call('edit',{'operation':'feature.delete','parameters':{'record_ids':[2]}},0,'stale',error=True)
                await call('edit',{'operation':'point.move','parameters':{'updates':[{'record_id':1,'x':99,'y':99}]}},1,'move',error=True)
                saved=await call('save',{},1,'save')
                assert saved['dirty'] is False and Path(saved['output']).exists()
                _,reader,_=read_native(Path(saved['output']))
                assert (reader.geom[0].x,reader.geom[0].y)==(12.5,13.75)
                deleted=await call('edit',{'operation':'feature.delete','parameters':{'record_ids':[2]}},1,'delete')
                assert deleted['revision']==2 and deleted['active']==1 and deleted['dirty']
                await call('close',{},2,error=True)
                closed=await call('close',{'discard':True},2)
                assert not closed['running'] and not closed['dirty']
                reopened=await call('recover',{},closed['revision'])
                assert reopened['native_pid']!=native_pid and reopened['active']==2 and not reopened['dirty']
                await call('close',{},reopened['revision'])
                assert source.read_bytes()==original
        return {'status':'PASS','stdio_tools':len(names),'successful_calls':total,
                'real_persistent_native_pid':native_pid,'dirty_revision_conflict_save_discard_recover':'PASS',
                'source_preserved':True,'mouse_used':False}


if __name__=='__main__':print(json.dumps(asyncio.run(run()),ensure_ascii=False))
