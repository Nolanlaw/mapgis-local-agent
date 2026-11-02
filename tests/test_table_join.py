import asyncio
import json
from pathlib import Path

import pytest
from openpyxl import Workbook,load_workbook
from shapely.geometry import Point,LineString,box,mapping

from mapgis_agent.core import Bridge,BridgeError,native_ids,read_native,sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import prepare_native_operation,native_file_info
from test_native_raster import native_settings


def fixture(tmp_path,suffix='wt',rows=None):
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    geometries={'wt':[Point(i*10,0) for i in range(3)],
                'wl':[LineString([(i*10,0),(i*10+5,5)]) for i in range(3)],
                'wp':[box(i*10,0,i*10+10,10) for i in range(3)]}[suffix]
    fc={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i+1),'geometry':mapping(g),
         'properties':{'ID':[9,9,12][i],'原字段':'保留'}} for i,g in enumerate(geometries)]}
    template={'name':'input.'+suffix,'map_scale':1000,'crs':{'kind':'local','name':'join fixture','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}}
    prepare_creation(bridge,fc,template,'source');source=Path(bridge.apply('source')['output'])
    book=Workbook();sheet=book.active;sheet.title='采样表';sheet.append(['样号','W','说明'])
    for row in rows or [[9,1.25,'甲'],[12,7.5,'乙']]:sheet.append(row)
    table=tmp_path/'chemistry.xlsx';book.save(table);book.close()
    return bridge,source,table


def parameters():return {'native_key':'ID','table_key':'样号','key_mode':'numeric','unmatched':'error',
    'columns':[{'source':'W','name':'化探值','type':'double'},{'source':'说明','name':'样品说明','type':'string','length':20}]}


@pytest.mark.parametrize('suffix',['wt','wl','wp'])
def test_join_preserves_duplicate_native_keys_and_all_nonattribute_bytes_then_native_reopens(tmp_path,suffix):
    from mapgis_agent.table_join import prepare_table_join
    bridge,source,table=fixture(tmp_path,suffix);before=source.read_bytes();table_hash=sha(table.read_bytes())
    prepared=prepare_table_join(bridge,str(source),sha(before),{'file':str(table),'expected_sha256':table_hash,'sheet':'采样表'},parameters(),'join')
    saved=bridge.apply('join');after,reader,sections=read_native(Path(saved['output']))
    assert reader.data['ID'].tolist()==[9,9,12] and reader.data['化探值'].tolist()==[1.25,1.25,7.5]
    assert reader.data['样品说明'].tolist()==['甲','甲','乙'] and reader.data['原字段'].tolist()==['保留']*3
    assert native_ids(reader)==[1,2,3]
    from pymapgis import reader as native
    _,old,old_sections=read_native(source);attribute_index=native._ATTR_HEADER_INDEX[old.shapeType]
    assert all(before[a:a+n]==after[b:b+m] for i,((a,n),(b,m)) in enumerate(zip(old_sections,sections)) if i!=attribute_index)
    assert prepared['metrics']['matched_features']==3 and prepared['non_attribute_sections_byte_identical']
    prepare_native_operation(bridge,'resave',saved['output'],sha(after),{},'native-save')
    reopened=bridge.apply('native-save');fresh=native_file_info(bridge.root,reopened['output'],reopened['candidate_sha256'])
    assert fresh['active']==3
    assert source.read_bytes()==before and sha(table.read_bytes())==table_hash


def test_join_requires_explicit_null_and_missing_replacements_and_rejects_duplicate_table_keys(tmp_path):
    from mapgis_agent.table_join import prepare_table_join
    bridge,source,table=fixture(tmp_path,rows=[[9,None,None]])
    binding={'file':str(table),'expected_sha256':sha(table.read_bytes()),'sheet':'采样表'}
    with pytest.raises(BridgeError,match='JOIN_MISSING_KEYS'):
        prepare_table_join(bridge,str(source),sha(source.read_bytes()),binding,parameters(),'missing')
    options=parameters();options['unmatched']='fill'
    options['columns'][0].update(missing_value=-999,null_value=-1)
    options['columns'][1].update(missing_value='缺样',null_value='未测')
    prepared=prepare_table_join(bridge,str(source),sha(source.read_bytes()),binding,options,'filled')
    reader=read_native(Path(bridge.apply('filled')['output']))[1]
    assert reader.data['化探值'].tolist()==[-1,-1,-999]
    assert reader.data['样品说明'].tolist()==['未测','未测','缺样']
    assert prepared['metrics']['unmatched_features']==1
    book=load_workbook(table);book.active.append([9,2,'重复']);book.save(table);book.close()
    binding['expected_sha256']=sha(table.read_bytes())
    with pytest.raises(BridgeError,match='JOIN_DUPLICATE_TABLE_KEY'):
        prepare_table_join(bridge,str(source),sha(source.read_bytes()),binding,options,'duplicate')


def test_join_publication_is_guarded_by_the_original_workbook_hash(tmp_path):
    from mapgis_agent.table_join import prepare_table_join
    bridge,source,table=fixture(tmp_path)
    prepare_table_join(bridge,str(source),sha(source.read_bytes()),{'file':str(table),'expected_sha256':sha(table.read_bytes()),'sheet':'采样表'},parameters(),'guard')
    book=load_workbook(table);book.active['B2']=88;book.save(table);book.close()
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('guard')
    assert not Path(bridge.status('guard')['output']).exists()


def test_join_tool_has_typed_nested_schema_and_real_mcp_call(tmp_path):
    from mapgis_agent.server import build_server
    bridge,source,table=fixture(tmp_path)
    async def scenario():
        server=build_server(tmp_path);tools=await server.list_tools()
        tool=next(t for t in tools if t.name=='mapgis_prepare_table_join')
        assert tool.annotations.destructiveHint is False and tool.annotations.idempotentHint is True
        assert 'JoinParameters' in tool.inputSchema['$defs']
        assert tool.inputSchema['$defs']['JoinParameters']['additionalProperties'] is False
        reply=await server.call_tool(tool.name,{'file':str(source),'expected_sha256':sha(source.read_bytes()),
            'table':{'file':str(table),'expected_sha256':sha(table.read_bytes()),'sheet':'采样表'},'parameters':parameters(),'request_id':'mcp-join'})
        result=json.loads(''.join(c.text for c in reply if c.type=='text'))
        assert result['state']=='PREPARED' and result['metrics']['matched_features']==3
    asyncio.run(scenario())
