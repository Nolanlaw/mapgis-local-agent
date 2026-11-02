"""Loaded-target native copies preserve existing slots and every source value."""
from pathlib import Path
import pytest
from shapely.geometry import Point,LineString,box,Polygon,mapping,shape

from mapgis_agent.core import Bridge,BridgeError,read_native,native_ids
from mapgis_agent.creation import prepare_creation
from mapgis_agent.features import read_features
from mapgis_agent.native_backend import prepare_native_operation,native_file_info
from mapgis_agent.native_com import prepare_native_subset
from mapgis_agent.raster import file_sha
from mapgis_agent.topology import validate_wp_topology,_wp_arc_records,_wp_nodes
from test_native_raster import native_settings
from test_native_subset_auxiliary import populated_auxiliary,table_records


def case(root,kind,projected=False,aux=False):
    bridge=Bridge(root);native_settings(root)
    if kind=='wp':
        outer=box(10,0,20,10);hole=box(14,2,16,4)
        geometries=[box(0,0,10,10),Polygon(outer.exterior.coords,[hole.exterior.coords]),hole,box(30,0,40,10)]
    elif kind=='wl':geometries=[LineString([(10*i,0),(10*i+5,3),(10*i+7,0)]) for i in range(4)]
    else:geometries=[Point(10*i,0) for i in range(4)]
    features=[]
    for i,g in enumerate(geometries,1):
        f={'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{'ID':7,'value':11*i,'name':'项'+str(i)}}
        if kind=='wt':
            f['style']=[{'height':3.,'width':2.},{'point_type':1,'symbol_no':1,'height':5.,'width':3.,'pen_width':.2},
                {'point_type':2,'radius_mm':2.,'filled':1},{'point_type':3,'radius_mm':3.,'start_angle':20.,'end_angle':130.}][i-1]
            if i==1:f['text']='中文T#-2#=d'
        features.append(f)
    frame=({'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'} if projected else
        {'kind':'local','name':'append native fixture','unit':'unknown','axis_order':'xy','reference_sha256':'0'*64})
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},{'name':'all.'+kind,'map_scale':50000 if projected else 1000,'crs':frame},'all')
    source=Path(bridge.apply('all')['output'])
    if aux:populated_auxiliary(source)
    prepare_native_subset(bridge,str(source),file_sha(source),[1,3],{'name':'base.'+kind},'base')
    base=Path(bridge.apply('base')['output'])
    return bridge,source,base


@pytest.mark.parametrize('kind',['wt','wl','wp'])
@pytest.mark.parametrize('projected',[False,True])
def test_native_append_keeps_old_physical_ids_and_copies_new_geometry_style_and_primary_rows(tmp_path,kind,projected):
    bridge,source,base=case(tmp_path,kind,projected);protected={f:file_sha(f) for f in [source,base]}
    receipt=prepare_native_subset(bridge,str(source),file_sha(source),[2,4],{'name':'merged.'+kind},'append',
        target={'file':str(base),'expected_sha256':file_sha(base)})
    output=bridge.apply('append');rows=read_features(output['output'],file_sha(Path(output['output'])))['collection']['features']
    original=read_features(str(source),file_sha(source))['collection']['features'];existing=read_features(str(base),file_sha(base))['collection']['features']
    assert [f['properties']['value'] for f in rows]==[11,33,22,44] and [f['id'] for f in rows]==['1','2','3','4']
    for actual,expected in zip(rows,[original[0],original[2],original[1],original[3]]):
        assert actual['properties']==expected['properties'] and shape(actual['geometry']).equals(shape(expected['geometry']))
        assert actual.get('text')==expected.get('text')
    for actual,expected in zip(rows[:2],existing):
        assert actual['id']==expected['id']
        # WP field5/6/7 are storage offsets, not graphic parameters. Growing
        # the node prefix relocates walks; topology validation checks them.
        styles=[{k:v for k,v in f['style'].items() if kind!='wp' or k not in {'field5','field6','field7'}} for f in (actual,expected)]
        assert styles[0]==styles[1]
    assert native_file_info(tmp_path,output['output'],file_sha(Path(output['output'])))['active']==4
    assert receipt['verification']['target_record_ids_preserved']
    assert receipt['native_execution']['pid']!=receipt['native_reopen']['pid']
    if kind=='wp':validate_wp_topology(Path(output['output']))
    assert all(file_sha(f)==value for f,value in protected.items())


def test_native_wp_append_retains_both_sources_nonempty_arc_and_node_attributes(tmp_path):
    bridge,source,base=case(tmp_path,'wp',aux=True)
    receipt=prepare_native_subset(bridge,str(source),file_sha(source),[2,4],{'name':'merged.wp'},'append',
        target={'file':str(base),'expected_sha256':file_sha(base)})
    output=Path(bridge.apply('append')['output']);aux=receipt['verification']['polygon_subordinate_records']
    assert aux['attribute_records_byte_identical']
    for index,key in [(2,'arc_mapping'),(5,'node_mapping')]:
        _,new,_,_=table_records(output,index)
        for pair in aux[key]:
            old_file=base if pair['source_role']=='target' else source
            _,old,_,_=table_records(old_file,index)
            assert old[pair['source_record_id']]==new[pair['output_record_id']]
            if pair['source_role']=='target':assert pair['output_record_id']==pair['source_record_id']
    validate_wp_topology(output)


def test_append_to_a_file_with_deleted_slots_keeps_remaining_identity(tmp_path):
    bridge,source,base=case(tmp_path,'wt')
    prepare_native_operation(bridge,'feature.delete',str(base),file_sha(base),{'record_ids':[1]},'delete')
    deleted=Path(bridge.apply('delete')['output']);before=deleted.read_bytes()
    receipt=prepare_native_subset(bridge,str(source),file_sha(source),[2],{'name':'merged.wt'},'append',
        target={'file':str(deleted),'expected_sha256':file_sha(deleted)})
    result=bridge.apply('append');rows=read_features(result['output'],result['candidate_sha256'])['collection']['features']
    assert next(f for f in rows if f['id']=='2')['properties']['value']==33
    assert len(rows)==2 and sorted(f['properties']['value'] for f in rows)==[22,33]
    assert deleted.read_bytes()==before


def test_append_source_and_target_hash_guards_continue_through_publication(tmp_path):
    bridge,source,base=case(tmp_path,'wl')
    receipt=prepare_native_subset(bridge,str(source),file_sha(source),[2],{'name':'merged.wl'},'append',
        target={'file':str(base),'expected_sha256':file_sha(base)})
    base.write_bytes(base.read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('append')
    assert not Path(receipt['output']).exists()


def test_append_refuses_implicit_transform_or_incompatible_attribute_schema(tmp_path):
    from mapgis_agent.attributes import prepare_attribute_schema
    bridge,source,base=case(tmp_path,'wl')
    with pytest.raises(BridgeError,match='APPEND_TRANSFORM'):
        prepare_native_subset(bridge,str(source),file_sha(source),[2],{'name':'merged.wl'},'transform',transform={},
            target={'file':str(base),'expected_sha256':file_sha(base)})
    fields=[{'name':name,'source':name} for name in read_native(source)[1].data.columns]+[{'name':'extra','type':'double','default':0.}]
    prepare_attribute_schema(bridge,str(source),file_sha(source),fields,'new-schema')
    changed=Path(bridge.apply('new-schema')['output'])
    with pytest.raises(BridgeError,match='APPEND_SCHEMA'):
        prepare_native_subset(bridge,str(changed),file_sha(changed),[2],{'name':'merged.wl'},'mismatch',
            target={'file':str(base),'expected_sha256':file_sha(base)})


@pytest.mark.parametrize('which',['target','source','both','empty'])
def test_wp_append_after_native_deletion_preserves_remaining_physical_topology_and_auxiliary_rows(tmp_path,which):
    bridge,source,base=case(tmp_path,'wp',aux=True)
    for role,file,removed in [('target',base,[1,2] if which=='empty' else [1]),('source',source,[1,3])]:
        if role not in ({'target'} if which=='empty' else {'target','source'} if which=='both' else {which}):continue
        identity='delete-'+role
        prepare_native_operation(bridge,'feature.delete',str(file),file_sha(file),{'record_ids':removed},identity)
        output=Path(bridge.apply(identity)['output'])
        if role=='target':base=output
        else:source=output
    old_ids=native_ids(read_native(base)[1]);protected={f:file_sha(f) for f in [source,base]}
    receipt=prepare_native_subset(bridge,str(source),file_sha(source),[2,4],{'name':'merged.wp'},'append',
        target={'file':str(base),'expected_sha256':file_sha(base)})
    result=Path(bridge.apply('append')['output']);ids=native_ids(read_native(result)[1]);assert set(old_ids)<=set(ids)
    assert receipt['verification']['target_record_ids']==old_ids
    assert receipt['verification']['polygon_subordinate_records']['attribute_records_byte_identical']
    assert len(ids)==len(old_ids)+2
    rows=read_features(str(result),file_sha(result))['collection']['features']
    assert sorted(f['properties']['value'] for f in rows)==sorted(([33] if old_ids==[2] else [11,33] if old_ids else [])+[22,44])
    validate_wp_topology(result);assert all(file_sha(f)==value for f,value in protected.items())
