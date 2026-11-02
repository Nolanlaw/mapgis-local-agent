import json
import math
import os
import asyncio
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mapgis_agent.core import Bridge,BridgeError,sha

pytestmark=pytest.mark.skipif(os.name!='nt',reason='bundled Windows GDAL runtime profile')
FRAME={'kind':'local','name':'analytical metre plane','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}


def source(tmp_path,flat=False,hole=False,collision=False):
    path=tmp_path/'高程.grd'
    y,x=np.mgrid[6:-1:-1,0:7]
    z=(np.full((7,7),100) if flat else 100+.1*x+.2*y).astype('float32')
    if hole:z[3,3]=np.float32(1.701410009187828e38)
    if collision:
        z=np.full((3,3),10099,np.float32);z[1,1]=100
    h,w=z.shape
    with rasterio.open(path,'w',driver='GSBG',height=h,width=w,count=1,dtype='float32',transform=from_origin(-.5,h-.5,1,1)) as ds:ds.write(z,1)
    metadata=Path(str(path)+'.json')
    metadata.write_text(json.dumps({'schema':'mapgis-agent/grid/1','grid_sha256':sha(path.read_bytes()),'crs':FRAME}),encoding='utf8')
    return path


@pytest.mark.parametrize('operation,expected',[
    ('slope',math.degrees(math.atan(math.sqrt(.05)))),('aspect',206.565051177),
    ('tri',math.sqrt(.3)),('tpi',0),('roughness',.6)])
def test_gdal_terrain_analytical_plane_and_native_grid_frame_are_preserved(tmp_path,operation,expected):
    from mapgis_agent.grid_analysis import prepare_grid_analysis
    grid=source(tmp_path);before=grid.read_bytes();bridge=Bridge(tmp_path)
    result=prepare_grid_analysis(bridge,str(grid),sha(before),{'operation':operation,'vertical_unit':'metre'},
        {'name':operation+'.tif','crs':FRAME},'analysis')
    output=bridge.apply('analysis')
    with rasterio.open(output['output']) as ds:
        values=ds.read(1,masked=True)
        assert values.count()==25
        assert np.max(np.abs(values.compressed()-expected))<.001
        assert ds.transform==from_origin(-.5,6.5,1,1)
    assert result['metrics']['valid_cells']==25
    assert grid.read_bytes()==before
    assert Path(output['output']+'.json').is_file()


def test_flat_zero_slope_stays_valid_and_flat_aspect_is_explicitly_undefined(tmp_path):
    from mapgis_agent.grid_analysis import prepare_grid_analysis
    grid=source(tmp_path,flat=True);bridge=Bridge(tmp_path);fingerprint=sha(grid.read_bytes())
    results={}
    for mode in ['slope','aspect','hillshade']:
        results[mode]=prepare_grid_analysis(bridge,str(grid),fingerprint,{'operation':mode,'vertical_unit':'metre'},
            {'name':mode+'.tif','crs':FRAME},mode)
        bridge.apply(mode)
    assert results['slope']['metrics']['range']==[0,0] and results['slope']['metrics']['valid_cells']==25
    assert results['aspect']['metrics']['valid_cells']==0 and results['aspect']['metrics']['range'] is None
    assert results['hillshade']['metrics']['range']==[181,181]


def test_holes_expand_only_by_the_explicit_gdal_neighborhood_and_units_are_resolved(tmp_path):
    from mapgis_agent.grid_analysis import prepare_grid_analysis
    grid=source(tmp_path,hole=True);bridge=Bridge(tmp_path)
    result=prepare_grid_analysis(bridge,str(grid),sha(grid.read_bytes()),{'operation':'slope','vertical_unit':'foot'},
        {'name':'slope.tif','crs':FRAME},'slope')
    output=bridge.apply('slope')
    with rasterio.open(output['output']) as ds:
        values=ds.read(1,masked=True)
        assert values.count()==16
        assert np.max(np.abs(values.compressed()-math.degrees(math.atan(math.sqrt(.05)*.3048))))<.001
    assert result['parameters']['horizontal_to_vertical_scale']==pytest.approx(1/.3048)


def test_tpi_minus9999_is_valid_and_roundtrips_through_native_msi(tmp_path):
    from mapgis_agent.grid_analysis import prepare_grid_analysis
    from mapgis_agent.native_raster import prepare_native_raster,raster_source_info
    from test_native_raster import native_settings
    grid=source(tmp_path,collision=True);bridge=Bridge(tmp_path);native_settings(tmp_path)
    result=prepare_grid_analysis(bridge,str(grid),sha(grid.read_bytes()),{'operation':'tpi','vertical_unit':'metre'},
        {'name':'tpi.tif','crs':FRAME},'analysis')
    output=bridge.apply('analysis')
    with rasterio.open(output['output']) as ds:
        values=ds.read(1,masked=True)
        assert values.count()==1 and float(values[1,1])==-9999 and ds.nodata!=-9999
    assert result['nodata_collision_pixels']==1
    info=raster_source_info(output['output'])
    prepare_native_raster(bridge,'to_msi',output['output'],output['candidate_sha256'],{'name':'tpi.msi'},'msi',info['members'])
    msi=bridge.apply('msi')
    prepare_native_raster(bridge,'to_tiff',msi['output'],msi['candidate_sha256'],{'name':'back.tif'},'back')
    back=bridge.apply('back')
    with rasterio.open(back['output']) as ds:
        values=ds.read(1,masked=True)
        assert values.count()==1 and float(values[1,1])==-9999


def test_grid_coordinate_metadata_change_blocks_analysis_publication(tmp_path):
    from mapgis_agent.grid_analysis import prepare_grid_analysis
    grid=source(tmp_path);bridge=Bridge(tmp_path)
    prepare_grid_analysis(bridge,str(grid),sha(grid.read_bytes()),{'operation':'slope','vertical_unit':'metre'},
        {'name':'slope.tif','crs':FRAME},'analysis')
    Path(str(grid)+'.json').write_text('{}',encoding='utf8')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('analysis')


def test_explicit_units_options_edges_and_percent_through_mcp(tmp_path):
    from mapgis_agent.server import build_server
    from mapgis_agent.grid_analysis import prepare_grid_analysis
    grid=source(tmp_path);bridge=Bridge(tmp_path);fingerprint=sha(grid.read_bytes())
    unknown=FRAME|{'unit':'unknown'}
    with pytest.raises(BridgeError,match='DEM_KNOWN_LINEAR_UNITS'):
        prepare_grid_analysis(bridge,str(grid),fingerprint,{'operation':'slope','vertical_unit':'metre'},
            {'name':'bad.tif','crs':unknown},'bad-units')
    with pytest.raises(BridgeError,match='DEM_UNEXPECTED_PARAMETERS'):
        prepare_grid_analysis(bridge,str(grid),fingerprint,{'operation':'tpi','vertical_unit':'metre','azimuth':180.},
            {'name':'bad.tif','crs':FRAME},'bad-options')
    async def scenario():
        server=build_server(tmp_path);tool=next(t for t in await server.list_tools() if t.name=='mapgis_prepare_grid_analysis')
        assert tool.annotations.idempotentHint is True
        assert tool.inputSchema['$defs']['SurfaceParameters']['additionalProperties'] is False
        reply=await server.call_tool(tool.name,{'file':str(grid),'expected_sha256':fingerprint,
            'parameters':{'operation':'slope','vertical_unit':'metre','compute_edges':True,'slope_format':'percent'},
            'template':{'name':'percent.tif','crs':FRAME},'request_id':'mcp-slope'})
        result=json.loads(''.join(c.text for c in reply if c.type=='text'))
        assert result['metrics']['valid_cells']==49 and result['value_unit']=='percent'
        output=bridge.apply('mcp-slope')
        with rasterio.open(output['output']) as ds:
            values=ds.read(1,masked=True)
            # GDAL's documented edge interpolation has a different corner
            # stencil; independently check the complete interior plane.
            assert np.max(np.abs(values[1:-1,1:-1]-math.sqrt(.05)*100))<.001
    asyncio.run(scenario())
