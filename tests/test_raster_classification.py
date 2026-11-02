"""Known AOIs, held-out pixels and analytic supervised decision boundaries."""
from pathlib import Path
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from mapgis_agent.core import Bridge,BridgeError,sha

GRID=Affine(2,.3,100,.2,-2,200)


def aoi(label,x0,y0,x1,y1):
    return {'class_id':label,'name':f'class {label}',
        'polygon':[[[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]]}


def fixture(folder):
    y,x=np.mgrid[:4,:8]
    centres=np.where(x<4,2.,10.)
    data=np.stack((centres+np.where(x%2==0,-1.,1.),centres+2+np.where(y%2==0,-1.,1.)))
    data[1,3,7]=-9999
    path=folder/'bands.tif'
    with rasterio.open(path,'w',driver='GTiff',width=8,height=4,count=2,dtype='float64',
            crs='EPSG:3857',transform=GRID,nodata=-9999) as ds:ds.write(data)
    params={'method':'minimum_distance','bands':[1,2],
        'training':[aoi(3,0,0,2,2),aoi(9,6,0,8,2)],
        'validation':[aoi(3,0,2,2,4),aoi(9,6,2,8,4)]}
    return path,params


def prepare(folder,path,params,name):
    from mapgis_agent.raster_classification import prepare_raster_classification
    bridge=Bridge(folder)
    receipt=prepare_raster_classification(bridge,str(path),sha(path.read_bytes()),params,{'name':name+'.tif'},name)
    return bridge,receipt


@pytest.mark.parametrize('method',['minimum_distance','mahalanobis','parallelepiped'])
def test_supervised_classes_keep_independent_validation_and_known_model(tmp_path,method):
    path,p=fixture(tmp_path);before=path.read_bytes();p['method']=method
    bridge,receipt=prepare(tmp_path,path,p,method);result=bridge.apply(method)
    with rasterio.open(result['output']) as ds:
        expected=np.where(np.indices((4,8))[1]<4,3,9).astype('uint8');expected[3,7]=255
        np.testing.assert_array_equal(ds.read(1),expected)
        assert ds.transform==GRID and ds.crs.to_epsg()==3857 and ds.nodata==255
        assert ds.read_masks(1)[0,0]==255 and ds.read_masks(1)[3,7]==0
    model=receipt['model'];validation=receipt['validation']
    assert [c['training_pixels'] for c in model['classes']]==[4,4]
    np.testing.assert_allclose([c['mean'] for c in model['classes']],[[2,4],[10,12]],rtol=0,atol=1e-14)
    if method=='mahalanobis':
        np.testing.assert_allclose(model['classes'][0]['covariance'],np.diag([4/3,4/3]),rtol=0,atol=1e-14)
    assert validation['class_ids']==[3,9] and validation['prediction_ids']==[0,3,9]
    assert validation['confusion_matrix']==[[0,4,0],[0,0,3]]
    assert validation['total_pixels']==7 and validation['overall_accuracy']==1
    assert validation['excluded_missing_pixels']==1 and validation['training_validation_overlap_pixels']==0
    assert path.read_bytes()==before
    _,again=prepare(tmp_path,path,p,method);assert again['state']=='APPLIED' and again['model']==model


def test_rejected_zero_remains_valid_and_is_counted_as_validation_error(tmp_path):
    path,p=fixture(tmp_path);p['max_distance']=.1
    bridge,receipt=prepare(tmp_path,path,p,'reject');result=bridge.apply('reject')
    with rasterio.open(result['output']) as ds:
        data=ds.read(1,masked=True)
        assert data.count()==31 and np.all(data.compressed()==0)
    assert receipt['validation']['confusion_matrix']==[[4,0,0],[3,0,0]]
    assert receipt['validation']['overall_accuracy']==0 and receipt['metrics']['unclassified_pixels']==31


def test_aoi_conflicts_and_training_validation_leakage_are_rejected(tmp_path):
    path,p=fixture(tmp_path)
    with pytest.raises(BridgeError,match='CLASSIFICATION_AOI_CONFLICT'):
        prepare(tmp_path,path,p|{'training':[p['training'][0],aoi(9,1,0,3,2)]},'conflict')
    with pytest.raises(BridgeError,match='CLASSIFICATION_LEAKAGE'):
        prepare(tmp_path,path,p|{'validation':[p['training'][0]]},'leak')
    bad=aoi(3,0,0,2,2);bad['polygon'][0]=[[0,0],[2,2],[0,2],[2,0],[0,0]]
    with pytest.raises(BridgeError,match='CLASSIFICATION_AOI_GEOMETRY'):
        prepare(tmp_path,path,p|{'training':[bad,p['training'][1]]},'invalid')


def test_singular_mahalanobis_requires_explicit_ridge(tmp_path):
    path,p=fixture(tmp_path)
    with rasterio.open(path,'r+') as ds:
        data=ds.read();data[1]=2*data[0];ds.write(data)
    p['method']='mahalanobis'
    with pytest.raises(BridgeError,match='CLASSIFICATION_COVARIANCE_SINGULAR'):
        prepare(tmp_path,path,p,'singular')
    bridge,receipt=prepare(tmp_path,path,p|{'covariance_ridge':.1},'ridge')
    assert receipt['model']['covariance_ridge']==.1 and receipt['validation']['overall_accuracy']==1


def test_map_aois_use_explicit_same_crs_and_rotated_grid(tmp_path):
    path,p=fixture(tmp_path)
    def mapped(item):
        return item|{'polygon':[[list(GRID*tuple(v)) for v in ring] for ring in item['polygon']]}
    p=p|{'aoi_space':'map','aoi_crs':'EPSG:3857','training':list(map(mapped,p['training'])),
        'validation':list(map(mapped,p['validation']))}
    bridge,receipt=prepare(tmp_path,path,p,'map');result=bridge.apply('map')
    assert receipt['validation']['overall_accuracy']==1 and receipt['model']['aoi_space']=='map'
    with pytest.raises(BridgeError,match='CLASSIFICATION_AOI_CRS'):
        prepare(tmp_path,path,p|{'aoi_crs':'EPSG:4326'},'wrong-crs')


def test_apply_rechecks_source_and_model_is_a_reproducible_bundle(tmp_path):
    path,p=fixture(tmp_path);bridge,receipt=prepare(tmp_path,path,p,'stale')
    path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('stale')
    assert receipt['validation']['status']=='VERIFIED_ON_DISJOINT_AOI_PIXELS'


def test_standardization_uses_only_training_observations(tmp_path):
    path,p=fixture(tmp_path);p['standardize']=True
    _,before=prepare(tmp_path,path,p,'before')
    np.testing.assert_allclose(before['model']['feature_center'],[6,8])
    np.testing.assert_allclose(before['model']['feature_scale'],np.sqrt([17,17]))
    with rasterio.open(path,'r+') as ds:
        values=ds.read()
        values[:,2:,:]=1000000;ds.write(values)
    _,after=prepare(tmp_path,path,p,'after')
    assert before['model']==after['model']
    assert after['validation']['overall_accuracy']<before['validation']['overall_accuracy']


@pytest.mark.parametrize('method',['minimum_distance','parallelepiped'])
def test_explicit_ambiguity_policy_and_valid_zero(tmp_path,method):
    path=tmp_path/'overlap.tif'
    values=np.array([[0,4,2,6,3],[1,3,5,7,3]],dtype='float64')
    with rasterio.open(path,'w',driver='GTiff',width=5,height=2,count=1,dtype='float64',
            crs='EPSG:3857',transform=GRID) as ds:ds.write(values,1)
    p={'method':method,'bands':[1],'training':[aoi(3,0,0,2,1),aoi(9,2,0,4,1)],
        'validation':[aoi(3,4,1,5,2)]}
    for policy,label in [('unclassified',0),('lowest_id',3)]:
        b,r=prepare(tmp_path,path,p|{'ambiguity':policy},policy);out=b.apply(policy)
        with rasterio.open(out['output']) as ds:
            assert ds.read(1)[1,4]==label and ds.read_masks(1)[1,4]==255
        assert r['validation']['overall_accuracy']==(1 if label==3 else 0)
