"""Mosaic validity is independent of sample zero; AOI keeps holes and grids."""
from pathlib import Path
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin,Affine
from shapely.geometry import box,Polygon,Point,mapping
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.native_raster import raster_source_info
from mapgis_agent.raster import file_sha

FRAME={'kind':'local','name':'composite test grid','unit':'unknown','axis_order':'xy','reference_sha256':'0'*64}


def write(file,data,transform=None,mask=None,nodata=None,crs=None,external=False):
    data=np.asarray(data)
    if data.ndim==2:data=data[None]
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=not external),rasterio.open(file,'w',driver='GTiff',width=data.shape[2],height=data.shape[1],count=len(data),dtype=data.dtype,
        transform=transform or from_origin(0,data.shape[1],1,1),nodata=nodata,crs=crs) as ds:
        ds.write(data)
        if mask is not None:ds.write_mask(mask)
    info=raster_source_info(str(file),file_sha(file))
    return {'file':str(file),'expected_sha256':file_sha(file),'expected_members':info['members']}


def mosaic(bridge,files,parameters,identity='mosaic'):
    from mapgis_agent.raster_composite import prepare_raster_mosaic
    receipt=prepare_raster_mosaic(bridge,files,{'method':'first','bounds':[0.,0.,5.,3.],'resolution':[1.,1.],'crs':FRAME,**parameters},
        {'name':'mosaic.tif'},identity)
    return receipt,Path(bridge.apply(identity)['output'])


def mask(bridge,file,geometry,**parameters):
    from mapgis_agent.raster_composite import prepare_raster_mask
    info=raster_source_info(str(file),file_sha(file))
    receipt=prepare_raster_mask(bridge,str(file),file_sha(file),{'geometry':mapping(geometry),'crs':FRAME,'crop':True,'all_touched':False,**parameters},
        {'name':'masked.tif'},'mask',expected_members=info['members'])
    return receipt,Path(bridge.apply('mask')['output'])


@pytest.mark.parametrize('method',['first','last','mean'])
def test_mosaic_matches_independent_padded_arrays_and_keeps_valid_zero_and_a_missing_hole(tmp_path,method):
    b=Bridge(tmp_path);a=np.arange(9,dtype='uint8').reshape(3,3);other=np.arange(30,39,dtype='uint8').reshape(3,3)
    valid=np.full((3,3),255,dtype='uint8');valid[1,1]=0
    files=[write(tmp_path/'a.tif',a,mask=valid),write(tmp_path/'b.tif',other,transform=from_origin(2,3,1,1))]
    protected={Path(f['file']):file_sha(Path(f['file'])) for f in files}
    receipt,output=mosaic(b,files,{'method':method})
    layers=np.zeros((2,3,5),dtype='float64');exists=np.zeros((2,3,5),dtype=bool)
    layers[0,:,:3]=a;exists[0,:,:3]=valid==255;layers[1,:,2:]=other;exists[1,:,2:]=True
    count=exists.sum(axis=0);result=np.zeros((3,5))
    for row in range(3):
        for col in range(5):
            values=layers[:,row,col][exists[:,row,col]]
            if len(values):result[row,col]=values[0] if method=='first' else values[-1] if method=='last' else values.mean()
    with rasterio.open(output) as ds:
        np.testing.assert_array_equal(ds.read(1),result)
        np.testing.assert_array_equal(ds.read_masks(1),(count>0).astype('uint8')*255)
        assert ds.read(1)[0,0]==0 and ds.read_masks(1)[0,0]==255 and ds.read_masks(1)[1,1]==0
        assert ds.transform==from_origin(0,3,1,1) and ds.nodata is None
        assert ds.dtypes==('float64' if method=='mean' else 'uint8',)
    assert receipt['source_preserved'] and all(file_sha(f)==value for f,value in protected.items())


def test_mosaic_explicit_safe_widening_and_finite_nodata_are_native_compatible(tmp_path):
    from mapgis_agent.native_raster import _native_mask_profile,raster_info
    b=Bridge(tmp_path);data=np.arange(9,dtype='uint8').reshape(3,3);valid=np.full((3,3),255,dtype='uint8');valid[1,1]=0
    source=write(tmp_path/'a.tif',data,mask=valid)
    _,output=mosaic(b,[source],{'bounds':[0,0,3,3],'dtype':'uint16','nodata':65535})
    with rasterio.open(output) as ds:
        assert ds.dtypes==('uint16',) and ds.nodata==65535 and ds.read(1)[1,1]==65535
        assert ds.read(1)[0,0]==0 and ds.read_masks(1)[0,0]==255
    _native_mask_profile(output,raster_info(output))


def test_mosaic_and_aoi_accept_an_embedded_matching_local_crs_and_refuse_a_different_named_grid(tmp_path):
    b=Bridge(tmp_path);local=rasterio.crs.CRS.from_wkt('LOCAL_CS["composite test grid",UNIT["metre",1],AXIS["Easting",EAST],AXIS["Northing",NORTH]]')
    source=write(tmp_path/'local.tif',np.arange(9,dtype='uint8').reshape(3,3),crs=local)
    frame={**FRAME,'unit':'metre','reference_sha256':source['expected_sha256']}
    _,merged=mosaic(b,[source],{'bounds':[0,0,3,3],'crs':frame})
    _,masked=mask(b,merged,box(0,0,2,3),crs=frame)
    with rasterio.open(merged) as a,rasterio.open(masked) as c:
        assert a.crs==c.crs==local
        np.testing.assert_array_equal(c.read(1),a.read(1)[:,:2])
    for change in [{'name':'another grid'},{'unit':'millimetre'},{'unit':'unknown'}]:
        with pytest.raises(BridgeError,match='COMPOSITE_FRAME'):
            mosaic(b,[source],{'bounds':[0,0,3,3],'crs':{**frame,**change}},'wrong-'+str(len(change))+str(change))


def test_mosaic_never_marks_valid_zero_missing_even_when_the_source_nodata_is_overridden_by_its_real_mask(tmp_path):
    b=Bridge(tmp_path);data=np.array([[0,1,2],[3,4,5],[6,7,8]],dtype='uint8');valid=np.full((3,3),255,dtype='uint8')
    source=write(tmp_path/'a.tif',data,mask=valid,nodata=0)
    _,output=mosaic(b,[source],{'bounds':[0,0,3,3]})
    with rasterio.open(output) as ds:assert ds.read_masks(1)[0,0]==255 and ds.read(1)[0,0]==0
    with pytest.raises(BridgeError,match='COMPOSITE_NODATA_COLLISION'):
        mosaic(b,[source],{'bounds':[0,0,3,3],'nodata':0},'collision')


@pytest.mark.parametrize('change',[{'resolution':[.5,.5]},{'bounds':[.5,0,5.5,3]}])
def test_mosaic_refuses_implicit_resampling_or_half_pixel_shifts(tmp_path,change):
    b=Bridge(tmp_path);source=write(tmp_path/'a.tif',np.ones((3,3),dtype='uint8'))
    with pytest.raises(BridgeError,match='MOSAIC_GRID'):mosaic(b,[source],change)


def test_mosaic_rejects_partial_alpha_and_different_band_masks_instead_of_losing_validity(tmp_path):
    b=Bridge(tmp_path);valid=np.full((3,3),255,dtype='uint8');valid[1,1]=127
    source=write(tmp_path/'partial.tif',np.ones((3,3),dtype='uint8'),mask=valid,external=True)
    with pytest.raises(BridgeError,match='COMPOSITE_BINARY_MASK'):mosaic(b,[source],{'bounds':[0,0,3,3]},'partial')
    data=np.ones((2,3,3),dtype='uint8');data[0,1,1]=0
    source=write(tmp_path/'bands.tif',data,nodata=0)
    with pytest.raises(BridgeError,match='COMPOSITE_BAND_MASKS'):mosaic(b,[source],{'bounds':[0,0,3,3]},'bands')


def test_mosaic_retains_calibration_and_rejects_incompatible_sources(tmp_path):
    b=Bridge(tmp_path);a=tmp_path/'a.tif';c=tmp_path/'c.tif'
    for file in (a,c):
        write(file,np.ones((3,3),dtype='uint16'))
        with rasterio.open(file,'r+') as ds:ds.scales=(2.,);ds.offsets=(10.,);ds.units=('ppm',);ds.descriptions=('W',)
    files=[{'file':str(f),'expected_sha256':file_sha(f)} for f in (a,c)]
    _,output=mosaic(b,files,{'bounds':[0,0,3,3],'method':'mean'})
    with rasterio.open(output) as ds:assert ds.scales==(2.,) and ds.offsets==(10.,) and ds.units==('ppm',) and ds.descriptions==('W',)
    with rasterio.open(c,'r+') as ds:ds.offsets=(20.,)
    files[1]['expected_sha256']=file_sha(c)
    with pytest.raises(BridgeError,match='MOSAIC_BAND_METADATA'):mosaic(b,files,{'bounds':[0,0,3,3]},'mismatch')


def test_mosaic_uint64_first_keeps_exact_samples_without_float_sentinel_rounding(tmp_path):
    b=Bridge(tmp_path);data=np.full((3,3),2**63+42,dtype='uint64');source=write(tmp_path/'large.tif',data)
    _,output=mosaic(b,[source],{'bounds':[0,0,3,3]})
    with rasterio.open(output) as ds:np.testing.assert_array_equal(ds.read(1),data)


def test_mosaic_source_guard_continues_through_atomic_publication(tmp_path):
    from mapgis_agent.raster_composite import prepare_raster_mosaic
    b=Bridge(tmp_path);source=write(tmp_path/'a.tif',np.ones((3,3),dtype='uint8'))
    receipt=prepare_raster_mosaic(b,[source],{'method':'first','bounds':[0,0,3,3],'resolution':[1,1],'crs':FRAME},{'name':'mosaic.tif'},'pending')
    Path(source['file']).write_bytes(Path(source['file']).read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('pending')
    assert not Path(receipt['output']).exists()


@pytest.mark.parametrize('all_touched',[False,True])
def test_polygon_mask_holes_and_pixel_rule_match_independent_geometry(tmp_path,all_touched):
    b=Bridge(tmp_path);source=tmp_path/'source.tif';data=np.arange(49,dtype='uint16').reshape(7,7);write(source,data)
    geometry=Polygon([(1.1,1.1),(5.8,1.1),(1.1,5.8)],holes=[[(2.2,2.2),(2.8,2.2),(2.8,2.8),(2.2,2.8)]])
    _,output=mask(b,source,geometry,all_touched=all_touched)
    with rasterio.open(output) as ds:
        expected=np.zeros((ds.height,ds.width),dtype='uint8')
        for row in range(ds.height):
            for col in range(ds.width):
                x,y=ds.transform*(col+.5,row+.5)
                corners=[ds.transform*(col+x,row+y) for x,y in [(0,0),(1,0),(1,1),(0,1)]]
                expected[row,col]=255 if (geometry.intersects(Polygon(corners)) if all_touched else geometry.covers(Point(x,y))) else 0
                if expected[row,col]:assert ds.read(1)[row,col]==data[int(7-y),int(x)]
        np.testing.assert_array_equal(ds.read_masks(1),expected)


def test_aoi_crop_preserves_original_zero_and_source_missing_pixel_and_exact_grid(tmp_path):
    b=Bridge(tmp_path);source=tmp_path/'source.tif';data=np.arange(49,dtype='uint8').reshape(7,7);valid=np.full((7,7),255,dtype='uint8');valid[2,2]=0
    write(source,data,mask=valid);_,output=mask(b,source,box(0,0,5,7))
    with rasterio.open(output) as ds:
        assert (ds.width,ds.height)==(5,7) and ds.transform==from_origin(0,7,1,1)
        assert ds.read(1)[0,0]==0 and ds.read_masks(1)[0,0]==255 and ds.read_masks(1)[2,2]==0


def test_aoi_rotated_affine_uses_real_pixel_footprints_and_does_not_resample(tmp_path):
    b=Bridge(tmp_path);source=tmp_path/'rotated.tif';transform=Affine(1,.2,0,.1,-1,7);data=np.arange(49,dtype='uint8').reshape(7,7)
    write(source,data,transform=transform);geometry=box(2.,2.,5.5,5.5);_,output=mask(b,source,geometry,crop=False)
    with rasterio.open(output) as ds:
        assert ds.transform==transform and ds.width==7 and ds.height==7
        for row in range(7):
            for col in range(7):
                yes=geometry.covers(Point(transform*(col+.5,row+.5)))
                assert bool(ds.read_masks(1)[row,col])==yes
                if yes:assert ds.read(1)[row,col]==data[row,col]


def test_aoi_empty_full_grid_and_invalid_geometry_are_explicit(tmp_path):
    b=Bridge(tmp_path);source=tmp_path/'source.tif';write(source,np.ones((3,3),dtype='uint8'))
    receipt,output=mask(b,source,box(20,20,30,30),crop=False)
    with rasterio.open(output) as ds:assert not ds.read_masks().any()
    assert receipt['empty_result']
    with pytest.raises(BridgeError,match='MASK_GEOMETRY'):
        mask(Bridge(tmp_path/'invalid'),source,Polygon([(0,0),(2,2),(0,2),(2,0)]))


def test_mean_mosaic_and_polygon_mask_roundtrip_as_real_float64_native_msi(tmp_path):
    from test_native_raster import native_settings
    from mapgis_agent.native_raster import prepare_native_raster
    b=Bridge(tmp_path);native_settings(tmp_path)
    a=np.arange(9,dtype='uint8').reshape(3,3);other=np.arange(30,39,dtype='uint8').reshape(3,3)
    valid=np.full((3,3),255,dtype='uint8');valid[1,1]=0
    files=[write(tmp_path/'a.tif',a,mask=valid),write(tmp_path/'b.tif',other,transform=from_origin(2,3,1,1))]
    _,combined=mosaic(b,files,{'method':'mean','nodata':-9999})
    _,clipped=mask(b,combined,box(.1,.1,4.9,2.9),nodata=-9999,all_touched=False)
    for index,file in enumerate([combined,clipped]):
        identity='native-'+str(index);prepared=prepare_native_raster(b,'to_msi',str(file),file_sha(file),{'name':'image.msi'},identity)
        native=Path(b.apply(identity)['output']);identity='reopen-'+str(index)
        reopened=prepare_native_raster(b,'to_tiff',str(native),file_sha(native),{'name':'reopened.tif'},identity);output=Path(b.apply(identity)['output'])
        assert prepared['native_execution']['pid']!=reopened['native_execution']['pid']
        with rasterio.open(file) as a,rasterio.open(output) as c:
            assert c.dtypes==a.dtypes==('float64',) and a.nodata==c.nodata==-9999
            np.testing.assert_array_equal(a.read(),c.read());np.testing.assert_array_equal(a.read_masks(),c.read_masks())
