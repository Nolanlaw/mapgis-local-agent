"""Known spectral groups, real adaptive topology changes and a saved predictor."""
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from mapgis_agent.core import Bridge,BridgeError,sha

GRID=Affine(2,.3,100,.2,-2,200)


def process(folder,values,options,**extra):
    from mapgis_agent.raster_processing import prepare_raster_processing
    if values.ndim==2:values=values[None,:,:]
    path=folder/'features.tif'
    if not path.exists():
        with rasterio.open(path,'w',driver='GTiff',width=values.shape[2],height=values.shape[1],count=len(values),
                dtype='float64',nodata=-9999.,transform=GRID,crs='EPSG:3857') as ds:ds.write(values)
    before=path.read_bytes();bridge=Bridge(folder);request=extra.pop('request_id','cluster')
    parameters={'operation':'isodata','bands':list(range(1,len(values)+1)),'isodata':options,**extra}
    receipt=prepare_raster_processing(bridge,str(path),sha(before),parameters,{'name':request+'.tif'},request)
    applied=bridge.apply(request)
    assert path.read_bytes()==before
    return receipt,rasterio.open(applied['output'])


def test_broad_cluster_really_splits_into_three_known_spectral_groups(tmp_path):
    values=np.tile(np.repeat([-10.,0.,10.],40),(6,1))
    y,x=np.indices(values.shape);values+=np.where(y%2==0,-.1,.1)
    options={'initial_classes':2,'max_classes':3,'min_cluster_pixels':10,'split_stddev':2.,'merge_distance':1.}
    receipt,result=process(tmp_path,values,options,iterations=30,seed=11)
    with result:
        expected=np.tile(np.repeat([1,2,3],40),(6,1)).astype('uint8')
        np.testing.assert_array_equal(result.read(1),expected)
        assert result.transform==GRID and result.crs.to_epsg()==3857 and result.nodata==0
    model=receipt['isodata']
    assert model['classes']==3 and any(row['action']=='split' for row in model['history'])
    np.testing.assert_allclose(np.asarray(model['centres_source_units'])[:,0],[-10,0,10],rtol=0,atol=1e-13)
    assert model['prediction_counts']==[240,240,240] and model['termination']=='converged'


def test_close_centres_really_merge_with_population_weighted_updates(tmp_path):
    values=np.tile(np.repeat([-10.2,-10.1,-9.9,-9.8,9.8,9.9,10.1,10.2],20),(4,1))
    options={'initial_classes':4,'max_classes':4,'min_cluster_pixels':5,'split_stddev':100.,
        'merge_distance':1.5,'max_merge_pairs':2}
    receipt,result=process(tmp_path,values,options,iterations=30,seed=7)
    with result:np.testing.assert_array_equal(result.read(1),np.tile(np.repeat([1,2],80),(4,1)))
    model=receipt['isodata'];assert model['classes']==2
    assert any(row['action']=='merge' for row in model['history'])
    np.testing.assert_allclose(model['centres_source_units'],[[-10.],[10.]],rtol=0,atol=1e-13)


def test_tiny_training_cluster_is_deleted_and_finite_zero_stays_valid(tmp_path):
    values=np.array([[-10.]*100+[10.]*100+[100.]*3+[0.]],dtype='float64')
    options={'initial_classes':3,'max_classes':3,'min_cluster_pixels':10,'split_stddev':100.,'merge_distance':0.}
    receipt,result=process(tmp_path,values,options,seed=3)
    with result:
        assert result.read_masks(1).min()==255 and result.read(1)[0,-1]>0
    assert receipt['isodata']['classes']==2
    assert any(row['action']=='prune' for row in receipt['isodata']['history'])


def test_constant_features_and_joint_missing_mask_are_not_empty_clusters(tmp_path):
    values=np.full((2,5,8),7.);values[1,4,7]=-9999
    options={'initial_classes':3,'max_classes':4,'min_cluster_pixels':2,'split_stddev':1.,'merge_distance':0.}
    receipt,result=process(tmp_path,values,options,standardize=True)
    with result:
        expected=np.ones((5,8),dtype='uint8');expected[4,7]=0
        np.testing.assert_array_equal(result.read(1),expected)
        assert result.read_masks(1)[0,0]==255 and result.read_masks(1)[4,7]==0
    assert receipt['isodata']['classes']==1 and receipt['isodata']['constant_bands']==[1,2]


def test_seeded_sampling_and_model_prediction_are_reproducible(tmp_path):
    rng=np.random.default_rng(5);values=np.stack((rng.normal(size=(20,40)),rng.normal(size=(20,40))))
    options={'initial_classes':3,'max_classes':5,'min_cluster_pixels':3,'split_stddev':3.,'merge_distance':0.}
    first,a=process(tmp_path,values,options,seed=22,sample_size=100,standardize=True,iterations=10)
    second,b=process(tmp_path,values,options,seed=22,sample_size=100,standardize=True,iterations=10,request_id='second')
    with a,b:np.testing.assert_array_equal(a.read(),b.read())
    assert first['isodata']==second['isodata']
    model=first['isodata'];centres=np.asarray(model['centres_working_units'])
    features=np.moveaxis(values,0,-1).reshape(-1,2)
    standardized=(features-model['centering'])/model['scales']
    expected=np.argmin(np.sum((standardized[:,None,:]-centres[None,:,:])**2,axis=2),axis=1)+1
    with rasterio.open(first['output']) as ds:np.testing.assert_array_equal(ds.read(1).ravel(),expected)
    assert model['fit_pixels']==100 and sum(model['prediction_counts'])==800


def test_nearest_centre_tie_uses_first_canonical_label():
    from mapgis_agent.raster_isodata import assign_nearest
    labels,distances=assign_nearest(np.array([[0.],[2.]]),np.array([[-1.],[1.]]))
    np.testing.assert_array_equal(labels,[0,1]);np.testing.assert_array_equal(distances,[1.,1.])


def test_single_diffuse_seed_can_adapt_instead_of_becoming_a_fixed_one_class_result(tmp_path):
    values=np.tile(np.repeat([-10.,0.,10.],30),(4,1))
    options={'initial_classes':1,'max_classes':3,'min_cluster_pixels':5,'split_stddev':2.,'merge_distance':1.}
    receipt,result=process(tmp_path,values,options,iterations=30)
    with result:np.testing.assert_array_equal(result.read(1),np.tile(np.repeat([1,2,3],30),(4,1)))
    assert receipt['isodata']['classes']==3 and any(row['action']=='split' for row in receipt['isodata']['history'])


def test_all_clusters_below_explicit_minimum_fail_without_an_empty_success(tmp_path):
    values=np.tile([0.,10.],(3,3))
    options={'initial_classes':2,'max_classes':3,'min_cluster_pixels':20,'split_stddev':1.,'merge_distance':0.}
    with pytest.raises(BridgeError,match='ISODATA_MINIMUM'):
        process(tmp_path,values,options)


def test_iteration_cap_is_reported_and_not_mislabeled_as_convergence(tmp_path):
    values=np.tile(np.repeat([-10.,0.,10.],20),(3,1))
    options={'initial_classes':2,'max_classes':4,'min_cluster_pixels':2,'split_stddev':1.,'merge_distance':1.}
    receipt,result=process(tmp_path,values,options,iterations=1)
    with result:assert result.read_masks(1).min()==255
    assert receipt['isodata']['iterations']==1 and receipt['isodata']['termination']=='max_iterations'
