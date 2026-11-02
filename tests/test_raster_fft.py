"""Complex transforms compared with a direct DFT, not another FFT call."""
import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from mapgis_agent.core import Bridge, BridgeError, sha
from mapgis_agent.raster_processing import prepare_raster_processing


GRID=Affine(2,.3,100,.2,-2,200)


def image(folder,name,data,nodata=None):
    path=folder/name
    data=np.asarray(data,dtype='float64')
    if data.ndim==2:data=data[None,:,:]
    with rasterio.open(path,'w',driver='GTiff',width=data.shape[2],height=data.shape[1],
            count=len(data),dtype='float64',transform=GRID,crs='EPSG:3857',nodata=nodata) as ds:
        ds.write(data)
    return path


def run(folder,path,parameters,name):
    bridge=Bridge(folder)
    receipt=prepare_raster_processing(bridge,str(path),sha(path.read_bytes()),parameters,{'name':name+'.tif'},name)
    applied=bridge.apply(name)
    return receipt,Path(applied['output'])


def dft(data,inverse=False,norm='ortho'):
    height,width=data.shape
    sign=1 if inverse else -1
    row=np.exp(sign*2j*np.pi*np.outer(np.arange(height),np.arange(height))/height)
    col=np.exp(sign*2j*np.pi*np.outer(np.arange(width),np.arange(width))/width)
    scale=np.sqrt(data.size) if norm=='ortho' else data.size if ((norm=='backward')==inverse) else 1
    return row@data@col.T/scale


@pytest.mark.parametrize('norm',['backward','forward','ortho'])
@pytest.mark.parametrize('shifted',[False,True])
def test_complex_fft_matches_direct_dft_and_inverse_restores_reference_grid(tmp_path,norm,shifted):
    y,x=np.mgrid[:3,:5]
    signal=(x-2*y)+1j*(3*x+y/3)
    path=image(tmp_path,'complex.tif',np.stack((signal.real,signal.imag)))
    before=path.read_bytes()
    parameters={'operation':'fft','bands':[1,2],'fft_norm':norm,'fft_shift':shifted}
    receipt,spectrum=run(tmp_path,path,parameters,'spectrum')
    expected=dft(signal,norm=norm)
    if shifted:expected=np.roll(expected,(1,2),axis=(0,1))
    with rasterio.open(spectrum) as ds:
        actual=ds.read()
        np.testing.assert_allclose(actual[0]+1j*actual[1],expected,rtol=0,atol=1e-12)
        assert ds.dtypes==('float64','float64') and ds.nodata is None and ds.crs is None and ds.transform==Affine.identity()
        assert ds.descriptions==('FFT_REAL','FFT_IMAG')
        assert json.loads(ds.tags()['MAPGIS_FFT'])['domain']=='frequency'
    _,restored=run(tmp_path,spectrum,{'operation':'ifft','bands':[1,2],
        'fft_norm':norm,'fft_shift':shifted,'fft_reference':{'file':str(path),
            'expected_sha256':sha(before)}},'inverse')
    with rasterio.open(restored) as ds:
        values=ds.read()
        np.testing.assert_allclose(values[0]+1j*values[1],signal,rtol=0,atol=2e-13)
        assert ds.transform==GRID and ds.crs.to_epsg()==3857
        assert ds.descriptions==('IFFT_REAL','IFFT_IMAG') and (ds.read_masks()==255).all()
    assert path.read_bytes()==before and receipt['fft']['missing_policy']=='reject any missing sample; no implicit zero fill'


def test_real_fft_keeps_dc_zero_and_sub_float32_signal(tmp_path):
    values=np.zeros((4,6));values[1,2]=1e-80
    path=image(tmp_path,'tiny.tif',values)
    _,spectrum=run(tmp_path,path,{'operation':'fft','bands':[1]},'tiny-fft')
    with rasterio.open(spectrum) as ds:
        frequency=ds.read();np.testing.assert_allclose(frequency[0]+1j*frequency[1],dft(values),rtol=1e-13,atol=1e-95)
    _,restored=run(tmp_path,spectrum,{'operation':'ifft','bands':[1,2]},'tiny-inverse')
    with rasterio.open(restored) as ds:
        np.testing.assert_allclose(ds.read(1),values,rtol=0,atol=1e-95)
        assert ds.read(1)[1,2]!=0 and ds.crs is None
    zero=image(tmp_path,'zero.tif',np.zeros((2,2)))
    receipt,zero_fft=run(tmp_path,zero,{'operation':'fft','bands':[1]},'zero-fft')
    with rasterio.open(zero_fft) as ds:assert np.all(ds.read()==0) and np.all(ds.read_masks()==255)


def test_fft_rejects_missing_samples_and_inconsistent_frequency_convention(tmp_path):
    values=np.arange(12,dtype=float).reshape(3,4);values[1,1]=-9999
    missing=image(tmp_path,'missing.tif',values,nodata=-9999)
    with pytest.raises(BridgeError,match='IMAGE_FFT_MISSING'):
        run(tmp_path,missing,{'operation':'fft','bands':[1]},'missing-fft')
    complete=image(tmp_path,'complete.tif',np.arange(12).reshape(3,4))
    _,spectrum=run(tmp_path,complete,{'operation':'fft','bands':[1],'fft_shift':True},'spectrum')
    with pytest.raises(BridgeError,match='IMAGE_FFT_CONVENTION'):
        run(tmp_path,spectrum,{'operation':'ifft','bands':[1,2],'fft_shift':False},'wrong-shift')
    with pytest.raises(BridgeError,match='IMAGE_FFT_CONVENTION'):
        run(tmp_path,spectrum,{'operation':'ifft','bands':[1,2],'fft_shift':True,'fft_norm':'forward'},'wrong-norm')
    with pytest.raises(BridgeError,match='IMAGE_FFT_BANDS'):
        run(tmp_path,complete,{'operation':'ifft','bands':[1]},'wrong-count')
    with pytest.raises(BridgeError,match='IMAGE_UNEXPECTED_PARAMETERS'):
        run(tmp_path,complete,{'operation':'fft','bands':[1],'rgb_maximum':255.},'wrong-options')


def test_ifft_reference_is_guarded_and_gcps_preserved(tmp_path):
    from rasterio.control import GroundControlPoint
    reference=image(tmp_path,'gcp.tif',np.arange(12).reshape(3,4))
    with rasterio.open(reference,'r+') as ds:
        ds.gcps=([GroundControlPoint(row=r,col=c,x=100+2*c,y=200-2*r)
            for r,c in [(0,0),(0,4),(3,0),(3,4)]],rasterio.crs.CRS.from_epsg(3857))
    _,spectrum=run(tmp_path,reference,{'operation':'fft','bands':[1]},'spectrum')
    p={'operation':'ifft','bands':[1,2],'fft_reference':{'file':str(reference),
        'expected_sha256':sha(reference.read_bytes())}}
    receipt,restored=run(tmp_path,spectrum,p,'restored')
    with rasterio.open(reference) as before,rasterio.open(restored) as after:
        coordinates=lambda ds:[(g.row,g.col,g.x,g.y,g.z) for g in ds.gcps[0]]
        assert coordinates(before)==coordinates(after) and before.gcps[1]==after.gcps[1]
    bridge=Bridge(tmp_path)
    prepare_raster_processing(bridge,str(spectrum),sha(spectrum.read_bytes()),p,{'name':'stale.tif'},'stale')
    reference.write_bytes(reference.read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('stale')
