"""Known Cartesian transforms, independent PROJ and withheld-point failures."""
import json
import numpy as np
import pytest
from pyproj import Transformer
from mapgis_agent.core import BridgeError


def fit(model,source,target,**parameters):
    from mapgis_agent.control_transform import fit_control_transform
    dimension=len(source[0]);frame={'kind':'planar' if dimension==2 else 'geocentric','unit':'metre','axis_order':'xy' if dimension==2 else 'xyz'}
    return fit_control_transform(model,[{'source':[float(v) for v in a],'target':[float(v) for v in b]} for a,b in zip(source,target)],frame,
        max_fit_error=parameters.pop('max_fit_error',1e-6),**parameters)


def transformed(transformer,points):return np.asarray(transformer.transform(*np.asarray(points).T)).T


def test_similarity_four_parameters_matches_independent_proj_in_projected_coordinates():
    x=np.array([[500000.,3300000.],[504000.,3300000.],[500000.,3304000.],[504000.,3304000.]])
    operation=Transformer.from_pipeline('+proj=helmert +x=120 +y=-45 +s=1.025 +theta=-111600')
    y=transformed(operation,x);query=[[500123.,3300321.],[501000.,3301200.]];check=[[501123.,3302345.]]
    result=fit('similarity2d',x,y,points=query,checkpoints=[{'source':check[0],'target':transformed(operation,check)[0].tolist()}],max_checkpoint_error=1e-6)
    np.testing.assert_allclose(result['points'],transformed(operation,query),rtol=0,atol=2e-8)
    np.testing.assert_allclose(transformed(Transformer.from_pipeline(result['proj_pipeline']),query),result['points'],rtol=0,atol=2e-8)
    assert result['parameters']['rotation_degrees']==pytest.approx(31.,abs=1e-10)
    assert result['parameters']['scale']==pytest.approx(1.025,abs=1e-12)
    assert result['independent_checkpoints']['count']==1 and result['native_subset_transform']['coordinate_frame']=='source'
    json.dumps(result,allow_nan=False)


@pytest.mark.parametrize('dimension',[2,3])
def test_translation_only_in_both_dimensions_and_inverse_queries(dimension):
    x=np.array([[1.,2.,3.],[4.,5.,8.],[9.,10.,4.]])[:,:dimension];delta=np.array([12.,-3.,17.])[:dimension];y=x+delta
    result=fit('translation'+str(dimension)+'d',x,y,points=y.tolist(),direction='inverse')
    np.testing.assert_allclose(result['points'],x,rtol=0,atol=1e-12)
    np.testing.assert_allclose(result['parameters']['translation'],delta)
    assert result['independent_checkpoints'] is None and result['ground_accuracy'].startswith('NOT_VERIFIED')


def test_affine_fit_is_centered_at_large_map_coordinates_and_keeps_shear():
    x=np.array([[6e6,3e6],[6e6+.2,3e6],[6e6,3e6+.3],[6e6+.1,3e6+.2]])
    linear=np.array([[1.2,.3],[-.2,.9]]);offset=np.array([1000.,-2000.]);y=x@linear.T+offset
    result=fit('affine2d',x,y,points=x.tolist())
    np.testing.assert_allclose(result['points'],y,rtol=0,atol=5e-9)
    assert result['native_subset_transform'] is None and len(result['affine_matrix'])==6


@pytest.mark.parametrize('large_rotation',[False,True])
def test_exact_seven_dof_fit_matches_independent_proj_and_exported_euler_convention(large_rotation):
    x=np.array([[3650000.,800000.,5100000.],[3750000.,800000.,5100000.],[3650000.,900000.,5100000.],[3650000.,800000.,5200000.],[3700000.,870000.,5130000.]])
    angles=[82800.,-61200.,147600.] if large_rotation else [2.5,-1.2,4.5]
    pipeline='+proj=helmert +x=1234.2 +y=-789.6 +z=3456.8 +s=3.25 +rx={} +ry={} +rz={} +convention=position_vector +exact'.format(*angles)
    operation=Transformer.from_pipeline(pipeline);y=transformed(operation,x);query=[[3690000.,835000.,5142000.]]
    result=fit('similarity3d',x,y,points=query,max_fit_error=2e-7)
    np.testing.assert_allclose(result['points'],transformed(operation,query),rtol=0,atol=3e-8)
    np.testing.assert_allclose(transformed(Transformer.from_pipeline(result['proj_pipeline']),query),result['points'],rtol=0,atol=3e-8)
    assert result['parameters']['scale']==pytest.approx(1.00000325,abs=2e-14)
    assert np.linalg.det(result['parameters']['rotation_matrix'])==pytest.approx(1.)
    assert 'rotation_vector_radians' in result['parameters'] and result['proj_pipeline'].endswith('+exact')


@pytest.mark.parametrize('model,x,y',[
    ('similarity2d',[[0.,0.],[0.,0.]],[[1.,2.],[1.,2.]]),
    ('affine2d',[[0.,0.],[1.,1.],[2.,2.]],[[1.,2.],[2.,3.],[3.,4.]]),
    ('similarity3d',[[0.,0.,0.],[1.,0.,0.],[2.,0.,0.]],[[0.,1.,0.],[1.,1.,0.],[2.,1.,0.]]),
    ('similarity2d',[[0.,0.],[1.,0.]],[[1.,2.],[1.,2.]]),
    ('affine2d',[[0.,0.],[1.,0.],[0.,1.]],[[0.,0.],[1.,0.],[2.,0.]])])
def test_degenerate_or_repeated_controls_do_not_create_a_transform(model,x,y):
    with pytest.raises(BridgeError,match='CONTROL_(DEGENERATE|DUPLICATE|SINGULAR)'):fit(model,x,y)


def test_perfect_fit_does_not_override_bad_withheld_point_and_reused_control_is_rejected():
    x=[[0.,0.],[10.,0.],[0.,10.]];y=[[1.,2.],[11.,2.],[1.,12.]]
    with pytest.raises(BridgeError,match='CONTROL_CHECKPOINT_RESIDUAL'):
        fit('similarity2d',x,y,checkpoints=[{'source':[5.,5.],'target':[99.,99.]}],max_checkpoint_error=.1)
    with pytest.raises(BridgeError,match='CONTROL_CHECKPOINT_REUSED'):
        fit('similarity2d',x,y,checkpoints=[{'source':x[0],'target':y[0]}])
    with pytest.raises(BridgeError,match='CONTROL_CHECKPOINTS_REQUIRED'):
        fit('similarity2d',x,y,max_checkpoint_error=.1)


def test_reflection_is_not_silently_used_by_proper_similarity():
    x=np.array([[0.,0.],[10.,0.],[0.,10.],[10.,10.]]);y=x*np.array([-1.,1.])
    with pytest.raises(BridgeError,match='CONTROL_(FIT_RESIDUAL|DEGENERATE|SINGULAR)'):fit('similarity2d',x,y)


def test_noisy_fit_is_checked_even_with_no_independent_controls():
    x=[[0.,0.],[10.,0.],[0.,10.],[10.,10.]];y=[[1.,2.],[11.,2.],[1.,12.],[12.,13.]]
    with pytest.raises(BridgeError,match='CONTROL_FIT_RESIDUAL'):fit('similarity2d',x,y,max_fit_error=.01)


def test_frame_and_finite_coordinate_contracts_are_explicit():
    from mapgis_agent.control_transform import fit_control_transform
    controls=[{'source':[0.,0.],'target':[1.,2.]},{'source':[10.,0.],'target':[11.,2.]}]
    with pytest.raises(BridgeError,match='CONTROL_FRAME'):
        fit_control_transform('similarity2d',controls,{'kind':'geographic','unit':'degree','axis_order':'xy'},max_fit_error=1.)
    controls[0]['source'][0]=True
    with pytest.raises(BridgeError,match='CONTROL_COORDINATES'):
        fit_control_transform('similarity2d',controls,{'kind':'planar','unit':'metre','axis_order':'xy'},max_fit_error=1.)


@pytest.mark.parametrize('reflection',[False,True])
def test_affine_chain_uses_existing_native_rotate_scale_translate_without_losing_shear(reflection):
    from mapgis_agent.native_com import _subset_matrix
    x=np.array([[0.,0.],[100.,0.],[0.,100.],[100.,100.]])
    a=np.array([[-1.2 if reflection else 1.2,.3],[-.2,.9]]);t=np.array([12.,-3.]);y=x@a.T+t
    result=fit('affine2d',x,y);current=x.copy()
    assert len(result['native_subset_transforms'])==2
    for operation in result['native_subset_transforms']:
        aa,b,d,e,tx,ty=_subset_matrix(operation);current=current@np.array([[aa,b],[d,e]]).T+[tx,ty]
    np.testing.assert_allclose(current,y,rtol=0,atol=1e-12)
