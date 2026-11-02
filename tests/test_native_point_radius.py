"""Radius edits must use native bounds updates and survive unsaved-buffer loss."""
from pathlib import Path
import pytest
from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import prepare_native_operation,native_file_info
from mapgis_agent.workspace import NativeWorkspaces
from mapgis_agent.raster import file_sha
from test_native_raster import native_settings
from test_point_primitives import FRAME


def sample(tmp_path,projected=False):
    b=Bridge(tmp_path);native_settings(tmp_path)
    positions=[(500000,3320000),(502000,3320000)] if projected else [(20,20),(50,20)]
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':list(xy)},'properties':{'ID':7,'名称':'图元'+str(i)},
        'style':{'point_type':i+1,'radius_mm':10,**({'start_angle':30,'end_angle':150} if i==2 else {})}} for i,xy in enumerate(positions,1)]}
    prepare_creation(b,features,{'name':'source.wt','map_scale':50000 if projected else 1,'crs':{'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'} if projected else FRAME},'create')
    return b,Path(b.apply('create')['output'])


@pytest.mark.parametrize('projected',[False,True])
def test_native_circle_and_arc_radius_changes_bounds_not_centres_or_other_parameters(tmp_path,projected):
    b,source=sample(tmp_path,projected);before=source.read_bytes()
    prepared=prepare_native_operation(b,'point.radius',str(source),file_sha(source),{'updates':[{'record_id':2,'radius_mm':15.},{'record_id':1,'radius_mm':5.}]},'radius')
    target=Path(b.apply('radius')['output']);_,old,_=read_native(source);_,new,_=read_native(target)
    assert list(new.point_params['radius_mm'])==[5.,15.] and list(new.data['ID'])==[7,7]
    assert [g.wkt for g in old.geom]==[g.wkt for g in new.geom]
    for key in old.point_params.columns:
        if key!='radius_mm':assert old.point_params[key].equals(new.point_params[key])
    info=native_file_info(tmp_path,str(target),file_sha(target))
    assert info['native_bounds']==([9995.,66385.,10055.,66415.] if projected else [15.,5.,65.,35.])
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid'] and source.read_bytes()==before
    b.recover('radius',str(target),file_sha(target),'undo');assert Path(b.apply('undo')['output']).read_bytes()==before


def test_radius_dirty_buffer_recovers_then_saves_and_reopens_exactly(tmp_path):
    b,source=sample(tmp_path);registry=NativeWorkspaces(b)
    try:
        opened=registry.execute('open','curves',{'source':str(source),'expected_sha256':file_sha(source)})
        edited=registry.execute('edit','curves',{'operation':'point.radius','parameters':{'updates':[{'record_id':1,'radius_mm':15.}]}},0,'radius')
        assert edited['dirty'] and edited['native_pid']==opened['native_pid']
        child=registry.buffers['curves'].process;child.kill();child.wait(timeout=5)
        recovered=registry.execute('recover','curves',{},1);assert recovered['dirty'] and recovered['native_pid']!=opened['native_pid']
        assert read_native(Path(recovered['checkpoint']))[1].point_params['radius_mm'].tolist()==[15.,10.]
        saved=registry.execute('save','curves',{},1,'save');assert not saved['dirty']
        registry.execute('close','curves',{},1)
    finally:registry.close_all()


@pytest.mark.parametrize('updates',[[{'record_id':1,'radius_mm':0}],[{'record_id':1,'radius_mm':float('nan')}],[{'record_id':True,'radius_mm':1}],
    [{'record_id':1,'radius_mm':1},{'record_id':1,'radius_mm':2}],[{'record_id':3,'radius_mm':1}]])
def test_radius_rejects_invalid_size_and_identity_before_child_launch(tmp_path,updates):
    b,source=sample(tmp_path)
    with pytest.raises(BridgeError,match='POINT_RADIUS'):
        prepare_native_operation(b,'point.radius',str(source),file_sha(source),{'updates':updates},'bad')
