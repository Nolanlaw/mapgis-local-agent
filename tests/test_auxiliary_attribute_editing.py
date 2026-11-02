"""The same schema tool must create/edit/read WP auxiliary tables via physical IDs."""
from pathlib import Path
import pytest
from mapgis_agent.core import BridgeError,read_native
from mapgis_agent.attributes import prepare_attribute_schema
from mapgis_agent.native_backend import prepare_native_operation
from mapgis_agent.raster import file_sha
from mapgis_agent.topology import read_wp_arcs,_wp_arc_records,_wp_nodes
from test_native_wp import wp_case,enable_native
from test_native_subset_auxiliary import populated_auxiliary,table_records


@pytest.mark.parametrize('table,index,key,identity_key',[('arc',2,'arcs','arc_id'),('node',5,'nodes','node_id')])
@pytest.mark.parametrize('existing',[False,True])
def test_auxiliary_schema_create_rename_convert_and_update_physical_records_then_native_reopen(wp_case,table,index,key,identity_key,existing):
    bridge,source=wp_case;enable_native(bridge)
    if existing:populated_auxiliary(source)
    before,_,sections=read_native(source)
    fields=[{'name':'编号','source':'ID'}] if existing else [{'name':'编号','type':'int','default':7}]
    fields += [{'name':'说明','source':'备注','type':'string','length':20},{'name':'值','source':'数值','type':'double'}] if existing else [
        {'name':'说明','type':'string','length':20,'default':'未赋值'},{'name':'值','type':'double','default':.125}]
    fields += [{'name':'采样日','type':'date','default':'2026-10-30'},{'name':'时刻','type':'time','default':'08:09:10.125'}]
    updates={'1':{'编号':7,'说明':'第一处','值':-3.5},'2':{'编号':7,'说明':'第二处','值':9.25}}
    prepared=prepare_attribute_schema(bridge,str(source),file_sha(source),fields,'schema',table=table,attribute_updates=updates)
    target=Path(bridge.apply('schema')['output']);after,_,new_sections=read_native(target)
    assert prepared['attribute_table']==table and prepared['non_attribute_sections_byte_identical']
    for i,((a,n),(b,m)) in enumerate(zip(sections,new_sections)):
        if i!=index:assert before[a:a+n]==after[b:b+m]
    report=read_wp_arcs(str(target),file_sha(target),limit=100)
    rows={r[identity_key]:r['attributes'] for r in report[key]}
    assert rows[1]['说明']=='第一处' and rows[2]['说明']=='第二处'
    assert rows[1]['值']==-3.5 and rows[2]['值']==9.25 and rows[1]['编号']==rows[2]['编号']==7
    assert rows[1]['采样日']=='2026-10-30' and rows[1]['时刻']=='08:09:10.125000'
    assert report['attribute_tables'][table]['fields']==['编号','说明','值','采样日','时刻']
    prepare_native_operation(bridge,'resave',str(target),file_sha(target),{},'native')
    saved=Path(bridge.apply('native')['output']);saved_rows=read_wp_arcs(str(saved),file_sha(saved),limit=100)[key]
    assert {r[identity_key]:r['attributes'] for r in saved_rows}==rows
    assert source.read_bytes()==before
    # Duplicate attribute numbers cannot redirect the update away from physical slot2.
    assert rows[1]['说明']!=rows[2]['说明']


@pytest.mark.parametrize('updates',[{'999':{'x':1}},{'01':{'x':1}},{'1':{'absent':1}},{'1':{'x':1.5}},[]])
def test_auxiliary_attribute_updates_reject_nonphysical_slots_unknown_fields_and_loss(wp_case,updates):
    bridge,source=wp_case;before=source.read_bytes()
    with pytest.raises(BridgeError,match='SCHEMA_|FIELD_CONVERSION'):
        prepare_attribute_schema(bridge,str(source),file_sha(source),[{'name':'x','type':'int','default':0}],'invalid',table='arc',attribute_updates=updates)
    assert source.read_bytes()==before


def test_schema_primary_updates_share_validation_and_keep_old_signature(wp_case):
    bridge,source=wp_case
    prepare_attribute_schema(bridge,str(source),file_sha(source),[{'name':'ID','source':'ID'},{'name':'count','type':'int','default':0}],
        'primary',attribute_updates={'2':{'count':12}})
    result=read_native(Path(bridge.apply('primary')['output']))[1]
    assert result.data['count'].tolist()==[0,12,0,0]


@pytest.mark.parametrize('section',[2,5])
def test_native_resave_validator_detects_changed_auxiliary_record_without_primary_difference(wp_case,section):
    from mapgis_agent.native_backend import _validate
    _,source=wp_case;populated_auxiliary(source)
    _,_,data,width=table_records(source,section);changed=bytearray(source.read_bytes());changed[data+width+1]^=1
    candidate=source.with_name('damaged.wp');candidate.write_bytes(changed)
    with pytest.raises(BridgeError,match='NATIVE_AUXILIARY_ATTRIBUTES_CHANGED'):_validate(source,candidate,'resave',{})
