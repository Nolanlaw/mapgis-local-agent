"""Image operations preserve grid/missing semantics and have known pixel answers."""
import json
from pathlib import Path
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine
from mapgis_agent.core import Bridge,BridgeError,sha

TRANSFORM=Affine(2,.3,100,.2,-2,200)
def source(root,values,nodata=None):
    values=np.asarray(values)
    if values.ndim==2:values=values[None,:,:]
    path=root/'source.tif'
    with rasterio.open(path,'w',driver='GTiff',width=values.shape[2],height=values.shape[1],count=len(values),dtype=values.dtype,
        nodata=nodata,crs='EPSG:3857',transform=TRANSFORM) as ds:ds.write(values)
    return path

def process(root,path,parameters,identity='result',members=None):
    from mapgis_agent.raster_processing import prepare_raster_processing
    b=Bridge(root);receipt=prepare_raster_processing(b,str(path),sha(path.read_bytes()),parameters,{'name':identity+'.tif'},identity,members)
    result=b.apply(identity)
    with rasterio.open(result['output']) as ds:
        pixels=ds.read(masked=True);assert ds.transform==TRANSFORM and ds.crs.to_epsg()==3857
    return b,receipt,result,pixels

@pytest.mark.parametrize('operation',['mean','median','gaussian','sobel'])
def test_neighborhood_operations_have_explicit_full_stencil_and_keep_valid_zero(tmp_path,operation):
    y,x=np.mgrid[:7,:7];values=(3*x+4*y).astype('float32');path=source(tmp_path,values)
    before=path.read_bytes()
    b,receipt,result,pixels=process(tmp_path,path,{'operation':operation,'bands':[1],'radius':1} if operation!='sobel' else {'operation':operation,'bands':[1]})
    assert pixels.count()==25
    assert float(pixels[0,3,3])==pytest.approx(5 if operation=='sobel' else 21,abs=1e-5)
    assert receipt['metrics']['valid_pixels']==25 and path.read_bytes()==before

def test_normalized_difference_masks_zero_denominator_but_retains_true_zero(tmp_path):
    path=source(tmp_path,np.array([[[0,2],[3,4]],[[0,2],[1,-4]]],dtype='float32'))
    _,receipt,result,pixels=process(tmp_path,path,{'operation':'normalized_difference','bands':[1,2]})
    assert pixels.count()==2 and pixels[0,0,1]==0 and pixels[0,1,0]==.5
    assert receipt['division_by_zero_pixels']==2

def test_stretch_and_binary_morphology_reserve_nodata_without_eating_valid_black(tmp_path):
    path=source(tmp_path,np.arange(49,dtype='uint8').reshape(7,7))
    _,receipt,result,pixels=process(tmp_path,path,{'operation':'stretch','bands':[1],'percentiles':[0,100]})
    assert pixels.dtype=='uint8' and pixels[0,0,0]==0 and pixels[0,-1,-1]==254 and pixels.count()==49
    assert receipt['output_value_range']==[0,254]
    _,_,threshold,mask=process(tmp_path,path,{'operation':'threshold','bands':[1],'threshold':24},'threshold')
    assert int(mask.sum())==25 and mask.count()==49
    _,_,_,opened=process(tmp_path,Path(threshold['output']),{'operation':'morphology','bands':[1],'threshold':.5,'morphology':'open','radius':1},'opened')
    assert opened.count()==9

def test_local_seeded_kmeans_is_repeatable_and_reports_actual_cluster_centres(tmp_path):
    values=np.zeros((3,4,6),dtype='float32');values[:,:,:3]=np.array([10,20,30])[:,None,None];values[:,:,3:]=np.array([200,210,220])[:,None,None]
    path=source(tmp_path,values)
    p={'operation':'kmeans','bands':[1,2,3],'classes':2,'seed':7,'standardize':True,'iterations':20}
    _,first,_,a=process(tmp_path,path,p,'first');_,second,_,b=process(tmp_path,path,p,'second')
    np.testing.assert_array_equal(a,b);np.testing.assert_array_equal(a[0,:,:3],1);np.testing.assert_array_equal(a[0,:,3:],2)
    np.testing.assert_allclose(first['classification']['centres_source_units'],[[10,20,30],[200,210,220]])
    assert first['classification']['class_counts']==[12,12] and first['classification']==second['classification']

def test_missing_pixel_neighborhood_and_external_mask_hash_are_preserved(tmp_path):
    path=source(tmp_path,np.ones((7,7),dtype='float32'))
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=False):
        with rasterio.open(path,'r+') as ds:
            mask=np.full((7,7),255,dtype='uint8');mask[3,3]=0;ds.write_mask(mask)
    from mapgis_agent.native_raster import raster_source_info
    info=raster_source_info(str(path));assert len(info['members'])==2
    b,receipt,result,pixels=process(tmp_path,path,{'operation':'mean','bands':[1],'radius':1},members=info['members'])
    assert pixels.count()==16 and pixels[0,2:5,2:5].mask.all()
    from mapgis_agent.raster_processing import prepare_raster_processing
    prepare_raster_processing(b,str(path),sha(path.read_bytes()),{'operation':'threshold','bands':[1],'threshold':0},{'name':'stale.tif'},'stale',info['members'])
    msk=Path(str(path)+'.msk');msk.write_bytes(msk.read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('stale')

def test_processed_float_and_byte_outputs_roundtrip_actual_native_msi(tmp_path):
    from mapgis_agent.native_raster import prepare_native_raster,raster_source_info
    from test_native_raster import native_settings
    path=source(tmp_path,np.arange(49,dtype='float32').reshape(7,7));native_settings(tmp_path)
    for operation in ['mean','stretch']:
        b,receipt,result,pixels=process(tmp_path,path,{'operation':operation,'bands':[1]},operation)
        info=raster_source_info(result['output'])
        prepare_native_raster(b,'to_msi',result['output'],result['candidate_sha256'],{'name':operation+'.msi'},operation+'-msi',info['members'])
        msi=b.apply(operation+'-msi')
        prepare_native_raster(b,'to_tiff',msi['output'],msi['candidate_sha256'],{'name':'reopen.tif'},operation+'-reopen')
        reopened=b.apply(operation+'-reopen');actual=raster_source_info(reopened['output'])
        assert actual['raster']['pixel_sha256']==info['raster']['pixel_sha256']
        assert actual['raster']['mask_sha256']==info['raster']['mask_sha256']


def test_gcps_survive_processing_and_invalid_options_fail_before_publication(tmp_path):
    from rasterio.control import GroundControlPoint
    from mapgis_agent.raster_processing import prepare_raster_processing
    path=source(tmp_path,np.arange(49,dtype='float32').reshape(7,7))
    with rasterio.open(path,'r+') as ds:
        ds.gcps=([GroundControlPoint(row=r,col=c,x=100+c*2,y=200-r*2) for r,c in [(0,0),(0,7),(7,0),(7,7)]],rasterio.crs.CRS.from_epsg(3857))
    b=Bridge(tmp_path)
    prepared=prepare_raster_processing(b,str(path),sha(path.read_bytes()),{'operation':'threshold','bands':[1],'threshold':0},{'name':'gcp.tif'},'gcp')
    result=b.apply('gcp')
    with rasterio.open(path) as original,rasterio.open(result['output']) as actual:
        before,crs=original.gcps;after,new_crs=actual.gcps
        assert crs==new_crs and [(g.row,g.col,g.x,g.y) for g in before]==[(g.row,g.col,g.x,g.y) for g in after]
    with pytest.raises(BridgeError,match='IMAGE_UNEXPECTED_PARAMETERS'):
        prepare_raster_processing(b,str(path),sha(path.read_bytes()),{'operation':'threshold','bands':[1],'threshold':0,'radius':3},{'name':'bad.tif'},'bad')


def test_public_mcp_image_processing_schema_and_true_zero(tmp_path):
    import asyncio
    from mapgis_agent.server import build_server
    path=source(tmp_path,np.zeros((3,3),dtype='float32'));b=Bridge(tmp_path)
    async def scenario():
        server=build_server(tmp_path);tool=next(t for t in await server.list_tools() if t.name=='mapgis_prepare_raster_processing')
        assert tool.annotations.idempotentHint and tool.inputSchema['$defs']['ImageParameters']['additionalProperties'] is False
        reply=await server.call_tool(tool.name,{'file':str(path),'expected_sha256':sha(path.read_bytes()),'parameters':{'operation':'mean','bands':[1]},
            'template':{'name':'zero.tif'},'request_id':'zero'})
        receipt=json.loads(''.join(c.text for c in reply if c.type=='text'));assert receipt['metrics']['valid_pixels']==1
    asyncio.run(scenario());result=b.apply('zero')
    with rasterio.open(result['output']) as ds:
        pixels=ds.read(1,masked=True);assert pixels.count()==1 and pixels[1,1]==0


def test_explicit_class_palette_keeps_black_valid_and_masks_unknown_labels_only_by_error(tmp_path):
    path=source(tmp_path,np.array([[0,1],[2,1]],dtype='uint8'),nodata=0)
    _,receipt,result,pixels=process(tmp_path,path,{'operation':'colorize','bands':[1],'palette':[{'value':1,'rgb':[0,0,0]},{'value':2,'rgb':[254,254,254]}]})
    assert pixels.count()==9 and pixels[:,0,1].tolist()==[0,0,0] and pixels[:,1,0].tolist()==[254,254,254]
    assert pixels[:,0,0].mask.all()
    with pytest.raises(BridgeError,match='IMAGE_UNMAPPED_LABEL'):
        process(tmp_path,path,{'operation':'colorize','bands':[1],'palette':[{'value':1,'rgb':[0,0,0]}]},'unknown')


def test_pca_has_known_axes_and_retains_zero_and_missing_pixels(tmp_path):
    x=np.array([[-2.,-1.,0.],[0.,1.,2.]],dtype='float32')
    path=source(tmp_path,np.stack((x,2*x+7)))
    with rasterio.open(path,'r+') as ds:
        valid=np.full(x.shape,255,dtype='uint8');valid[0,0]=0;ds.write_mask(valid)
    before=path.read_bytes()
    _,receipt,_,scores=process(tmp_path,path,{'operation':'pca','bands':[1,2],'components':2})
    model=receipt['pca'];loadings=np.asarray(model['components'])
    np.testing.assert_allclose(loadings[0],[1/np.sqrt(5),2/np.sqrt(5)],rtol=0,atol=1e-12)
    assert model['effective_rank']==1 and model['training_pixels']==5
    assert scores.count()==10 and scores[:,0,0].mask.all()
    matrix=scores[:,valid>0].data.astype(float)
    reconstructed=(loadings.T@matrix)*np.array(model['scale'])[:,None]+np.array(model['mean'])[:,None]
    expected=np.stack((x,2*x+7))[:,valid>0]
    np.testing.assert_allclose(reconstructed,expected,rtol=0,atol=1e-6)
    assert np.all(np.abs(matrix[1])<1e-6) and path.read_bytes()==before


def test_pca_whitening_uses_sample_variance_and_rejects_null_axes(tmp_path):
    a=np.array([[-2.,-1.],[1.,2.]])
    b=np.array([[1.,-2.],[2.,-1.]])
    path=source(tmp_path,np.stack((a,b)))
    _,receipt,_,scores=process(tmp_path,path,{'operation':'pca','bands':[1,2],'components':2,'whiten':True,'standardize':True})
    np.testing.assert_allclose(np.cov(scores.data.reshape(2,-1),ddof=1),np.eye(2),rtol=0,atol=1e-6)
    assert receipt['pca']['whiten'] and receipt['pca']['standardize']
    constant=source(tmp_path,np.zeros((2,2,2),dtype='float32'))
    _,receipt,_,scores=process(tmp_path,constant,{'operation':'pca','bands':[1,2]},'constant')
    assert np.all(scores==0) and receipt['pca']['effective_rank']==0
    with pytest.raises(BridgeError,match='WHITEN'):
        process(tmp_path,constant,{'operation':'pca','bands':[1,2],'whiten':True},'bad-whiten')


def test_pca_preserves_a_small_component_in_an_ill_conditioned_float64_input(tmp_path):
    a=np.array([[1e8,-1e8],[1e8,-1e8]])
    noise=np.array([[1.,-1.],[-1.,1.]])
    path=source(tmp_path,np.stack((a,a+noise)))
    _,receipt,_,scores=process(tmp_path,path,{'operation':'pca','bands':[1,2],'components':2})
    assert receipt['pca']['effective_rank']==2
    assert receipt['pca']['eigenvalues'][1]==pytest.approx(2/3,rel=1e-7)
    assert np.var(scores[1].data,ddof=1)==pytest.approx(2/3,rel=1e-6)


def test_new_pca_options_keep_historical_non_pca_request_fingerprints(tmp_path):
    frozen=json.loads((Path(__file__).parent/'fixtures/image-parameters-v1.json').read_text(encoding='utf8'))
    path=source(tmp_path,np.ones((3,3),dtype='float32'))
    b,receipt,_,_=process(tmp_path,path,{'operation':'mean','bands':[1]},'historical')
    assert receipt['payload']['parameters']==frozen
    from mapgis_agent.raster_processing import prepare_raster_processing
    repeat=prepare_raster_processing(b,str(path),sha(path.read_bytes()),{'operation':'mean','bands':[1]},
        {'name':'historical.tif'},'historical')
    assert repeat['state']=='APPLIED'


@pytest.mark.parametrize('space',['hsv','hls'])
def test_color_space_primary_grey_and_general_colors_match_independent_stdlib_and_roundtrip(tmp_path,space):
    import colorsys
    colors=np.array([[0,0,0],[1,0,0],[0,1,0],[0,0,1],[1,1,1],
                     [1,1,0],[0,1,1],[1,0,1],[.2,.4,.4],[.5,.5,.5]],dtype='float64')
    values=(colors*254).reshape(2,5,3).transpose(2,0,1)
    path=source(tmp_path,values);before=path.read_bytes()
    _,receipt,result,actual=process(tmp_path,path,{'operation':'rgb_to_'+space,'bands':[1,2,3],'rgb_maximum':254.},'forward')
    expected=np.array([getattr(colorsys,'rgb_to_'+space)(*c) for c in colors]).reshape(2,5,3).transpose(2,0,1)
    np.testing.assert_allclose(actual.data,expected,rtol=0,atol=4e-8)
    assert actual.count()==30 and receipt['color_space']['source_rgb_maximum']==254.
    _,inverse,_,rgb=process(tmp_path,Path(result['output']),{'operation':space+'_to_rgb','bands':[1,2,3],'rgb_maximum':254.},'inverse')
    np.testing.assert_allclose(rgb.data,values,rtol=0,atol=8e-5)
    assert path.read_bytes()==before and inverse['color_space']['hue_unit']=='turn; 1 aliases 0'


def test_color_space_retains_small_saturated_signal_without_epsilon_desaturation(tmp_path):
    path=source(tmp_path,np.array([[[1e-12,0]],[[0,1e-12]],[[0,0]]],dtype='float64'))
    _,_,_,pixels=process(tmp_path,path,{'operation':'rgb_to_hsv','bands':[1,2,3],'rgb_maximum':1.})
    np.testing.assert_array_equal(pixels[1].data,[[1.,1.]])
    np.testing.assert_allclose(pixels[2].data,[[1e-12,1e-12]],rtol=1e-7,atol=0)
    np.testing.assert_allclose(pixels[0].data,[[0,1/3]],rtol=0,atol=1e-8)


@pytest.mark.parametrize('operation,values',[
    ('rgb_to_hsv',[-1.,0.,0.]),('rgb_to_hls',[0.,0.,256.]),('hsv_to_rgb',[0.,1.01,1.])])
def test_color_space_rejects_out_of_declared_gamut_instead_of_clipping_inputs(tmp_path,operation,values):
    path=source(tmp_path,np.array(values,dtype='float64')[:,None,None])
    with pytest.raises(BridgeError,match='IMAGE_COLOR_RANGE'):
        process(tmp_path,path,{'operation':operation,'bands':[1,2,3],'rgb_maximum':255.})


def test_color_space_mask_true_black_explicit_scale_and_unrelated_options(tmp_path):
    path=source(tmp_path,np.array([[[0,12],[1,-9999]],[[0,4],[2,3]],[[0,6],[3,4]]],dtype='float32'),-9999)
    _,receipt,_,pixels=process(tmp_path,path,{'operation':'rgb_to_hls','bands':[1,2,3],'rgb_maximum':254.})
    assert pixels.count()==9 and np.all(pixels[:,0,0]==0) and pixels.mask[:,1,1].all()
    assert receipt['metrics']['valid_pixels']==3
    with pytest.raises(BridgeError,match='IMAGE_COLOR_PARAMETERS'):
        process(tmp_path,path,{'operation':'rgb_to_hls','bands':[1,2,3]},'no-scale')
    with pytest.raises(BridgeError,match='IMAGE_UNEXPECTED_PARAMETERS'):
        process(tmp_path,path,{'operation':'rgb_to_hls','bands':[1,2,3],'rgb_maximum':254.,'threshold':.5},'bad-option')
