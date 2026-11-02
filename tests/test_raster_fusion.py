"""Independent colour matrix and direct convolution oracles for fusion."""
import hashlib
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.transform import Affine

from mapgis_agent.core import Bridge, BridgeError

GRID = Affine(2, .3, 100, .2, -2, 200)


def write(path, data, valid=None, grid=GRID, gcps=False, calibration=None):
    if data.ndim == 2: data = data[None]
    with rasterio.open(path, 'w', driver='GTiff', width=data.shape[2], height=data.shape[1],
                       count=len(data), dtype='float64', crs='EPSG:4549', transform=grid) as ds:
        ds.write(data)
        if valid is not None: ds.write_mask(valid.astype('uint8')*255)
        if gcps:
            ds.gcps = ([GroundControlPoint(row=r, col=c, x=x, y=y) for r,c,x,y in
                        [(0,0,100,200), (0,data.shape[2],140,204), (data.shape[1],0,103,180)]], ds.crs)
        if calibration: ds.scales, ds.offsets = calibration
    from mapgis_agent.native_raster import raster_source_info
    info = raster_source_info(str(path))
    return {'file':str(path), 'expected_sha256':info['source_sha256'], 'expected_members':info['members']}


def run(folder, ms, pan, options, valid=None, pan_valid=None, reference=None, gcps=False):
    from mapgis_agent.raster_fusion import prepare_raster_fusion
    multispectral = write(folder/'ms.tif', ms, valid, gcps=gcps)
    panchromatic = write(folder/'pan.tif', pan, pan_valid, gcps=gcps)
    ref = write(folder/'reference.tif', reference, valid, gcps=gcps) if reference is not None else None
    bridge = Bridge(folder)
    prepared = prepare_raster_fusion(bridge, multispectral, panchromatic,
        {'bands':list(range(1,len(ms)+1)), 'matching':'none', **options}, {'name':'fused.tif'}, 'fusion', ref)
    applied = bridge.apply('fusion')
    with rasterio.open(applied['output']) as ds:
        assert ds.dtypes == ('float64',)*len(ms)
        return prepared, ds.read(), ds.read_masks(1)>0, ds.transform


def smooth_oracle(data, level, reflect=True):
    # Direct25-tap2D convolution, independent of SciPy's separable passes.
    step = 2**level; radius = 2*step
    padded = np.pad(data, ((radius,radius),(radius,radius)), mode='symmetric' if reflect else 'constant')
    result = np.zeros(data.shape); weights = [1,4,6,4,1]
    for i, wy in enumerate(weights):
        for j, wx in enumerate(weights):
            result += wy*wx/256*padded[i*step:i*step+data.shape[0],j*step:j*step+data.shape[1]]
    return result


def test_linear_ihs_is_independently_inverted_and_not_clipped(tmp_path):
    ms = np.array([[[0.,4.,80.],[2.,90.,10.]], [[0.,8.,50.],[3.,20.,30.]], [[0.,12.,20.],[4.,10.,90.]]])
    pan = np.array([[0.,50.,5.],[90.,1.,150.]])
    prepared, actual, valid, transform = run(tmp_path, ms, pan, {'method':'ihs'})
    matrix = np.array([[1/3,1/3,1/3], [2/np.sqrt(6),-1/np.sqrt(6),-1/np.sqrt(6)], [0.,-1/np.sqrt(2),1/np.sqrt(2)]])
    transformed = np.einsum('ij,jyx->iyx',matrix,ms); transformed[0] = pan
    expected = np.einsum('ij,jyx->iyx',np.linalg.inv(matrix),transformed)
    np.testing.assert_allclose(actual, expected, atol=3e-14)
    assert valid.all() and actual[:,0,0].tolist() == [0.,0.,0.] and actual.min() < 0 and actual.max() > 100
    assert transform == GRID and prepared['fusion']['clipping'] == 'none'


def test_weighted_fusion_uses_each_explicit_pan_fraction(tmp_path):
    ms = np.arange(36.).reshape(3,3,4); pan = np.ones((3,4))*100
    _, actual, valid, _ = run(tmp_path,ms,pan,{'method':'weighted','pan_weights':[0.,1.,.25]})
    expected = np.stack([ms[0],pan,.75*ms[2]+.25*pan])
    np.testing.assert_array_equal(actual, expected); assert valid.all()


@pytest.mark.parametrize('method',['wavelet_ihs','wavelet_awlp'])
def test_wavelet_injection_matches_direct_multilevel_b3_spline(tmp_path,method):
    y,x = np.indices((15,17)); pan = 40.+.4*x+.2*y+4.*((-1.)**(x+y))
    ms = np.stack([10.+.1*x+0*y,20.+.3*x+.2*y,30.+.2*x+.1*y])
    _, actual, valid, _ = run(tmp_path,ms,pan,{'method':method,'levels':2,'edge':'reflect'})
    low = smooth_oracle(smooth_oracle(pan,0),1); detail = pan-low
    expected = ms+detail if method=='wavelet_ihs' else ms+ms/ms.mean(axis=0)*detail
    np.testing.assert_allclose(actual,expected,atol=3e-14); assert valid.all()


def test_wavelet_feature_rule_preserves_signed_largest_details_and_ms_approximation(tmp_path):
    y,x = np.indices((15,17)); pan = 25.+3.*((-1.)**x)
    ms = np.stack([25.+5.*((-1.)**y), 25.+.2*x+.2*y, np.full(x.shape,25.)])
    _, actual, valid, _ = run(tmp_path,ms,pan,{'method':'wavelet_maxabs','levels':2,'edge':'reflect'})
    expected = np.zeros_like(ms); pan_low=pan.copy(); ms_low=ms.copy(); chosen_counts=[]
    tolerance=64*np.finfo('float64').eps*np.maximum(np.max(abs(ms),axis=(1,2)),np.max(abs(pan)))
    for level in range(2):
        pnext = smooth_oracle(pan_low,level); pd=pan_low-pnext
        for band in range(3):
            mnext = smooth_oracle(ms_low[band],level); md=ms_low[band]-mnext
            choose = abs(pd)-abs(md)>tolerance[band]; expected[band] += np.where(choose,pd,md)
            chosen_counts.append(int(choose.sum())); ms_low[band] = mnext
        pan_low=pnext
    expected += ms_low
    np.testing.assert_allclose(actual,expected,atol=5e-14); assert valid.all() and max(chosen_counts)>0
    assert actual[0].min()<25 and actual[0].max()>25


def test_opposite_signed_equal_magnitude_details_do_not_flip_from_convolution_roundoff(tmp_path):
    y,x=np.indices((15,17)); ms=(30.+1.4*x+.2*y)[None]; pan=100.-ms[0]
    prepared,actual,valid,_=run(tmp_path,ms,pan,{'method':'wavelet_maxabs','levels':2})
    np.testing.assert_allclose(actual,ms,atol=5e-13,rtol=0)
    assert valid.all() and prepared['fusion']['maxabs_comparison_roundoff_factor']==64


def test_missing_neighbour_is_never_filled_or_renormalized(tmp_path):
    ms = np.ones((3,48,50))*30; pan = np.ones((48,50))*30
    valid = np.ones(pan.shape,dtype=bool); valid[24,25]=False; pan[24,25]=1e99
    prepared, actual, mask, _ = run(tmp_path,ms,pan,{'method':'wavelet_ihs','levels':2,'edge':'complete'},pan_valid=valid)
    expected = np.zeros(pan.shape,dtype=bool); expected[6:-6,6:-6]=True; expected[18:31,19:32]=False
    np.testing.assert_array_equal(mask,expected)
    np.testing.assert_array_equal(actual[:,mask],np.ones((3,int(mask.sum())))*30)
    assert prepared['fusion']['support_radius_pixels'] == 6


def test_zero_intensity_awlp_is_explicitly_masked_or_rejected(tmp_path):
    ms=np.zeros((3,7,9)); ms[:,0,:]=10; pan=np.ones((7,9))*2
    prepared, actual, valid, _ = run(tmp_path,ms,pan,{'method':'wavelet_awlp','levels':1,'zero_intensity':'mask'})
    assert valid.sum()==9 and prepared['fusion']['undefined_intensity_pixels']==54
    assert np.isfinite(actual[:,valid]).all()


def test_moment_matching_rejects_constant_pan_when_intensity_varies(tmp_path):
    ms=np.arange(63.).reshape(1,7,9)+1; pan=np.ones((7,9))
    with pytest.raises(BridgeError,match='FUSION_MATCH_VARIANCE'):
        run(tmp_path,ms,pan,{'method':'weighted','pan_weights':[.5],'matching':'mean_std'})


def test_wavelet_matching_uses_lowpass_moments_on_the_final_support(tmp_path):
    y,x=np.indices((15,17)); ms=np.stack([10.+x+.3*y,20.+.2*x+y,30.+.7*x+.8*y])
    pan=70.+2*x+3*y+12*((-1.)**(x+y))
    prepared,actual,valid,_=run(tmp_path,ms,pan,{'method':'wavelet_ihs','levels':2,'matching':'mean_std'})
    low=smooth_oracle(smooth_oracle(pan,0),1); gain=ms.mean(axis=0).std()/low.std()
    np.testing.assert_allclose(actual,ms+gain*(pan-low),atol=4e-14)
    assert prepared['fusion']['pan_gain']==pytest.approx(gain) and valid.all()


def test_explicit_radiometric_calibration_is_applied_exactly_once(tmp_path):
    from mapgis_agent.raster_fusion import prepare_raster_fusion
    ms=np.arange(54.).reshape(3,3,6); pan=np.arange(18.).reshape(3,6)
    m=write(tmp_path/'ms.tif',ms,calibration=((2.,3.,4.),(1.,2.,3.)))
    p=write(tmp_path/'pan.tif',pan,calibration=((5.,),(7.,)))
    bridge=Bridge(tmp_path)
    prepared=prepare_raster_fusion(bridge,m,p,{'method':'weighted','bands':[1,2,3],
        'matching':'none','radiometry':'scale_offset','pan_weights':[.5,.5,.5]}, {'name':'calibrated.tif'},'calibrated')
    with rasterio.open(bridge.apply('calibrated')['output']) as ds:
        calibrated=ms*np.array([2.,3.,4.])[:,None,None]+np.array([1.,2.,3.])[:,None,None]
        np.testing.assert_array_equal(ds.read(),.5*calibrated+.5*(5*pan+7))
        assert ds.scales==(1.,1.,1.) and ds.offsets==(0.,0.,0.)
    assert prepared['fusion']['radiometry']=='scale_offset'


def test_reference_metrics_use_actual_reference_and_declared_scale_ratio(tmp_path):
    y,x=np.indices((7,9)); ms=np.stack([10.+x+y,20.+2*x+y,30.+x+2*y]); pan=ms.mean(axis=0)
    prepared,actual,valid,_=run(tmp_path,ms,pan,{'method':'ihs','reference_scale_ratio':2.},reference=ms)
    quality=prepared['quality']['reference']
    assert quality['rmse_by_band']==[0.,0.,0.] and quality['sam_degrees']==0. and quality['ergas']==0.
    assert quality['valid_pixels']==63 and quality['scale_ratio']==2.


def test_spectral_angle_and_undefined_ergas_have_independent_analytic_values():
    import math
    from mapgis_agent.raster_fusion import _quality
    actual=np.array([[[3.,0.]],[[4.,0.]],[[0.,0.]]])
    truth=np.array([[[4.,0.]],[[3.,0.]],[[0.,0.]]]); valid=np.ones((1,2),dtype=bool)
    quality=_quality(actual,truth,valid,truth,valid,2.)['reference']
    assert quality['sam_degrees']==pytest.approx(math.degrees(math.acos(24/25)),abs=2e-14)
    np.testing.assert_allclose(quality['rmse_by_band'],[math.sqrt(.5),math.sqrt(.5),0.],atol=1e-15)
    assert quality['sam_zero_vector_pixels']==1 and quality['sam_valid_pixels']==1
    assert quality['ergas'] is None and quality['ergas_zero_mean_bands']==[3]


def test_equal_gcp_frames_and_unknown_map_affine_are_preserved(tmp_path):
    ms=np.ones((3,7,9))*10; pan=np.ones((7,9))*20
    prepared,actual,valid,_=run(tmp_path,ms,pan,{'method':'ihs'},gcps=True)
    with rasterio.open(tmp_path/'ms.tif') as source, rasterio.open(prepared['output']) as output:
        def anchors(ds): return [(g.row,g.col,g.x,g.y,g.z) for g in ds.gcps[0]],ds.gcps[1]
        assert anchors(source)==anchors(output)
    assert valid.all() and np.all(actual==20)


def test_alignment_and_source_hashes_are_guarded_through_apply(tmp_path):
    from mapgis_agent.raster_fusion import prepare_raster_fusion
    ms=np.ones((3,7,9))*10; pan=np.ones((7,9))*20
    m=write(tmp_path/'ms.tif',ms); shifted=write(tmp_path/'shifted.tif',pan,grid=GRID*Affine.translation(.5,0))
    bridge=Bridge(tmp_path); options={'method':'ihs','bands':[1,2,3],'matching':'none'}
    with pytest.raises(BridgeError,match='FUSION_ALIGNMENT'):
        prepare_raster_fusion(bridge,m,shifted,options,{'name':'fused.tif'},'bad-frame')
    p=write(tmp_path/'pan.tif',pan)
    result=prepare_raster_fusion(bridge,m,p,options,{'name':'fused.tif'},'stale')
    Path(p['file']).write_bytes(Path(p['file']).read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):
        bridge.apply('stale')
    assert not Path(result['output']).exists()
