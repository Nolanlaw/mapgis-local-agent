"""Extend toward the first explicit target on a directed endpoint ray."""
from pathlib import Path
import numpy as np
import pytest
from shapely.geometry import LineString, mapping
from mapgis_agent.core import BridgeError,read_native,sha
from mapgis_agent.native_backend import prepare_native_operation
from mapgis_agent.workspace import NativeWorkspaces
from test_native_line_editing import native_case,create,LOCAL


@pytest.mark.parametrize('projected',[False,True])
@pytest.mark.parametrize('endpoint',['start','end'])
def test_extend_hits_nearest_forward_boundary_and_retains_identity(native_case,projected,endpoint):
    origin,factor=(500000.,100.) if projected else (0.,1.)
    source=create(native_case,'extend.wl',LineString([(origin,0),(origin+3*factor,0),(origin+3*factor,4*factor)]),
        {'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'} if projected else LOCAL,50000 if projected else 1000,
        LineString([(origin+10*factor,0),(origin+11*factor,0)]))
    target={'type':'MultiLineString','coordinates':[
        [[origin-2*factor,-factor],[origin-2*factor,factor]],[[origin-5*factor,-factor],[origin-5*factor,factor]]]} if endpoint=='start' else {
        'type':'MultiLineString','coordinates':[[[origin-2*factor,7*factor],[origin+6*factor,7*factor]],[[origin-2*factor,9*factor],[origin+6*factor,9*factor]]]}
    before=source.read_bytes()
    prepared=prepare_native_operation(native_case,'line.extend_to',str(source),sha(before),
        {'updates':[{'record_id':1,'end':endpoint,'max_distance':10*factor,'boundary':target}]},'extend')
    output=Path(native_case.apply('extend')['output']);old,new=read_native(source)[1],read_native(output)[1]
    expected=[[-2,0],[0,0],[3,0],[3,4]] if endpoint=='start' else [[0,0],[3,0],[3,4],[3,7]]
    actual=np.asarray(new.geom[0].coords);actual[:,0]-=origin;actual/=factor
    np.testing.assert_allclose(actual,expected,atol=1e-10,rtol=0)
    assert old.geom[1].equals_exact(new.geom[1],0) and old.data.equals(new.data) and old.line_params.equals(new.line_params)
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid'] and source.read_bytes()==before


def test_extend_already_connected_polygon_boundary_is_noop(native_case):
    source=create(native_case,'connected.wl',LineString([(0,0),(3,0),(3,4)]),LOCAL,1000)
    from shapely.geometry import box
    prepare_native_operation(native_case,'line.extend_to',str(source),sha(source.read_bytes()),{'updates':[
        {'record_id':1,'end':'end','max_distance':10.,'boundary':mapping(box(2,4,4,6))}]},'connected')
    output=Path(native_case.apply('connected')['output'])
    assert read_native(output)[1].geom[0].equals_exact(read_native(source)[1].geom[0],0)


@pytest.mark.parametrize('update',[
    {'record_id':1,'end':'end','max_distance':2.,'boundary':{'type':'LineString','coordinates':[[0,7],[6,7]]}},
    {'record_id':1,'end':'end','max_distance':0.,'boundary':{'type':'LineString','coordinates':[[0,7],[6,7]]}},
    {'record_id':1,'end':'side','max_distance':10.,'boundary':{'type':'LineString','coordinates':[[0,7],[6,7]]}},
    {'record_id':1,'end':'end','max_distance':10.,'boundary':{'type':'LineString','coordinates':[[0,float('inf')],[6,7]]}},
])
def test_unreachable_or_invalid_target_rejected_before_native_launch(native_case,update,monkeypatch):
    import mapgis_agent.native_backend as backend
    source=create(native_case,'reject.wl',LineString([(0,0),(3,0),(3,4)]),LOCAL,1000)
    calls=[];monkeypatch.setattr(backend,'_invoke',lambda *a,**k:calls.append(a))
    with pytest.raises(BridgeError,match='EXTEND_'):
        prepare_native_operation(native_case,'line.extend_to',str(source),sha(source.read_bytes()),{'updates':[update]},'bad')
    assert not calls


def test_extend_dirty_checkpoint_recovers_without_double_extension(native_case):
    source=create(native_case,'buffer.wl',LineString([(0,0),(3,0),(3,4)]),LOCAL,1000);registry=NativeWorkspaces(native_case)
    try:
        registry.execute('open','extend',{'source':str(source),'expected_sha256':sha(source.read_bytes())})
        edited=registry.execute('edit','extend',{'operation':'line.extend_to','parameters':{'updates':[
            {'record_id':1,'end':'end','max_distance':10.,'boundary':{'type':'LineString','coordinates':[[0,7],[6,7]]}}]}},0,'edit')
        assert edited['dirty'];checkpoint=Path(edited['checkpoint']).read_bytes();child=registry.buffers['extend'].process;child.kill();child.wait(timeout=5)
        recovered=registry.execute('recover','extend',{},1);assert recovered['dirty'] and Path(recovered['checkpoint']).read_bytes()==checkpoint
        assert not registry.execute('save','extend',{},1,'save')['dirty']
        registry.execute('close','extend',{},1)
    finally:registry.close_all()
