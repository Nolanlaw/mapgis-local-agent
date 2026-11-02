"""Independent XYZV examples, physical file bounds and three-axis sections."""
import struct
import json
from pathlib import Path

import numpy as np
import pytest

from mapgis_agent.core import BridgeError
from mapgis_agent.volume import encode_3bn, read_3bn, encode_3dm, read_3dm, interpolate_volume, slice_volume, parse_dat


def test_scatter_preserves_zero_order_and_reports_float32_loss(tmp_path):
    points=parse_dat(b'4\n0 0 0 0\n0 0 5 10\n10 0 0 20\n10 0 5 30\n')
    blob,info=encode_3bn(points,0,0)
    # This exact installed build starts with n, not a float version word.
    assert struct.unpack_from('<i',blob)[0]==4 and len(blob)==192
    assert struct.unpack_from('<ii',blob,44)==(2,2)
    path=tmp_path/'samples.3bn';path.write_bytes(blob)
    actual,header=read_3bn(path)
    assert json.loads(json.dumps(header))['header_group_counts_match'] is True
    assert np.array_equal(actual,points) and header['range']==[0.,30.]
    with pytest.raises(BridgeError,match='ROUNDING'):
        encode_3bn(np.array([[500000.01,0,0,1.]]),.001,0)
    blob,info=encode_3bn(np.array([[500000.01,0,0,1.]]),.02,0)
    assert info['max_coordinate_error_m']==pytest.approx(.01)
    for malformed in (b'2\n0 0 0 0\n',b'1\n0 0 nan 3\n',b'1\n0 0 0 3\n9 9 9 9\n'):
        with pytest.raises(BridgeError):parse_dat(malformed)


def test_3dm_uses_zyx_values_and_rejects_invalid_offsets_before_native(tmp_path):
    z,y,x=np.meshgrid([0.,5.,10.],[20.,22.,24.],[10.,11.,12.,13.],indexing='ij')
    values=x+10*y+100*z
    blob,info=encode_3dm(values,[10,20,0,13,24,10],0,0)
    path=tmp_path/'field.3dm';path.write_bytes(blob)
    axes,actual,info=read_3dm(path)
    assert info['shape']==[4,3,3] and blob[:4]==b'3DEM'
    assert np.array_equal(actual,values) and np.array_equal(axes[2],[0,5,10])
    bad=bytearray(blob);struct.pack_into('<i',bad,58,-1);path.write_bytes(bad)
    with pytest.raises(BridgeError,match='OFFSET'):read_3dm(path)
    path.write_bytes(blob[:-1])
    with pytest.raises(BridgeError):read_3dm(path)


def test_every_saved_depth_plane_must_meet_its_coordinate_budget():
    values=np.zeros((4,2,2))
    with pytest.raises(BridgeError,match='ROUNDING'):
        encode_3dm(values,[0,0,0,1,1,1],0,0)
    _,info=encode_3dm(values,[0,0,0,1,1,1],1e-6,0)
    assert 0<info['max_coordinate_error_m']<1e-6
    with pytest.raises(BridgeError,match='COLLAPSED_FLOAT32_Z'):
        encode_3dm(values,[0,0,5000000,1,1,5000001],.5,0)


def test_interpolation_methods_are_explicit_and_do_not_turn_zero_into_missing():
    points=np.array([[0,0,0,0],[2,0,0,4]],dtype=float)
    values,meta=interpolate_volume(points,[0,0,0,2,1,1],[3,2,2],
        'idw_3d',2,2,'reject')
    assert values[0,0].tolist()==[0.,2.,4.]
    assert meta['algorithm']=='idw_3d' and np.isfinite(values).all()
    holes=np.array([[x,y,z,z*2] for x,y in [(0,0),(2,0),(0,2),(2,2)] for z in [0,10]],dtype=float)
    volume,_=interpolate_volume(holes,[0,0,0,2,2,10],[3,3,3],
        'borehole_linear_idw',2,4,'reject')
    assert np.all(volume[0]==0) and np.all(volume[1]==10) and np.all(volume[2]==20)
    with pytest.raises(BridgeError,match='VERTICAL'):
        interpolate_volume(holes,[0,0,-1,2,2,10],[3,3,3],'borehole_linear_idw',2,4,'reject')
    unordered=holes.copy();unordered[[0,1]]=unordered[[1,0]]
    with pytest.raises(BridgeError,match='ORDER'):
        interpolate_volume(unordered,[0,0,0,2,2,10],[3,3,3],'borehole_linear_idw',2,4,'reject')
    with pytest.raises(BridgeError,match='DUPLICATE'):
        interpolate_volume(np.vstack((points,[0,0,0,1])),[0,0,0,2,1,1],[3,2,2],'idw_3d',2,2,'reject')


@pytest.mark.parametrize(('axis','position'),[('x',10.5),('y',21.),('z',2.5)])
def test_sections_interpolate_world_positions_with_explicit_axis_mapping(axis,position):
    axes=[np.array([10.,11.,12.,13.]),np.array([20.,22.,24.]),np.array([0.,5.,10.])]
    z,y,x=np.meshgrid(axes[2],axes[1],axes[0],indexing='ij')
    u,v,section,world,meta=slice_volume(axes,x+10*y+100*z,axis,position,'linear')
    assert np.array_equal(world[...,'xyz'.index(axis)],np.full(section.shape,position))
    assert np.allclose(section,world[...,0]+10*world[...,1]+100*world[...,2],rtol=0,atol=1e-12)
    assert section.shape==(len(v),len(u)) and meta['method']=='linear'
    with pytest.raises(BridgeError,match='OUTSIDE'):
        slice_volume(axes,x,'z',11,'linear')


def test_original_captured_headers_match_the_compatible_fixtures_and_their_layer_order():
    from mapgis_agent.volume import _verify_original
    import hashlib
    folder=Path(__file__).parent/'fixtures'
    fixtures=json.loads((folder/'volume-original-readback.json').read_text(encoding='utf8'))
    for item in fixtures:
        path=folder/item['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
        info=read_3bn(path)[1] if path.suffix=='.3bn' else read_3dm(path)[2]
        _verify_original(info,item['readback'])
    model=fixtures[1];lines=model['readback']['lines'].copy()
    # Identical unordered sets must not hide metadata attached to a wrong layer.
    lines[6],lines[14]=lines[14],lines[6]
    with pytest.raises(BridgeError,match='HEADER_DIFF'):
        _verify_original(read_3dm(folder/model['file'])[2],model['readback']|{'lines':lines})


def test_explicit_legacy_statistics_report_retains_both_declared_and_recomputed_values(tmp_path):
    from mapgis_agent.volume import MODEL_HEADER,LAYER_HEADER
    values=np.arange(24,dtype='float32').reshape(2,3,4)
    blob,_=encode_3dm(values,[0,0,0,3,2,1],0,0);legacy=bytearray(blob)
    model=list(MODEL_HEADER.unpack_from(legacy));model[8]=-50.;model[10]=1000.
    MODEL_HEADER.pack_into(legacy,0,*model)
    first=struct.unpack_from('<i',legacy,58)[0]
    layer=list(LAYER_HEADER.unpack_from(legacy,first));layer[12]=99.
    LAYER_HEADER.pack_into(legacy,first,*layer)
    path=tmp_path/'legacy.3dm';path.write_bytes(legacy)
    with pytest.raises(BridgeError,match='STATISTICS'):read_3dm(path)
    axes,actual,info=read_3dm(path,statistics_policy='report_discrepancies')
    np.testing.assert_array_equal(actual,values)
    assert info['mean']==float(values.mean()) and info['range']==[0.,23.]
    assert info['statistics']['declared']['range']==[-50.,23.]
    assert info['statistics']['declared']['mean']==1000.
    assert info['statistics']['computed']['mean']==11.5 and info['statistics']['matches'] is False
    assert info['layers'][0]['statistics']['declared']['mean']==99.
    assert info['layers'][0]['mean']==5.5 and info['layers'][0]['statistics']['matches'] is False
    assert path.read_bytes()==legacy


def test_statistics_report_never_relaxes_offsets_dimensions_or_log_axis_profile(tmp_path):
    from mapgis_agent.volume import MODEL_HEADER,LAYER_HEADER
    blob,_=encode_3dm(np.arange(24,dtype='float32').reshape(2,3,4),[0,0,0,3,2,1],0,0)
    bad=bytearray(blob);struct.pack_into('<i',bad,58,-1)
    path=tmp_path/'bad.3dm';path.write_bytes(bad)
    with pytest.raises(BridgeError,match='OFFSET'):read_3dm(path,statistics_policy='report_discrepancies')
    bad=bytearray(blob);offset=struct.unpack_from('<i',bad,58)[0]
    layer=list(LAYER_HEADER.unpack_from(bad,offset));layer[2]=0
    LAYER_HEADER.pack_into(bad,offset,*layer);path.write_bytes(bad)
    with pytest.raises(BridgeError):read_3dm(path,statistics_policy='report_discrepancies')
    bad=bytearray(blob);header=list(MODEL_HEADER.unpack_from(bad));header[12]=1
    MODEL_HEADER.pack_into(bad,0,*header);path.write_bytes(bad)
    with pytest.raises(BridgeError,match='PROFILE'):read_3dm(path,statistics_policy='report_discrepancies')
    with pytest.raises(BridgeError,match='STATISTICS_POLICY'):read_3dm(path,statistics_policy='ignore_everything')


def test_legacy_volume_page_reads_and_source_info_do_not_infer_coordinate_units(tmp_path):
    import hashlib
    from mapgis_agent.volume import MODEL_HEADER,volume_source_info,read_volume_samples
    values=np.arange(24,dtype='float32').reshape(2,3,4)
    blob,_=encode_3dm(values,[100,200,0,103,202,1],0,0);legacy=bytearray(blob)
    header=list(MODEL_HEADER.unpack_from(legacy));header[8]=-50.
    MODEL_HEADER.pack_into(legacy,0,*header)
    path=tmp_path/'legacy.3dm';path.write_bytes(legacy);digest=hashlib.sha256(legacy).hexdigest()
    info=volume_source_info(tmp_path,str(path),digest,statistics_policy='report_discrepancies')
    assert info['metrics']['statistics']['matches'] is False and info['frame'] is None
    assert info['native_readback_status']=='NOT_REQUESTED'
    page=read_volume_samples(str(path),digest,offset=11,limit=3,statistics_policy='report_discrepancies')
    assert [r['value'] for r in page['records']]==[11.,12.,13.]
    assert [r['xyz'] for r in page['records']]==[[103.,202.,0.],[100.,200.,1.],[101.,200.,1.]]
    assert page['statistics_policy']=='report_discrepancies' and page['frame'] is None


@pytest.mark.parametrize('value,places,expected',[(4172.9140625,6,'4172.914063'),
    (-4172.9140625,6,'-4172.914063'),(100.125,2,'100.13'),(-100.125,2,'-100.13')])
def test_original_header_text_uses_observed_legacy_crt_exact_tie_rounding(value,places,expected):
    from mapgis_agent.volume import _native_fixed
    assert _native_fixed(value,places)==expected
