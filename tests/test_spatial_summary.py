"""Spatial join/aggregation must keep physical identities and original geometry."""
import json,math
from pathlib import Path
import pytest
from shapely.geometry import Point,LineString,box,mapping
from mapgis_agent.core import Bridge,BridgeError,read_native,sha,native_ids
from mapgis_agent.creation import prepare_creation
from test_spatial_query import LOCAL

def fixture(tmp_path):
    b=Bridge(tmp_path);results=[]
    for key,geometries,values,suffix in [('zones',[box(0,0,2,2),box(2,0,4,2),box(10,0,12,2)],[0,0,0],'wp'),
        ('samples',[Point(1,1),Point(2,1),Point(3,1),Point(99,99)],[1,3,5,100],'wt')]:
        features=[{'type':'Feature','id':str(i),'geometry':mapping(g),'properties':{'ID':7,'W':v,'label':'甲' if i%2 else '乙'}} for i,(g,v) in enumerate(zip(geometries,values),1)]
        prepare_creation(b,{'type':'FeatureCollection','features':features},{'name':key+'.'+suffix,'crs':LOCAL,'map_scale':1000},key)
        results.append(b.apply(key))
    return b,*results

def prepare(b,left,right,parameters,identity):
    from mapgis_agent.spatial_summary import prepare_spatial_summary
    return prepare_spatial_summary(b,{'file':left['output'],'expected_sha256':left['candidate_sha256']},
        {'file':right['output'],'expected_sha256':right['candidate_sha256']},parameters,identity)

def options():return {'predicate':'covers','crs':LOCAL,'match_policy':'all','nulls':'error','columns':[
    {'operation':'count','name':'样品数'}, {'operation':'mean','field':'W','name':'W均值','empty_value':-999},
    {'operation':'stddev','field':'W','name':'W标准差','empty_value':-999,'ddof':1},
    {'operation':'concat_unique','field':'label','name':'样品标签','length':20,'empty_value':'无样'}]}

def test_zonal_statistics_treat_boundaries_and_duplicate_ids_explicitly_and_keep_other_bytes(tmp_path):
    b,zones,samples=fixture(tmp_path);before=Path(zones['output']).read_bytes()
    prepared=prepare(b,zones,samples,options(),'stats');result=b.apply('stats')
    after,reader,sections=read_native(Path(result['output']));_,original,old_sections=read_native(Path(zones['output']))
    assert reader.data['ID'].tolist()==[7,7,7] and native_ids(reader)==[1,2,3]
    assert reader.data['样品数'].tolist()==[2,2,0] and reader.data['W均值'].tolist()==[2,4,-999]
    assert reader.data['W标准差'].tolist()==pytest.approx([math.sqrt(2),math.sqrt(2),-999])
    assert reader.data['样品标签'].tolist()==['乙;甲','乙;甲','无样']
    from pymapgis import reader as native
    attribute=native._ATTR_HEADER_INDEX[reader.shapeType]
    assert all(before[a:a+n]==after[c:c+m] for i,((a,n),(c,m)) in enumerate(zip(old_sections,sections)) if i!=attribute)
    assert prepared['metrics']['multiply_matched_right_records']==1 and prepared['metrics']['unmatched_right_records']==1
    assert Path(zones['output']).read_bytes()==before
    provenance=json.loads((Path(result['output']).parent/(Path(result['output']).name+'.spatial.json')).read_text(encoding='utf8'))
    assert provenance['matches']==[{'left_record_id':1,'right_record_ids':[1,2]},{'left_record_id':2,'right_record_ids':[2,3]},{'left_record_id':3,'right_record_ids':[]}]

def test_unique_ownership_and_interior_boundary_policy_are_not_silently_guessed(tmp_path):
    b,zones,samples=fixture(tmp_path)
    with pytest.raises(BridgeError,match='SPATIAL_MULTIPLE_OWNERS'):
        prepare(b,zones,samples,options()|{'match_policy':'unique'},'ambiguous')
    prepared=prepare(b,zones,samples,options()|{'predicate':'contains','match_policy':'unique'},'interiors')
    result=b.apply('interiors');reader=read_native(Path(result['output']))[1]
    assert reader.data['样品数'].tolist()==[1,1,0]
    assert reader.data['W标准差'].tolist()==[-999]*3
    assert prepared['metrics']['unmatched_right_records']==2

def test_empty_single_numeric_and_field_collisions_have_actionable_errors(tmp_path):
    b,zones,samples=fixture(tmp_path)
    with pytest.raises(BridgeError,match='SPATIAL_EMPTY_VALUE'):
        prepare(b,zones,samples,options()|{'columns':[{'operation':'mean','field':'W','name':'mean'}]},'empty')
    with pytest.raises(BridgeError,match='SPATIAL_SINGLE_MATCH'):
        prepare(b,zones,samples,options()|{'columns':[{'operation':'single','field':'W','name':'single','empty_value':0}]},'many')
    with pytest.raises(BridgeError,match='SPATIAL_TARGET_EXISTS'):
        prepare(b,zones,samples,options()|{'columns':[{'operation':'count','name':'ID'}]},'collision')

def test_both_inputs_are_guarded_through_publication(tmp_path):
    b,zones,samples=fixture(tmp_path)
    prepare(b,zones,samples,options(),'stale')
    Path(samples['output']).write_bytes(Path(samples['output']).read_bytes()+b'changed')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('stale')

@pytest.mark.parametrize('suffix,geometry',[('wt',Point(1,1)),('wl',LineString([(0,1),(4,1)]))])
def test_spatial_join_also_updates_point_and_line_targets_without_rebuilding(tmp_path,suffix,geometry):
    b,zones,samples=fixture(tmp_path)
    prepare_creation(b,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':mapping(geometry),'properties':{'ID':7}}]},
        {'name':'target.'+suffix,'crs':LOCAL,'map_scale':1000},'target');target=b.apply('target')
    prepare(b,target,zones,{'predicate':'intersects','crs':LOCAL,'match_policy':'all','columns':[{'operation':'count','name':'matches'}]},'joined')
    result=b.apply('joined');reader=read_native(Path(result['output']))[1]
    assert reader.geom[0].equals(geometry) and reader.data['matches'].tolist()==[1 if suffix=='wt' else 2]


def test_null_and_sample_population_rules_do_not_invent_values():
    from mapgis_agent.spatial_summary import SummaryColumn,_value
    column=SummaryColumn(operation='mean',field='W',name='mean',empty_value=-999)
    with pytest.raises(BridgeError,match='SPATIAL_NULL_VALUE'):_value(column,[1,None],'error')
    assert _value(column,[1,None],'skip')==1 and _value(column,[None],'skip')==-999
    sample=SummaryColumn(operation='stddev',field='W',name='sample',ddof=1,empty_value=-1)
    population=SummaryColumn(operation='stddev',field='W',name='population',ddof=0,empty_value=-1)
    assert _value(sample,[1,3],'error')==pytest.approx(math.sqrt(2))
    assert _value(population,[1,3],'error')==1 and _value(sample,[1],'error')==-1 and _value(population,[1],'error')==0


def test_public_mcp_spatial_summary_has_strict_schema_and_native_reopens(tmp_path):
    import asyncio
    from mapgis_agent.server import build_server
    from mapgis_agent.native_backend import prepare_native_operation,native_file_info
    from test_native_raster import native_settings
    b,zones,samples=fixture(tmp_path);native_settings(tmp_path)
    async def scenario():
        server=build_server(tmp_path);tool=next(t for t in await server.list_tools() if t.name=='mapgis_prepare_spatial_summary')
        assert tool.annotations.idempotentHint and not tool.annotations.destructiveHint
        for name in ['SpatialSource','SummaryParameters','SummaryColumn']:assert tool.inputSchema['$defs'][name]['additionalProperties'] is False
        response=await server.call_tool(tool.name,{'left':{'file':zones['output'],'expected_sha256':zones['candidate_sha256']},
            'right':{'file':samples['output'],'expected_sha256':samples['candidate_sha256']},'parameters':options(),'request_id':'mcp-stats'})
        result=json.loads(''.join(c.text for c in response if c.type=='text'));assert result['metrics']['pairs']==4
    asyncio.run(scenario());result=b.apply('mcp-stats')
    prepare_native_operation(b,'resave',result['output'],result['candidate_sha256'],{},'native-save');saved=b.apply('native-save')
    info=native_file_info(tmp_path,saved['output'],saved['candidate_sha256']);assert info['active']==3
    reader=read_native(Path(saved['output']))[1];assert reader.data['样品数'].tolist()==[2,2,0]
