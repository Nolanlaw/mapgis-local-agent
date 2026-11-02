"""A volume projection keeps its source frame distinct from the native drawing."""
from pathlib import Path
import numpy as np
import pytest
from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.volume import encode_3dm,read_3dm,VolumeFrame


def fixture(folder):
    z,y,x=np.mgrid[:4,:3,:5];values=x+2*y+3*z+.25
    blob,_=encode_3dm(values,[10,20,30,14,22,36],1e-6,1e-6)
    path=folder/'source.3dm';path.write_bytes(blob)
    return path,values


def frame(axis):
    return {'crs':{'definition':'EPSG:3857','axis_order':'xy'},'z_axis':axis,'value_name':'synthetic','value_unit':'unit'}


def test_elevation_render_input_is_exact_copy_and_keeps_source_immutable(tmp_path):
    from mapgis_agent.volume_plot import _render_input
    source,values=fixture(tmp_path);before=source.read_bytes()
    target=tmp_path/'render.3dm'
    metadata=_render_input(source,target,VolumeFrame.model_validate(frame('elevation_up')),None)
    assert source.read_bytes()==target.read_bytes()==before
    assert metadata['native_z']=='elevation in metres; positive up'


def test_depth_render_input_reverses_values_and_negates_z_with_explicit_budget(tmp_path):
    from mapgis_agent.volume_plot import _render_input
    source,values=fixture(tmp_path);before=source.read_bytes()
    target=tmp_path/'render.3dm'
    metadata=_render_input(source,target,VolumeFrame.model_validate(frame('depth_down')),1e-6)
    axes,actual,info=read_3dm(target)
    np.testing.assert_array_equal(actual,values[::-1])
    np.testing.assert_allclose(axes[2],[-36,-34,-32,-30],rtol=0,atol=0)
    assert info['bounds']==[10,20,-36,14,22,-30] and source.read_bytes()==before
    assert metadata['native_z']=='negative depth in metres; positive up' and metadata['max_value_error']==0


def test_depth_does_not_silently_introduce_a_rounding_budget(tmp_path):
    from mapgis_agent.volume_plot import _render_input
    source,_=fixture(tmp_path)
    with pytest.raises(BridgeError,match='VOLUME_PLOT_DEPTH_BUDGET'):
        _render_input(source,tmp_path/'render.3dm',VolumeFrame.model_validate(frame('depth_down')),None)
    assert not (tmp_path/'render.3dm').exists()


def test_prepare_requires_existing_private_ui_opt_in_without_creating_application(tmp_path):
    from mapgis_agent.volume_plot import prepare_volume_plot
    source,_=fixture(tmp_path)
    with pytest.raises(BridgeError,match='VOLUME_NATIVE_ROUTE_NOT_ENABLED'):
        prepare_volume_plot(Bridge(tmp_path),str(source),sha(source.read_bytes()),
            {'frame':frame('elevation_up')},{'name':'view.wp'},'view')
    assert not list((tmp_path/'work').glob('**/application.started.json'))


def test_native_slider_selection_reports_effective_grid_plane_and_ui_quantization():
    from mapgis_agent.volume_native import select_native_plane
    # UI ticks are0.1m but the actual native slice uses the grid-node index.
    plane={'minimum':0.,'maximum':7.,'count':8,'position':3.4,'max_error':.5}
    selected=select_native_plane(plane,0,71)
    assert selected['grid_index']==3 and selected['actual_position']==3.
    assert abs(selected['actual_position']-plane['position'])<=plane['max_error']
    assert selected['tick'] in range(71) and selected['ui_position']>=3.
    with pytest.raises(BridgeError,match='VOLUME_NATIVE_PLANE_ERROR'):
        select_native_plane(plane|{'max_error':.1},0,71)


def test_native_slider_unreachable_subdecimetre_plane_is_not_silently_moved():
    from mapgis_agent.volume_native import select_native_plane
    plane={'minimum':0.,'maximum':.06,'count':5,'position':.03,'max_error':.001}
    with pytest.raises(BridgeError,match='VOLUME_NATIVE_PLANE_ERROR'):
        select_native_plane(plane,0,1)


def test_native_slider_negative_up_coordinates_and_source_float32_offsets():
    from mapgis_agent.volume_native import select_native_plane
    selected=select_native_plane({'minimum':-8.,'maximum':0.,'count':9,'position':-3.,'max_error':0.},0,81)
    assert selected['grid_index']==5 and selected['actual_position']==-3.
    selected=select_native_plane({'minimum':500000.,'maximum':500060.,'count':7,'position':500030.,'max_error':0.},0,601)
    assert selected['grid_index']==3 and selected['actual_position']==500030.


@pytest.mark.parametrize('z_axis',['elevation_up','depth_down'])
def test_plane_budget_is_checked_against_source_nodes_before_native_launch(tmp_path,z_axis):
    from mapgis_agent.volume_plot import _render_input,_settings,VolumePlotParameters
    source,_=fixture(tmp_path);before=source.read_bytes();target=tmp_path/'render.3dm'
    _render_input(source,target,VolumeFrame.model_validate(frame(z_axis)),1e-6)
    parameters={'frame':frame(z_axis),'view':'flat_z','planes':{'z':{'position_m':33.,'max_error_m':.99}}}
    with pytest.raises(BridgeError,match='VOLUME_PLOT_PLANE_ERROR'):
        _settings(source,target,VolumePlotParameters.model_validate(parameters))
    parameters['planes']['z']['max_error_m']=1.
    settings=_settings(source,target,VolumePlotParameters.model_validate(parameters))
    assert settings['planes']['z']['position']==(-33. if z_axis=='depth_down' else 33.)
    assert source.read_bytes()==before


@pytest.mark.parametrize('view,required',[('block_1','xz'),('block_2','yz'),('block_3','xyz')])
def test_block_modes_accept_exactly_the_planes_used_in_the_native_drawing(tmp_path,view,required):
    from mapgis_agent.volume_plot import _settings,VolumePlotParameters
    source,_=fixture(tmp_path);positions={'x':12.,'y':21.,'z':32.}
    parameters={'frame':frame('elevation_up'),'view':view,
        'planes':{axis:{'position_m':positions[axis],'max_error_m':0.} for axis in required}}
    settings=_settings(source,source,VolumePlotParameters.model_validate(parameters))
    assert set(settings['planes'])==set(required)
    parameters['planes'].pop(required[0])
    with pytest.raises(BridgeError,match='VOLUME_PLOT_PLANES'):
        _settings(source,source,VolumePlotParameters.model_validate(parameters))


def test_multilayer_keeps_requested_source_planes_and_depth_draw_order_separate(tmp_path):
    from mapgis_agent.volume_plot import _render_input,_settings,_source_planes,VolumePlotParameters
    from mapgis_agent.volume_native import select_native_plane
    source,_=fixture(tmp_path);target=tmp_path/'render.3dm'
    _render_input(source,target,VolumeFrame.model_validate(frame('depth_down')),1e-6)
    parameters={'frame':frame('depth_down'),'view':'multilayer',
        'layers':[{'position_m':v,'max_error_m':0.} for v in (30.,34.,36.)]}
    parsed=VolumePlotParameters.model_validate(parameters);settings=_settings(source,target,parsed)
    assert settings['planes']=={} and [v['position'] for v in settings['layers']]==[-30.,-34.,-36.]
    selected=[{'axis':'z',**select_native_plane(v,0,61)} for v in settings['layers']]
    plan={'native_settings':settings,'source_file':str(source),'source_frame':parameters['frame'],
        'layer_requests':parameters['layers']}
    actual=_source_planes(tmp_path,plan,{'layer_planes':selected})
    assert [v['actual_position_m'] for v in actual]==[30.,34.,36.]
    assert [v['source_grid_index'] for v in actual]==[0,2,3]
    assert [v['layer'] for v in sorted(actual,key=lambda v:v['native_control']['actual_position'])]==[2,1,0]
    with pytest.raises(BridgeError,match='VOLUME_PLOT_PLANE_READBACK'):
        _source_planes(tmp_path,plan,{'layer_planes':[selected[0]]*3})


@pytest.mark.parametrize('positions',[(32.,30.),(30.,30.)])
def test_multilayer_rejects_unordered_and_duplicate_source_positions(tmp_path,positions):
    from mapgis_agent.volume_plot import _settings,VolumePlotParameters
    source,_=fixture(tmp_path)
    parameters={'frame':frame('elevation_up'),'view':'multilayer',
        'layers':[{'position_m':v,'max_error_m':0.} for v in positions]}
    with pytest.raises(BridgeError,match='VOLUME_PLOT_LAYERS'):
        _settings(source,source,VolumePlotParameters.model_validate(parameters))
