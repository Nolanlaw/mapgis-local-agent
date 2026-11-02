"""Cold source stdio prevents regression of Windows Fortran/pipe initialization stall."""
import asyncio,json,sys
from datetime import timedelta
from pathlib import Path
import rasterio
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from mapgis_agent.raster import file_sha
from test_hydrology import dem,PARAMETERS,TEMPLATE


def test_cold_hydrology_actual_stdio_computes_and_publishes(tmp_path):
    source,_=dem(tmp_path);before=(file_sha(source),source.stat().st_mtime_ns)
    entry=Path(__file__).resolve().parents[1]/'scripts/launch_mcp.py'
    async def scenario():
        async with stdio_client(StdioServerParameters(command=sys.executable,args=['-s','-B','-X','utf8',str(entry),'--root',str(tmp_path)])) as streams:
            async with ClientSession(*streams,read_timeout_seconds=timedelta(seconds=45)) as session:
                await session.initialize()
                async def call(name,args):
                    reply=await session.call_tool(name,args)
                    assert not reply.isError,reply
                    return reply.structuredContent or json.loads(''.join(c.text for c in reply.content if c.type=='text'))
                prepared=await call('mapgis_prepare_hydrology',{'file':str(source),'expected_sha256':before[0],
                    'parameters':PARAMETERS|{'vector_outputs':False},'template':TEMPLATE,'request_id':'cold'})
                assert prepared['metrics']['outlet_upstream_cell_sum']==49
                published=await call('mapgis_apply_patch',{'request_id':'cold'})
                state=await call('mapgis_get_request',{'request_id':'cold'})
                assert state['state']=='APPLIED' and state['output_matches']
                return published
    published=asyncio.run(scenario())
    folder=Path(published['output']).parent
    with rasterio.open(folder/published['rasters']['upstream_cells']['file']) as ds:assert ds.read(1)[6,3]==49
    assert (file_sha(source),source.stat().st_mtime_ns)==before
