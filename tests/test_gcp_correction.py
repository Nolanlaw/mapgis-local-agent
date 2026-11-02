"""GDAL polynomial/TPS corrections retain withheld checks and source pixels."""
import json,math
from pathlib import Path
import numpy as np
import pytest
import rasterio
from rasterio.control import GroundControlPoint
from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.native_raster import prepare_raster_warp

def world(c,r,model):
    if model=='tps':return 1000+c,2000+32-r
    x=1000+c+.001*c*c;y=2000+32-r+.0015*r*r
    if model=='polynomial3':x+=.00001*c*c*c;y+=.000008*r*r*r
    return x,y

def controls(model):
    count=4 if model=='polynomial3' else 3
    grid=np.linspace(0,32,count)
    return [GroundControlPoint(col=c,row=r,x=world(c,r,model)[0],y=world(c,r,model)[1]) for r in grid for c in grid]

@pytest.mark.parametrize('model',['polynomial2','polynomial3','tps'])
def test_gdal_model_reports_fit_inverse_and_actually_withheld_checks(model):
    from mapgis_agent.gcp_correction import validate_gcp_model
    gcps=controls(model);c,r=12.3,8.7;target=world(c,r,model)
    report=validate_gcp_model(gcps,model,[{'pixel':[c,r],'world':list(target)}],1e-6,.5,1e-6)
    assert report['control_points']==len(gcps) and report['fit']['max_world_error']<1e-6
    assert report['independent_checkpoints']['count']==1 and report['independent_checkpoints']['max_world_error']<1e-6
    assert report['inverse']['max_pixel_error']<.5

def test_gcp_model_rejects_insufficient_degenerate_and_reused_checkpoints():
    from mapgis_agent.gcp_correction import validate_gcp_model
    with pytest.raises(BridgeError,match='GCP_MINIMUM'):
        validate_gcp_model(controls('polynomial2')[:5],'polynomial2',[],1e-6,.5,None)
    line=[GroundControlPoint(col=i,row=0,x=i,y=0) for i in range(6)]
    with pytest.raises(BridgeError,match='GCP_DEGENERATE'):
        validate_gcp_model(line,'polynomial2',[],1e-6,.5,None)
    p=controls('tps')[0]
    with pytest.raises(BridgeError,match='GCP_CHECKPOINT_REUSED'):
        validate_gcp_model(controls('tps'),'tps',[{'pixel':[float(p.col),float(p.row)],'world':[float(p.x),float(p.y)]}],1e-6,.5,None)
    with pytest.raises(BridgeError,match='GCP_CHECKPOINTS_REQUIRED'):
        validate_gcp_model(controls('polynomial2'),'polynomial2',[],1e-6,.5,.01)

@pytest.mark.parametrize('model',['polynomial2','polynomial3','tps'])
def test_nonlinear_warp_actual_known_target_pixel_and_native_msi_roundtrip(tmp_path,model):
    b=Bridge(tmp_path);source=tmp_path/'curved.tif';r,c=np.mgrid[:32,:32];values=(100*r+c).astype('float32')
    with rasterio.open(source,'w',driver='GTiff',width=32,height=32,count=1,dtype='float32',
        gcps=controls(model),crs='EPSG:3857') as ds:ds.write(values,1)
    fingerprint=sha(source.read_bytes());x,y=world(16.5,16.5,model)
    cx,cy=world(12.3,8.7,model)
    prepared=prepare_raster_warp(b,str(source),fingerprint,{'name':'rectified.tif','target_crs':{'definition':'EPSG:3857','axis_order':'xy'},
        'bounds':[x-1.75,y-2.25,x+2.25,y+1.75],'resolution':[.5,.5],'resampling':'nearest','gcp_model':model,
        'checkpoints':[{'pixel':[12.3,8.7],'world':[cx,cy]}],'max_gcp_error':1e-6,'max_inverse_error_pixels':.5,'max_checkpoint_error':1e-6},'rectify')
    result=b.apply('rectify')
    with rasterio.open(result['output']) as ds:
        assert ds.read(1)[3,3]==1616 and ds.gcps[0]==[] and ds.crs.to_epsg()==3857
    assert prepared['gcp_report']['independent_checkpoints']['count']==1
    assert prepared['georeference']=='GCP_'+model.upper()+'_RECTIFIED' and sha(source.read_bytes())==fingerprint
    from test_native_raster import native_settings
    from mapgis_agent.native_raster import prepare_native_raster,raster_source_info
    native_settings(tmp_path);info=raster_source_info(result['output'])
    prepare_native_raster(b,'to_msi',result['output'],result['candidate_sha256'],{'name':'rectified.msi'},'msi',info['members']);msi=b.apply('msi')
    prepare_native_raster(b,'to_tiff',msi['output'],msi['candidate_sha256'],{'name':'readback.tif'},'reopen');reopened=b.apply('reopen')
    actual=raster_source_info(reopened['output'])
    assert actual['raster']['pixel_sha256']==info['raster']['pixel_sha256'] and actual['raster']['mask_sha256']==info['raster']['mask_sha256']
