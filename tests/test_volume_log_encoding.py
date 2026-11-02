"""Separate exact stored log10 values from explicit physical reconstruction."""
from pathlib import Path
import struct

import numpy as np
import pytest

from mapgis_agent.core import BridgeError, sha
from mapgis_agent.volume import encode_3dm, read_3dm, read_volume_samples, volume_source_info


def sample(folder, values=None):
    values=np.arange(24.,dtype='float64').reshape(3,2,4)/10-1 if values is None else values
    encoded,_=encode_3dm(values,[0.,0.,-2.,3.,1.,0.],1e-6,1e-6)
    encoded=bytearray(encoded);encoded[48:50]=bytes([1,1])
    path=folder/'log.3dm';path.write_bytes(encoded)
    return path


def test_default_reader_still_rejects_log_profile_and_stored_mode_keeps_exact_voxels(tmp_path):
    path=sample(tmp_path)
    with pytest.raises(BridgeError,match='VOLUME_3DM_PROFILE'):read_3dm(path)
    axes,values,info=read_3dm(path,encoding_policy='stored')
    np.testing.assert_array_equal(axes[2],[-2.,-1.,0.])
    np.testing.assert_array_equal(values,(np.arange(24.).reshape(3,2,4)/10-1).astype('float32').astype('float64'))
    assert info['encoding']['stored_profile']=='log10_z_and_value'
    assert info['encoding']['returned_domain']=='stored'
    assert values[1,0,2]==0 # Stored log0 is a valid observation of physical1.


def test_physical_reconstruction_exponentiates_each_saved_voxel_and_recomputes_stats(tmp_path):
    path=sample(tmp_path);before=path.read_bytes()
    axes,data,info=read_3dm(path,encoding_policy='physical')
    stored_axes,stored,raw_info=read_3dm(path,encoding_policy='stored')
    np.testing.assert_allclose(axes[2],[.01,.1,1.],rtol=1e-15)
    np.testing.assert_array_equal(data,np.power(10.,stored))
    assert info['mean']==pytest.approx(float(np.power(10.,stored).mean()))
    assert info['mean']!=pytest.approx(float(10.**stored.mean()))
    assert info['stored_metrics']['range']==raw_info['range']
    assert info['encoding']['returned_domain']=='physical_reconstructed'
    assert info['bounds']==[0.,0.,.01,3.,1.,1.]
    assert path.read_bytes()==before


def test_read_only_info_and_pages_report_the_selected_domain_without_inventing_units(tmp_path):
    path=sample(tmp_path);digest=sha(path.read_bytes())
    source=volume_source_info(tmp_path,str(path),digest,encoding_policy='physical')
    saved=read_volume_samples(str(path),digest,offset=10,limit=1,encoding_policy='stored')
    physical=read_volume_samples(str(path),digest,offset=10,limit=1,encoding_policy='physical')
    assert source['frame'] is None and source['metrics']['encoding']['base']==10
    assert saved['records'][0]['value']==0 and physical['records'][0]['value']==1
    assert saved['records'][0]['xyz'][2]==-1 and physical['records'][0]['xyz'][2]==pytest.approx(.1)
    assert physical['encoding']['returned_domain']=='physical_reconstructed'


@pytest.mark.parametrize('flags',[(1,0),(0,1),(2,2)])
def test_unverified_flag_combinations_are_never_guessed(tmp_path,flags):
    path=sample(tmp_path);data=bytearray(path.read_bytes());data[48:50]=bytes(flags);path.write_bytes(data)
    with pytest.raises(BridgeError,match='VOLUME_3DM_PROFILE'):
        read_3dm(path,encoding_policy='stored')


@pytest.mark.parametrize('log_value',[-1000.,1000.])
def test_physical_overflow_and_positive_underflow_are_explicit_failures(tmp_path,log_value):
    path=sample(tmp_path,np.ones((3,2,4))*log_value)
    with pytest.raises(BridgeError,match='VOLUME_LOG10_RANGE'):
        read_3dm(path,encoding_policy='physical')


def test_statistics_policy_stays_separate_from_encoding_policy(tmp_path):
    path=sample(tmp_path);data=bytearray(path.read_bytes());struct.pack_into('<f',data,32,777.)
    path.write_bytes(data)
    with pytest.raises(BridgeError,match='VOLUME_3DM_STATISTICS'):
        read_3dm(path,encoding_policy='physical')
    _,_,info=read_3dm(path,'report_discrepancies','physical')
    assert not info['stored_metrics']['statistics']['matches']
    assert info['range'][0]==pytest.approx(.1)
