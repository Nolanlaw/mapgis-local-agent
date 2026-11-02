from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mapgis_agent.core import Bridge,BridgeError,sha
from test_native_raster import native_settings


def masked_source(tmp_path):
    source=tmp_path/'masked.tif'
    pixels=np.arange(24*32,dtype=np.uint8).reshape(1,24,32)
    valid=np.ones((24,32),dtype=np.uint8)*255;valid[5:10,8:12]=0
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=1,dtype='uint8',
            crs='EPSG:32650',transform=from_origin(400,900,2,2)) as raster:
            raster.write(pixels);raster.write_mask(valid)
    return source,pixels,valid


def test_raster_info_counts_and_fingerprints_the_actual_validity_mask(tmp_path):
    from mapgis_agent.native_raster import raster_info
    source,_,valid=masked_source(tmp_path)
    result=raster_info(source)
    assert result['valid_pixels_by_band']==[24*32-20]
    assert result['mask_sha256']==sha(valid.tobytes())


def test_native_msi_import_never_silently_drops_a_tiff_validity_mask(tmp_path):
    from mapgis_agent.native_raster import prepare_native_raster
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    source,_,_=masked_source(tmp_path);before=source.read_bytes()
    with pytest.raises(BridgeError,match='NATIVE_RASTER_MASK'):
        prepare_native_raster(bridge,'to_msi',str(source),sha(before),{},'masked-import')
    assert source.read_bytes()==before


def test_native_msi_rejects_partial_alpha_instead_of_reinterpreting_it_as_an_opaque_band(tmp_path):
    from mapgis_agent.native_raster import prepare_native_raster
    bridge=Bridge(tmp_path);native_settings(tmp_path);source=tmp_path/'partial-alpha.tif'
    rgba=np.ones((4,8,8),dtype='uint8')*255;rgba[3]=128
    with rasterio.open(source,'w',driver='GTiff',width=8,height=8,count=4,dtype='uint8',
        crs='EPSG:32650',transform=from_origin(400,900,2,2)) as raster:raster.write(rgba)
    with pytest.raises(BridgeError,match='NATIVE_RASTER_MASK'):
        prepare_native_raster(bridge,'to_msi',str(source),sha(source.read_bytes()),{},'partial-alpha')


def test_warp_preserves_explicit_mask_without_treating_valid_zero_as_nodata(tmp_path):
    from mapgis_agent.native_raster import prepare_raster_warp
    bridge=Bridge(tmp_path);source,pixels,valid=masked_source(tmp_path)
    prepared=prepare_raster_warp(bridge,str(source),sha(source.read_bytes()),{
        'target_crs':{'definition':'EPSG:32650','axis_order':'xy'},
        'bounds':[400,852,464,900],'resolution':[2,2]},'masked-warp')
    result=bridge.apply('masked-warp')
    with rasterio.open(result['output']) as actual:
        assert actual.nodata is None
        assert np.array_equal(actual.dataset_mask(),valid)
        assert actual.dataset_mask()[0,0]==255 and actual.read()[0,0,0]==0
        assert np.array_equal(actual.read()[0][valid>0],pixels[0][valid>0])
    assert prepared['mask_policy']=='INTERSECTION_OF_WARPED_BAND_MASKS'


def test_external_mask_is_fingerprinted_snapshotted_and_checked_before_publication(tmp_path):
    from mapgis_agent.native_raster import raster_source_info,prepare_raster_warp,prepare_native_raster
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    source,pixels,valid=masked_source(tmp_path)
    external=tmp_path/'external.tif'
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False):
        with rasterio.open(external,'w',driver='GTiff',width=32,height=24,count=1,dtype='uint8',
            crs='EPSG:32650',transform=from_origin(400,900,2,2)) as raster:
            raster.write(pixels);raster.write_mask(valid)
    digest=sha(external.read_bytes());info=raster_source_info(str(external),digest)
    members=info['members']
    assert set(members)=={'external.tif','external.tif.msk'}
    parameters={'target_crs':{'definition':'EPSG:32650','axis_order':'xy'},
        'bounds':[400,852,464,900],'resolution':[2,2]}
    with pytest.raises(BridgeError,match='RASTER_MEMBER_HASHES_REQUIRED'):
        prepare_raster_warp(bridge,str(external),digest,parameters,'missing-members')
    with pytest.raises(BridgeError,match='NATIVE_RASTER_MASK'):
        prepare_native_raster(bridge,'to_msi',str(external),digest,{},'external-native',expected_members=members)
    prepare_raster_warp(bridge,str(external),digest,parameters,'external-warp',expected_members=members)
    with rasterio.open(external.with_name('external.tif.msk'),'r+') as mask:
        mask.write(np.ones((1,24,32),dtype='uint8')*255)
    assert sha(external.read_bytes())==digest
    with pytest.raises(BridgeError,match='STALE_SOURCE'):
        bridge.apply('external-warp')
    with pytest.raises(BridgeError,match='STALE_RASTER_MEMBER'):
        prepare_raster_warp(bridge,str(external),digest,parameters,'stale-mask',expected_members=members)


@pytest.mark.parametrize('use_gcps',[False,True])
def test_local_grid_rectification_keeps_the_declared_native_frame_without_a_fake_earth_projection(tmp_path,use_gcps):
    from rasterio.control import GroundControlPoint
    from mapgis_agent.native_raster import prepare_raster_warp
    bridge=Bridge(tmp_path);source=tmp_path/'local.tif';values=np.arange(9,dtype='uint8').reshape(3,3)
    local=rasterio.crs.CRS.from_wkt('LOCAL_CS["survey illustration",UNIT["metre",1],AXIS["Easting",EAST],AXIS["Northing",NORTH]]')
    spatial={'gcps':[GroundControlPoint(col=c,row=r,x=100+c,y=203-r) for c,r in [(0,0),(3,0),(3,3),(0,3)]]} if use_gcps else {'transform':from_origin(100,203,1,1)}
    with rasterio.open(source,'w',driver='GTiff',width=3,height=3,count=1,dtype='uint8',crs=local,**spatial) as ds:ds.write(values,1)
    frame={'kind':'local','name':'survey illustration','unit':'metre','axis_order':'xy','reference_sha256':sha(source.read_bytes())}
    parameters={'target_crs':frame,'bounds':[100,200,103,203],'resolution':[1,1]}
    prepared=prepare_raster_warp(bridge,str(source),sha(source.read_bytes()),parameters,'local-warp')
    with rasterio.open(bridge.apply('local-warp')['output']) as ds:
        assert ds.crs==local and not ds.gcps[0] and ds.transform==from_origin(100,203,1,1)
        np.testing.assert_array_equal(ds.read(1),values)
        np.testing.assert_array_equal(ds.read_masks(1),np.full((3,3),255,dtype='uint8'))
    assert prepared['source_preserved']
    for field,value in [('name','different survey'),('unit','millimetre')]:
        with pytest.raises(BridgeError,match='LOCAL_FRAME_MISMATCH'):
            prepare_raster_warp(bridge,str(source),sha(source.read_bytes()),{**parameters,'target_crs':{**frame,field:value}},'wrong-'+field)


def test_warp_does_not_add_a_column_to_an_integral_grid_due_to_float_roundoff(tmp_path):
    from mapgis_agent.native_raster import prepare_raster_warp
    b=Bridge(tmp_path);source=tmp_path/'decimal.tif';left=11854.45396353604;top=28207.347894221628;step=6.;width=890;height=892
    with rasterio.open(source,'w',driver='GTiff',width=width,height=height,count=1,dtype='uint8',
            crs='EPSG:3857',transform=from_origin(left,top,step,step)) as ds:ds.write(np.ones((height,width),dtype='uint8'),1)
    prepare_raster_warp(b,str(source),sha(source.read_bytes()),{'bounds':[left,top-height*step,left+width*step,top],
        'resolution':[step,step],'target_crs':{'definition':'EPSG:3857','axis_order':'xy'}},'decimal')
    with rasterio.open(b.apply('decimal')['output']) as ds:assert (ds.width,ds.height)==(width,height)
