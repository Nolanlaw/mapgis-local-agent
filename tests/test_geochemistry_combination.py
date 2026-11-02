"""Independent rectilinear cell coverage and scalar anomaly feature statistics."""
import json,math
from pathlib import Path
import pytest
from pymapgis import Reader
from shapely.geometry import Point,Polygon,box,mapping
from shapely.ops import unary_union

from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha
from mapgis_agent.geochemistry_combination import prepare_combination

FRAME={'kind':'local','name':'declared combination domain','unit':'metre','axis_order':'xy','reference_sha256':'c'*64}


def dataset(tmp_path,points='grid',frame=None):
    bridge=Bridge(tmp_path);frame=frame or FRAME
    shapes=[Polygon([(0,0),(4,0),(4,4),(0,4)],holes=[[(1,1),(2,1),(2,2),(1,2)]]),box(0,-1,6,3),box(3,1,5,5)]
    layers=[]
    for name,shape in zip(['Cu','Pb','Zn'],shapes):
        values=[shape,shape] if name=='Cu' else [shape]
        prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{'original':i}} for i,g in enumerate(values,1)]},
            {'name':name+'.wp','crs':frame,'map_scale':1000},name)
        native=Path(bridge.apply(name)['output']);layers.append({'name':name,'regions':{'file':str(native),'expected_sha256':file_sha(native)},
            'value_field':name,'unit':None if name=='Zn' else 'ppm','threshold':{'Cu':10.,'Pb':20.,'Zn':30.}[name],
            'background':{'Cu':5.,'Pb':10.,'Zn':15.}[name],'weight':{'Cu':1.,'Pb':2.,'Zn':1.}[name]})
    coordinates=[(x+.5,y+.5) for x in range(-1,7) for y in range(-2,6)]+[(4.,2.)] if points=='grid' else [(2.5,.5)] if points=='one' else [(20.,20.)]
    features=[]
    for i,(x,y) in enumerate(coordinates,1):
        values={'SAMPLE':f'S{i:03d}','Cu':0. if (x,y)==(2.5,.5) else 3*x+2*y+20,'Pb':x+4*y+25,'Zn':2*x+y+50}
        features.append({'type':'Feature','id':str(i),'geometry':mapping(Point(x,y)),'properties':values})
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},{'name':'samples.wt','crs':frame,'map_scale':1000},'samples')
    file=Path(bridge.apply('samples')['output']);samples={'file':str(file),'expected_sha256':file_sha(file)}
    parameters={'domain_id':'D0','minimum_elements':2,'boundary':'strict_interior','comparison':'gt',
        'strength_rule':'weighted_mean_member_ratio','undefined_numeric_value':-9999.,'sample_id_field':'SAMPLE'}
    return bridge,samples,layers,parameters,{'name':'combined.wp','crs':frame,'map_scale':1000},features


def run(bridge,samples,elements,p,t,identity='combine'):
    prepared=prepare_combination(bridge,samples,elements,p,t,identity);output=bridge.apply(identity)
    report=json.loads(Path(output['output']).with_name('analysis.json').read_text(encoding='utf8'))
    return prepared,output,report


def scalar_members(x,y):
    return [name for name,inside in [('Cu',0<x<4 and 0<y<4 and not (1<x<2 and 1<y<2)),
        ('Pb',0<x<6 and -1<y<3),('Zn',3<x<5 and 1<y<5)] if inside]


def test_exact_k_of_n_footprints_holes_duplicate_regions_and_scalar_statistics(tmp_path):
    bridge,samples,elements,p,t,features=dataset(tmp_path)
    original={s['file']:file_sha(Path(s['file'])) for s in [samples,*[e['regions'] for e in elements]]}
    _,output,r=run(bridge,samples,elements,p,t);reader=Reader(output['output']);union=unary_union(reader.geom)
    assert union.area==14 and not union.covers(Point(1.5,1.5))
    expected={};actual={}
    for x in range(-1,7):
        for y in range(-2,6):
            names=scalar_members(x+.5,y+.5);included=len(names)>=2
            assert union.covers(Point(x+.5,y+.5))==included
            if included:expected[tuple(names)]=expected.get(tuple(names),0)+1
    for i,feature in enumerate(r['features']):
        g=reader.geom[i];names=feature['members'];actual[tuple(names)]=actual.get(tuple(names),0)+g.area
        assert reader.data['SUPPORT'].iloc[i]==len(names)<=3
        assert reader.data['AREA_M2'].iloc[i]==g.area
        selected=[f for f in features if g.contains(Point(f['geometry']['coordinates']))]
        assert feature['sample_count']==len(selected)
        ratios=[];weights=[]
        for j,e in enumerate(elements,1):
            row=feature['elements'][e['name']];values=[f['properties'][e['name']] for f in selected]
            mean=math.fsum(values)/len(values) if values else None
            assert row['mean']==mean
            if mean is not None:
                ratio=mean/e['threshold'];scale=ratio*g.area/1e6;rnap=(mean-e['background'])/e['background']*g.area/1e6
                assert row['ratio_to_threshold']==ratio and row['scale_threshold_km2']==pytest.approx(scale,rel=1e-14)
                assert row['rnap_km2']==pytest.approx(rnap,rel=1e-14)
                assert reader.data[f'E{j:02d}_RATIO'].iloc[i]==ratio
                if e['name'] in names:ratios.append(ratio);weights.append(e['weight'])
            if e['name']=='Cu' and 'Cu' in names:assert feature['source_records']['Cu']==[1,2]
        if ratios:assert feature['strength']==pytest.approx(math.fsum(v*w for v,w in zip(ratios,weights))/math.fsum(weights),rel=1e-14)
    assert actual==expected
    assert len(r['boundary_sample_record_ids'])==1 and r['boundary_sample_record_ids'][0]==len(features)
    assert all(file_sha(Path(file))==checksum for file,checksum in original.items())
    assert prepare_combination(bridge,samples,elements,p,t,'combine')['state']=='APPLIED'


def test_boundary_owner_is_an_explicit_counting_policy(tmp_path):
    bridge,s,e,p,t,_=dataset(tmp_path)
    with pytest.raises(BridgeError,match='MULTIPLE_OWNERS'):
        prepare_combination(bridge,s,e,p|{'boundary':'unique_inclusive'},t,'ambiguous')
    _,_,report=run(bridge,s,e,p|{'boundary':'all_inclusive'},t,'all')
    assert report['multiply_matched_sample_record_ids'] and report['boundary_sample_record_ids']
    assert report['statistics_scope'].startswith('All physical sample records')


def test_empty_or_one_sample_is_not_a_measured_zero(tmp_path):
    bridge,s,e,p,t,_=dataset(tmp_path,points='one');_,out,r=run(bridge,s,e,p,t)
    reader=Reader(out['output'])
    assert any(f['sample_count']==0 for f in r['features'])
    for i,f in enumerate(r['features']):
        for j,name in enumerate(['Cu','Pb','Zn'],1):
            row=f['elements'][name]
            assert row['sample_sd'] is None and reader.data[f'E{j:02d}_SD'].iloc[i]==-9999
            if f['sample_count']==0:assert row['mean'] is None and reader.data[f'E{j:02d}_MEAN'].iloc[i]==-9999
            elif name=='Cu':assert row['mean']==0 and row['ratio_to_threshold']==0


def test_millimetre_local_frame_has_explicit_square_unit_conversion(tmp_path):
    frame=FRAME|{'unit':'millimetre'};bridge,s,e,p,t,_=dataset(tmp_path,frame=frame);_,_,r=run(bridge,s,e,p,t)
    assert sum(f['area_m2'] for f in r['features'])==pytest.approx(14e-6,rel=1e-14)
    assert r['metres_per_coordinate_unit']==.001


@pytest.mark.parametrize('rule',['weighted_sum_member_ratio','maximum_weighted_member_ratio'])
def test_strength_equation_is_frozen_and_reported(tmp_path,rule):
    bridge,s,e,p,t,_=dataset(tmp_path);_,_,r=run(bridge,s,e,p|{'strength_rule':rule},t)
    for f in r['features']:
        if not f['sample_count']:continue
        values=[f['elements'][a['name']]['ratio_to_threshold']*a['weight'] for a in e if a['name'] in f['members']]
        assert f['strength']==pytest.approx(math.fsum(values) if rule.startswith('weighted_sum') else max(values),rel=1e-14)


def test_no_composite_area_returns_real_empty_report_without_fake_native_file(tmp_path):
    bridge,s,e,p,t,_=dataset(tmp_path);e=e[:1]
    _,output,r=run(bridge,s,e,p|{'minimum_elements':1},t,'one-element')
    assert r['features']
    # A source WP can have no area in common with another source.
    from shapely.affinity import translate
    point=bridge.status('Zn');old=Reader(point['output']).geom[0]
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'far','geometry':mapping(translate(old,100,100)),'properties':{}}]},
        {'name':'far.wp','crs':FRAME,'map_scale':1000},'far')
    far=bridge.apply('far');e=[e[0],{'name':'Pb','regions':{'file':far['output'],'expected_sha256':far['candidate_sha256']},'value_field':'Pb','unit':'ppm','threshold':20.}]
    _,output,r=run(bridge,s,e,p,t,'empty')
    assert r['features']==[] and Path(output['output']).name=='analysis.json'
    assert not Path(output['output']).with_name('combined.wp').exists()


def test_invalid_source_fields_and_region_budget_are_explicit(tmp_path):
    bridge,s,e,p,t,_=dataset(tmp_path)
    with pytest.raises(BridgeError,match='STALE'):prepare_combination(bridge,s|{'expected_sha256':'0'*64},e,p,t,'stale')
    with pytest.raises(BridgeError,match='COMBINATION_ELEMENTS'):prepare_combination(bridge,s,[e[0],e[0]],p,t,'duplicates')
    with pytest.raises(BridgeError,match='COMBINATION_LIMIT'):prepare_combination(bridge,s,e,p|{'max_regions':1},t,'budget')
    with pytest.raises(BridgeError,match='COMBINATION_FIELD'):prepare_combination(bridge,s,[e[0]|{'value_field':'missing'},*e[1:]],p,t,'missing')


def test_source_mutation_after_prepare_does_not_publish(tmp_path):
    bridge,s,e,p,t,_=dataset(tmp_path);prepare_combination(bridge,s,e,p,t,'stale-later')
    with Path(s['file']).open('ab') as stream:stream.write(b'changed')
    with pytest.raises(BridgeError,match='STALE'):bridge.apply('stale-later')


def test_observed_centres_preserve_all_maximum_ties_and_empty_faces(tmp_path):
    bridge,s,e,p,t,_=dataset(tmp_path,points='one')
    _,output,r=run(bridge,s,e,p|{'concentration_centres':'all_observed_maxima'},t)
    centres=r['concentration_centres'];native=Reader(Path(output['output']).with_name(centres['native_output']))
    assert len(centres['rows'])==len(native.geom)==2
    assert {row[1] for row in centres['rows']}=={'Cu','Pb'}
    assert all(row[2]==1 and row[4:]==[2.5,.5] for row in centres['rows'])
    assert next(row[3] for row in centres['rows'] if row[1]=='Cu')==0
    assert all(g.equals(Point(2.5,.5)) for g in native.geom)
    assert any(f['sample_count']==0 and all(f['elements'][n]['maximum_sample_record_ids']==[] for n in f['members']) for f in r['features'])
    # Duplicate physical samples at the same maximum remain separate observations.
    original=Reader(s['file']);features=[{'type':'Feature','id':str(i),'geometry':mapping(Point(2.5,.5)),
        'properties':{str(c):original.data[c].iloc[0].item() if hasattr(original.data[c].iloc[0],'item') else original.data[c].iloc[0] for c in original.data.columns}} for i in (1,2)]
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},{'name':'ties.wt','crs':FRAME,'map_scale':1000},'ties')
    ties=bridge.apply('ties');_,_,r=run(bridge,{'file':ties['output'],'expected_sha256':ties['candidate_sha256']},e,p|{'concentration_centres':'all_observed_maxima'},t,'all-ties')
    assert len(r['concentration_centres']['rows'])==4
    assert {row[2] for row in r['concentration_centres']['rows']}=={1,2}
