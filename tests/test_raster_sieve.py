"""Analytic connected regions, masks and exact integer thematic labels."""
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from mapgis_agent.core import Bridge,BridgeError,sha

GRID=Affine(2,.3,100,.2,-2,200)


def run(folder,values,**options):
    from mapgis_agent.raster_processing import prepare_raster_processing
    path=folder/'labels.tif'
    with rasterio.open(path,'w',driver='GTiff',width=values.shape[1],height=values.shape[0],count=1,
            dtype=values.dtype,crs='EPSG:3857',transform=GRID,nodata=options.pop('nodata',-9999)) as ds:
        ds.write(values,1)
    before=path.read_bytes();bridge=Bridge(folder)
    parameters={'operation':'sieve','bands':[1],'minimum_region_pixels':2,**options}
    receipt=prepare_raster_processing(bridge,str(path),sha(before),parameters,{'name':'clean.tif'},'clean')
    applied=bridge.apply('clean')
    assert path.read_bytes()==before
    return receipt,rasterio.open(applied['output'])


def test_isolated_class_merges_into_zero_and_keeps_rotated_grid_and_nodata(tmp_path):
    values=np.zeros((5,5),dtype='int32');values[2,2]=4;values[0,0]=-9999
    receipt,result=run(tmp_path,values)
    with result:
        assert result.dtypes==('int32',) and result.transform==GRID and result.crs.to_epsg()==3857
        expected=np.zeros((5,5),dtype='int32');expected[0,0]=int(result.nodata)
        np.testing.assert_array_equal(result.read(1),expected)
        assert result.read_masks(1)[2,2]==255 and result.read_masks(1)[0,0]==0
    assert receipt['sieve']['changed_valid_pixels']==1


@pytest.mark.parametrize('connectivity',[4,8])
def test_diagonal_region_connectivity_and_exact_threshold(tmp_path,connectivity):
    values=np.zeros((5,5),dtype='int32');values[1,1]=values[2,2]=values[3,3]=7
    _,result=run(tmp_path,values,minimum_region_pixels=3,connectivity=connectivity)
    with result:
        expected=np.zeros_like(values) if connectivity==4 else values
        np.testing.assert_array_equal(result.read(1),expected)


def test_nodata_barrier_is_not_a_merge_target_or_a_connected_region(tmp_path):
    values=np.full((5,5),-9999,dtype='int32');values[2,2]=0
    receipt,result=run(tmp_path,values)
    with result:
        data=result.read(1,masked=True)
        assert data.count()==1 and data[2,2]==0
    assert receipt['sieve']['changed_valid_pixels']==0


def test_large_signed_labels_and_nodata_marker_collision_are_lossless(tmp_path):
    values=np.full((5,8),16777217,dtype='int32');values[:,:4]=np.iinfo('int32').min
    values[2,2]=2147483647;values[4,7]=-9999
    _,result=run(tmp_path,values)
    with result:
        assert result.nodata!=np.iinfo('int32').min
        actual=result.read(1);expected=values.copy();expected[2,2]=np.iinfo('int32').min;expected[4,7]=int(result.nodata)
        np.testing.assert_array_equal(actual,expected)
        assert result.read_masks(1)[0,0]==255


@pytest.mark.parametrize('bad',[.5,2147483648.,-2147483649.])
def test_nonintegral_or_out_of_int32_labels_are_rejected_without_rounding(tmp_path,bad):
    values=np.zeros((3,3),dtype='float64');values[1,1]=bad
    with pytest.raises(BridgeError,match='IMAGE_SIEVE_LABELS'):
        run(tmp_path,values)
