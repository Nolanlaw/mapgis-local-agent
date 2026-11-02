"""Independent rectangular area weights; intensive/extensive values stay distinct."""
from pathlib import Path
import json
import pytest
from shapely.geometry import box,mapping
from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import prepare_native_operation
from test_spatial_query import LOCAL
from test_spatial_summary import prepare
from test_native_wp import enable_native


def case(tmp_path,projected=False,overlap=False,geographic=False):
    bridge=Bridge(tmp_path);enable_native(bridge)
    origin,factor=(500000.,100.) if projected else (0.,1.)
    frame={'definition':'EPSG:4326','axis_order':'xy'} if geographic else {'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'} if projected else LOCAL
    convert=lambda a,b,c,d:box(origin+a*factor,b*factor,origin+c*factor,d*factor)
    layers=[('zones',[convert(0,0,5,2),convert(5,0,6,2),convert(20,0,22,2)],[{}, {}, {}]),
            ('values',[convert(0,0,4,2),convert(2,-1,5,3) if overlap else convert(4,0,6,2),convert(6,0,8,2)],
                [{'grade':10.,'total':100.},{'grade':20.,'total':40.},{'grade':30.,'total':60.}])]
    results=[]
    for name,geometries,properties in layers:
        prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':mapping(g),
            'properties':{'ID':7,**row}} for i,(g,row) in enumerate(zip(geometries,properties),1)]},
            {'name':name+'.wp','crs':frame,'map_scale':50000 if projected else 1000},name)
        results.append(bridge.apply(name))
    parameters={'predicate':'intersects','match_policy':'all','weighting':'intersection_area','columns':[
        {'operation':'count','name':'matches'},
        {'operation':'weighted_mean','field':'grade','name':'grade_mean','empty_value':-999.},
        {'operation':'prorated_sum','field':'total','name':'allocated','empty_value':0.},
        {'operation':'coverage_area','name':'covered'}]}
    if not geographic and not projected:parameters['crs']=LOCAL
    return bridge,*results,parameters,factor


@pytest.mark.parametrize('projected',[False,True])
def test_area_weighted_mean_allocation_coverage_native_save_and_original_preservation(tmp_path,projected):
    bridge,zones,values,parameters,factor=case(tmp_path,projected)
    before=Path(zones['output']).read_bytes();prepared=prepare(bridge,zones,values,parameters,'weighted');output=bridge.apply('weighted')
    blob,reader,sections=read_native(Path(output['output']));old,_,old_sections=read_native(Path(zones['output']))
    assert reader.data['matches'].tolist()==[2,1,0]
    assert reader.data['grade_mean'].tolist()==pytest.approx([12.,20.,-999.])
    assert reader.data['allocated'].tolist()==pytest.approx([120.,20.,0.])
    assert reader.data['covered'].tolist()==pytest.approx([10*factor**2,2*factor**2,0.])
    assert sum(reader.data['allocated'])==pytest.approx(140.)
    assert all(old[a:a+n]==blob[b:b+m] for i,((a,n),(b,m)) in enumerate(zip(old_sections,sections)) if i!=9)
    assert prepared['metrics']['pairs']==3 and prepared['area_weighting']['touches_excluded']>=1
    prepare_native_operation(bridge,'resave',output['output'],output['candidate_sha256'],{},'native')
    saved=read_native(Path(bridge.apply('native')['output']))[1];assert saved.data.equals(reader.data)
    assert Path(zones['output']).read_bytes()==before


def test_coverage_is_union_area_while_each_source_value_has_its_own_weight(tmp_path):
    bridge,zones,values,parameters,_=case(tmp_path,overlap=True)
    prepare(bridge,zones,values,parameters,'overlap');reader=read_native(Path(bridge.apply('overlap')['output']))[1]
    assert reader.data['grade_mean'].tolist()==pytest.approx([100/7,-999.,-999.])
    assert reader.data['covered'].tolist()==[10.,0.,0.]  # Per-feature overlaps total14, their union covers10.
    assert reader.data['allocated'].tolist()==[120.,0.,0.]


def test_area_mode_rejects_geographic_measurement_and_requires_explicit_weight_semantics(tmp_path):
    bridge,zones,values,parameters,_=case(tmp_path,geographic=True)
    with pytest.raises(BridgeError,match='SPATIAL_AREA_FRAME'):prepare(bridge,zones,values,parameters,'geographic')


def test_weighted_operators_cannot_silently_run_as_unweighted_statistics(tmp_path):
    bridge,zones,values,parameters,_=case(tmp_path)
    parameters.pop('weighting')
    with pytest.raises(BridgeError,match='SPATIAL_AREA_WEIGHTING'):prepare(bridge,zones,values,parameters,'missing-mode')


def test_skipped_null_weight_is_removed_from_the_mean_denominator():
    from mapgis_agent.spatial_summary import SummaryColumn,_area_value
    mean=SummaryColumn(name='mean',operation='weighted_mean',field='x',empty_value=-999.)
    total=SummaryColumn(name='total',operation='prorated_sum',field='x',empty_value=0.)
    assert _area_value(mean,[10.,None],[1.,9.],[2.,10.],0.,'skip')==10.
    assert _area_value(total,[100.,None],[1.,9.],[2.,10.],0.,'skip')==50.
    assert _area_value(mean,[None],[9.],[10.],0.,'skip')==-999.
    with pytest.raises(BridgeError,match='SPATIAL_NULL_VALUE'):_area_value(mean,[10.,None],[1.,9.],[2.,10.],0.,'error')


def test_boundary_only_contacts_do_not_create_false_multiple_area_owners(tmp_path):
    bridge,zones,values,parameters,_=case(tmp_path)
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{'ID':7}}
        for i,g in enumerate([box(0,0,6,2),box(6,0,8,2)],1)]},{'name':'touch-zones.wp','crs':LOCAL,'map_scale':1000},'touch-zones')
    zones=bridge.apply('touch-zones');parameters['match_policy']='unique'
    prepared=prepare(bridge,zones,values,parameters,'unique-area');reader=read_native(Path(bridge.apply('unique-area')['output']))[1]
    assert prepared['metrics']['pairs']==3 and prepared['metrics']['multiply_matched_right_records']==0
    assert prepared['area_weighting']['touches_excluded']==2
    assert reader.data['covered'].tolist()==[12.,4.] and reader.data['allocated'].tolist()==[140.,60.]
