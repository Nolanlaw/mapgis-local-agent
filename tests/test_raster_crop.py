"""Window cropping keeps samples, per-band validity and the physical pixel frame."""
import asyncio
import json

import numpy as np
import pytest
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.enums import ColorInterp
from rasterio.transform import Affine
from rasterio.windows import Window

from mapgis_agent.core import Bridge, BridgeError, sha


def crop(root, path, window, identity='crop', members=None):
    from mapgis_agent.raster_crop import prepare_raster_crop
    bridge=Bridge(root)
    receipt=prepare_raster_crop(bridge,str(path),sha(path.read_bytes()),window,
        {'name':identity+'.tif'},identity,members)
    return bridge,receipt,bridge.apply(identity)


def test_rotated_crop_preserves_all_raw_bands_and_independent_nodata_masks(tmp_path):
    values=np.arange(2*8*10,dtype='uint16').reshape(2,8,10)
    values[0,2,3]=0;values[1,3,4]=0
    transform=Affine(2,.3,100,.2,-2,200)
    path=tmp_path/'source.tif'
    with rasterio.open(path,'w',driver='GTiff',width=10,height=8,count=2,dtype='uint16',
        crs='EPSG:3857',transform=transform,nodata=0) as ds:
        ds.write(values);ds.scales=(.5,2);ds.offsets=(10,-5)
        ds.units=('ppm','ppm');ds.descriptions=('W','Sn')
        ds.update_tags(AREA_OR_POINT='Point',mission='test',STATISTICS_MINIMUM='0')
        ds.update_tags(1,description='tungsten',STATISTICS_MAXIMUM='79')
    before=path.read_bytes()
    _,receipt,result=crop(tmp_path,path,[2,1,5,4])
    with rasterio.open(path) as src,rasterio.open(result['output']) as dst:
        assert dst.transform==src.window_transform(Window(2,1,5,4))
        assert dst.crs==src.crs and dst.scales==src.scales and dst.offsets==src.offsets
        assert dst.units==src.units and dst.descriptions==src.descriptions
        np.testing.assert_array_equal(dst.read(),values[:,1:5,2:7])
        np.testing.assert_array_equal(dst.read_masks(),src.read_masks(window=Window(2,1,5,4)))
        assert dst.tags()['mission']=='test' and dst.tags()['AREA_OR_POINT']=='Point'
        assert not any(k.startswith('STATISTICS_') for k in dst.tags())
        assert dst.tags(1)=={'description':'tungsten'}
    assert receipt['pixel_check']=='EXACT_SOURCE_WINDOW_AND_MASKS' and path.read_bytes()==before


def test_external_shared_mask_and_partial_alpha_are_not_binarized(tmp_path):
    from mapgis_agent.native_raster import raster_source_info
    path=tmp_path/'mask.tif'
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False):
        with rasterio.open(path,'w',driver='GTiff',width=6,height=5,count=1,dtype='uint8',
            transform=Affine.translation(10,20)) as ds:
            ds.write(np.zeros((1,5,6),dtype='uint8'))
            ds.write_mask(np.array([[255,255,255,255,255,255]]*5,dtype='uint8'))
        with rasterio.open(path,'r+') as ds:
            mask=ds.read_masks(1);mask[2,2]=0;mask[2,3]=127;ds.write_mask(mask)
    info=raster_source_info(str(path));assert len(info['members'])==2
    bridge,_,result=crop(tmp_path,path,[1,1,4,3],members=info['members'])
    with rasterio.open(result['output']) as dst:
        assert dst.read_masks(1)[1,1:3].tolist()==[0,127] and np.all(dst.read()==0)
    from mapgis_agent.raster_crop import prepare_raster_crop
    prepare_raster_crop(bridge,str(path),sha(path.read_bytes()),[1,1,4,3],{'name':'stale.tif'},'stale',info['members'])
    sidecar=path.with_name(path.name+'.msk');sidecar.write_bytes(sidecar.read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('stale')


def test_gcp_crop_shifts_pixel_positions_without_changing_world_controls_or_inventing_crs(tmp_path):
    path=tmp_path/'gcp.tif'
    points=[GroundControlPoint(col=c,row=r,x=100+c*2,y=200-r*3,z=7,id=str(i),info='control')
        for i,(c,r) in enumerate([(0,0),(10,0),(10,8),(0,8)])]
    with rasterio.open(path,'w',driver='GTiff',width=10,height=8,count=1,dtype='uint8',
        gcps=points,crs='EPSG:32650') as ds:ds.write(np.arange(80,dtype='uint8').reshape(1,8,10))
    _,_,result=crop(tmp_path,path,[2,1,5,4])
    with rasterio.open(path) as src,rasterio.open(result['output']) as dst:
        before,crs=src.gcps;after,newcrs=dst.gcps
        assert dst.crs is None and newcrs==crs
        assert [(g.col-2,g.row-1,g.x,g.y,g.z) for g in before]==[(g.col,g.row,g.x,g.y,g.z) for g in after]


def test_palette_and_nan_nodata_are_preserved_without_nonfinite_json(tmp_path):
    palette=tmp_path/'palette.tif'
    with rasterio.open(palette,'w',driver='GTiff',width=5,height=4,count=1,dtype='uint8') as ds:
        ds.write(np.arange(20,dtype='uint8').reshape(1,4,5));ds.write_colormap(1,{0:(0,0,0,255),1:(255,0,0,255)})
    _,_,result=crop(tmp_path,palette,[1,1,3,2],'palette-crop')
    with rasterio.open(palette) as src,rasterio.open(result['output']) as dst:
        assert dst.colorinterp==(ColorInterp.palette,) and src.colormap(1)==dst.colormap(1)
    path=tmp_path/'nan.tif';values=np.arange(20,dtype='float32').reshape(1,4,5);values[0,2,2]=np.nan
    with rasterio.open(path,'w',driver='GTiff',width=5,height=4,count=1,dtype='float32',nodata=np.nan) as ds:ds.write(values)
    _,receipt,result=crop(tmp_path,path,[1,1,3,2],'nan-crop')
    json.dumps(receipt,allow_nan=False)
    with rasterio.open(result['output']) as dst:
        assert np.isnan(dst.nodata) and dst.read().tobytes()==values[:,1:3,1:4].tobytes()
        assert dst.read_masks(1)[1,1]==0


@pytest.mark.parametrize('window',[[0,0,0,2],[-1,0,2,2],[9,0,2,2],[True,0,2,2],[.5,0,2,2]])
def test_invalid_or_rounded_windows_are_rejected_before_any_request(tmp_path,window):
    path=tmp_path/'source.tif'
    with rasterio.open(path,'w',driver='GTiff',width=10,height=8,count=1,dtype='uint8') as ds:
        ds.write(np.zeros((1,8,10),dtype='uint8'))
    with pytest.raises(BridgeError,match='CROP_WINDOW'):crop(tmp_path,path,window)
    with Bridge(tmp_path).connect() as db:assert db.execute('SELECT count(*) FROM commands').fetchone()[0]==0


def test_public_crop_tool_has_strict_window_schema_and_preserves_source_hash(tmp_path):
    from mapgis_agent.server import build_server
    path=tmp_path/'source.tif'
    with rasterio.open(path,'w',driver='GTiff',width=5,height=4,count=1,dtype='uint8',
        crs='EPSG:3857',transform=Affine(2,0,100,0,-2,200)) as ds:
        ds.write(np.arange(20,dtype='uint8').reshape(1,4,5))
    async def scenario():
        server=build_server(tmp_path);tool=next(t for t in await server.list_tools() if t.name=='mapgis_prepare_raster_crop')
        assert tool.annotations.idempotentHint and tool.inputSchema['properties']['window']['maxItems']==4
        reply=await server.call_tool(tool.name,{'file':str(path),'expected_sha256':sha(path.read_bytes()),
            'window':[1,1,3,2],'template':{'name':'result.tif'},'request_id':'public-crop'})
        return json.loads(''.join(c.text for c in reply if c.type=='text'))
    receipt=asyncio.run(scenario());assert receipt['crop_window']==[1,1,3,2]
    assert Bridge(tmp_path).apply('public-crop')['candidate_sha256']==receipt['candidate_sha256']
