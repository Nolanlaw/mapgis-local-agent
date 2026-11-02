"""Signed endpoint distances must edit the existing native line identities."""
from pathlib import Path
import numpy as np
import pytest
from shapely.geometry import LineString
from mapgis_agent.core import BridgeError, read_native, sha
from mapgis_agent.native_backend import prepare_native_operation
from mapgis_agent.workspace import NativeWorkspaces
from test_native_line_editing import native_case, create, LOCAL


@pytest.mark.parametrize('projected',[False,True])
@pytest.mark.parametrize('start,end,expected',[
    (2.,1.,[[-2.,0.],[0.,0.],[3.,0.],[3.,4.],[3.,5.]]),
    (-1.,-2.,[[1.,0.],[3.,0.],[3.,2.]]),
    (-8.,3.,[[3.,5.],[3.,7.]]),
    (5.,-8.,[[-5.,0.],[-1.,0.]]),
])
def test_native_resize_extension_shortening_and_beyond_original_interval(native_case,projected,start,end,expected):
    origin,factor=(500000.,100.) if projected else (0.,1.)
    source=create(native_case,'resize.wl',LineString([(origin,0.),(origin+3*factor,0.),(origin+3*factor,4*factor)]),
        {'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'} if projected else LOCAL,50000 if projected else 1000,
        LineString([(origin+10*factor,0.),(origin+11*factor,0.)]))
    before=source.read_bytes()
    prepared=prepare_native_operation(native_case,'line.resize',str(source),sha(before),
        {'updates':[{'record_id':1,'start_delta':start*factor,'end_delta':end*factor}]},'resize')
    output=Path(native_case.apply('resize')['output']);old,new=read_native(source)[1],read_native(output)[1]
    vertices=np.asarray(new.geom[0].coords);vertices[:,0]-=origin;vertices/=factor
    np.testing.assert_allclose(vertices,expected,rtol=0,atol=1e-10)
    assert new.geom[0].length==pytest.approx((7+start+end)*factor)
    assert new.geom[1].equals_exact(old.geom[1],0.) and old.data.equals(new.data) and old.line_params.equals(new.line_params)
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid'] and source.read_bytes()==before
    native_case.recover('resize',str(output),sha(output.read_bytes()),'undo')
    assert Path(native_case.apply('undo')['output']).read_bytes()==before


def test_resize_unsaved_native_buffer_survives_loss_and_saves(native_case):
    source=create(native_case,'buffer.wl',LineString([(0,0),(3,0),(3,4)]),LOCAL,1000);registry=NativeWorkspaces(native_case)
    try:
        opened=registry.execute('open','resize',{'source':str(source),'expected_sha256':sha(source.read_bytes())})
        edited=registry.execute('edit','resize',{'operation':'line.resize','parameters':{'updates':[{'record_id':1,'start_delta':2.,'end_delta':-1.}]}},0,'edit')
        assert edited['dirty'] and edited['native_pid']==opened['native_pid']
        checkpoint=Path(edited['checkpoint']).read_bytes();child=registry.buffers['resize'].process;child.kill();child.wait(timeout=5)
        restored=registry.execute('recover','resize',{},1);assert restored['dirty'] and Path(restored['checkpoint']).read_bytes()==checkpoint
        assert not registry.execute('save','resize',{},1,'save')['dirty']
        registry.execute('close','resize',{},1)
    finally:registry.close_all()


@pytest.mark.parametrize('updates',[
    [{'record_id':1,'start_delta':float('nan'),'end_delta':0.}],
    [{'record_id':True,'start_delta':1.,'end_delta':0.}],
    [{'record_id':1,'start_delta':-7.,'end_delta':0.}],
    [{'record_id':1,'start_delta':-8.,'end_delta':0.}],
    [{'record_id':1,'start_delta':1.,'end_delta':False}],
])
def test_resize_rejects_invalid_or_collapsed_intervals_before_native_launch(native_case,updates,monkeypatch):
    import mapgis_agent.native_backend as backend
    source=create(native_case,'reject.wl',LineString([(0,0),(3,0),(3,4)]),LOCAL,1000)
    calls=[];monkeypatch.setattr(backend,'_invoke',lambda *a,**k:calls.append(a))
    with pytest.raises(BridgeError,match='RESIZE_'):
        prepare_native_operation(native_case,'line.resize',str(source),sha(source.read_bytes()),{'updates':updates},'invalid')
    assert not calls
