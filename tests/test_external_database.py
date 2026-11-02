import json,sqlite3,os
from pathlib import Path
import pytest
from pymapgis import Reader
from pymapgis.reader import _ATTR_HEADER_INDEX

from mapgis_agent.core import Bridge,BridgeError,read_native,native_ids
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha
from mapgis_agent.external_database import prepare_database_join,prepare_database_writeback,database_info
from test_database_sources import sqlite_source,dbf_source,access_source

FRAME={'kind':'local','name':'explicit database join test','unit':'metre','axis_order':'xy','reference_sha256':'d'*64}


def native_source(tmp_path):
    bridge=Bridge(tmp_path);features=[]
    for i,key in enumerate(['001','002','003','004','001'],1):
        features.append({'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':[10.*i,0.]},
            'properties':{'KEY':key,'ORIGINAL':f'原样{i}'},'style':{'point_type':2,'radius_mm':2.,'filled':1,'color':3,'border_color':3,'pen_width':.2}})
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},{'name':'original.wt','crs':FRAME,'map_scale':1000},'native')
    value=bridge.apply('native');return bridge,Path(value['output'])


def join_options(dbf=False):
    fields=[('NOTE' if dbf else '中文','NOTE','string','',40,'identity'),('GRADE','GRADE','double',0.,None,'identity'),
        ('DAY' if dbf else 'TIME','TM','string','',80,'identity')]
    if not dbf:fields.extend([('RAW','BIN','string','',128,'hex'),('BIG','BIG','string','',40,'identity')])
    return {'native_key':'KEY','table_key':'ID','key_mode':'text','unmatched':'fill','columns':[
        {'source':source,'name':name,'type':kind,'missing_value':missing,'null_value':missing,'null_flag':name+'_NULL','value_mode':mode,
            **({'length':width} if width else {})} for source,name,kind,missing,width,mode in fields]}


def test_sqlite_join_preserves_source_sections_and_null_empty_zero_big_and_binary(tmp_path):
    bridge,native=native_source(tmp_path);source=sqlite_source(tmp_path);before,reader,sections=read_native(native)
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(),'join');output=bridge.apply('join');file=Path(output['output'])
    after,new,new_sections=read_native(file);assert native_ids(reader)==native_ids(new)
    for i,((a,n),(b,m)) in enumerate(zip(sections,new_sections)):
        if i!=_ATTR_HEADER_INDEX['POINT']:assert before[a:a+n]==after[b:b+m]
    assert new.data['GRADE'].tolist()==[1.25,0.,0.,0.,1.25]
    assert new.data['GRADE_NULL'].tolist()==[0,0,1,-1,0]
    assert new.data['NOTE_NULL'].tolist()==[0,0,1,-1,0]
    assert new.data['NOTE'].tolist()==['石英脉','','','','石英脉']
    assert new.data['BIN'].tolist()==['00ff','','','','00ff'] and new.data['BIN_NULL'].tolist()==[0,1,0,-1,0]
    assert new.data['BIG'].iloc[0]==str(2**60+1)
    assert file_sha(native)==file_sha(Path(bridge.status('native')['output'])) and file_sha(Path(source['file']))==source['expected_sha256']
    assert prepare_database_join(bridge,str(native),file_sha(native),source,join_options(),'join')['state']=='APPLIED'


def test_database_nulls_require_a_separate_native_state_field(tmp_path):
    bridge,native=native_source(tmp_path);source=sqlite_source(tmp_path);options=join_options();options['columns'][1].pop('null_flag')
    with pytest.raises(BridgeError,match='DATABASE_NULL_FLAG_REQUIRED'):prepare_database_join(bridge,str(native),file_sha(native),source,options,'unsafe-null')


@pytest.mark.parametrize('format',['sqlite','dbf'])
def test_info_decodes_only_requested_preview_and_reports_true_total_count(tmp_path,format):
    source=sqlite_source(tmp_path) if format=='sqlite' else dbf_source(tmp_path);bridge=Bridge(tmp_path)
    info=database_info(bridge,source,1);assert info['rows']==3 and len(info['preview'])==1
    info=database_info(bridge,source,0);assert info['rows']==3 and info['preview']==[]
    if format=='sqlite':
        with sqlite3.connect(source['file']) as con:con.execute('UPDATE "样品" SET GRADE=? WHERE ID="003"',(float('inf'),))
        con.close();source['expected_sha256']=file_sha(Path(source['file']))
        assert database_info(bridge,source,1)['rows']==3
        from mapgis_agent.database_sources import read_snapshot
        with pytest.raises(BridgeError,match='DATABASE_NONFINITE'):read_snapshot(source,tmp_path/'complete')


def test_database_double_does_not_roundtrip_through_xlsx_serialization(tmp_path):
    bridge,native=native_source(tmp_path);source=sqlite_source(tmp_path);value=1.2345678901234567
    with sqlite3.connect(source['file']) as con:con.execute('UPDATE "样品" SET GRADE=? WHERE ID="001"',(value,))
    con.close();source['expected_sha256']=file_sha(Path(source['file']))
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(),'join');output=bridge.apply('join')
    assert float(Reader(output['output']).data['GRADE'].iloc[0]).hex()==value.hex()


def test_writeback_to_sqlite_copy_restores_nulls_and_keeps_unrelated_rows_and_objects(tmp_path):
    bridge,native=native_source(tmp_path);source=sqlite_source(tmp_path)
    with sqlite3.connect(source['file']) as con:con.execute('CREATE TABLE unrelated (n TEXT)');con.execute("INSERT INTO unrelated VALUES ('原样保留')")
    con.close();source['expected_sha256']=file_sha(Path(source['file']))
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(),'join');joined=bridge.apply('join')
    bridge.prepare(joined['output'],joined['candidate_sha256'],[1,5],'edit',attribute_updates={'1':{'GRADE':2.5,'NOTE':'改变'},'5':{'GRADE':2.5,'NOTE':'改变'}})
    edited=bridge.apply('edit');parameters={'native_key':'KEY','table_key':'ID','key_mode':'text','unmatched':'ignore','native_duplicates':'identical',
        'columns':[{'native':'GRADE','database':'GRADE','null_flag':'GRADE_NULL'},{'native':'NOTE','database':'中文','null_flag':'NOTE_NULL'}]}
    prepare_database_writeback(bridge,edited['output'],edited['candidate_sha256'],source,parameters,'writeback');output=bridge.apply('writeback')
    with sqlite3.connect(output['output']) as con:
        rows=con.execute('SELECT ID,"中文",GRADE,BIG,RAW FROM "样品" ORDER BY ID').fetchall();assert con.execute('SELECT * FROM unrelated').fetchall()==[('原样保留',)]
    con.close();assert rows==[('001','改变',2.5,2**60+1,b'\x00\xff'),('002','',0.,-2**60-1,None),('003',None,None,0,b'')]
    assert file_sha(Path(source['file']))==source['expected_sha256']


def test_dbf_join_retains_deleted_slots_and_explicit_null_byte_policies(tmp_path):
    bridge,native=native_source(tmp_path);source=dbf_source(tmp_path)
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(True),'join');output=bridge.apply('join');reader=Reader(output['output'])
    assert reader.data['GRADE_NULL'].tolist()==[0,1,0,-1,0]
    assert reader.data['NOTE_NULL'].tolist()==[0,0,1,-1,0]
    assert reader.data['GRADE'].tolist()==[1.25,0,0,0,1.25]


def test_dbf_writeback_is_exact_and_keeps_header_deleted_slots_and_other_fields(tmp_path):
    from mapgis_agent.database_sources import read_snapshot
    bridge,native=native_source(tmp_path);source=dbf_source(tmp_path)
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(True),'join');joined=bridge.apply('join')
    bridge.prepare(joined['output'],joined['candidate_sha256'],[1,5],'edit',attribute_updates={'1':{'GRADE':2.5},'5':{'GRADE':2.5}});edited=bridge.apply('edit')
    params={'native_key':'KEY','table_key':'ID','key_mode':'text','unmatched':'ignore','native_duplicates':'identical',
        'columns':[{'native':'GRADE','database':'GRADE','null_flag':'GRADE_NULL'}]}
    prepare_database_writeback(bridge,edited['output'],edited['candidate_sha256'],source,params,'write');output=bridge.apply('write')
    _,data,_=read_snapshot(source|{'file':output['output'],'expected_sha256':output['candidate_sha256']},tmp_path/'result-snapshot')
    assert data['rows'][0][2]=='2.50' and data['rows'][1][2] is None and data['rows'][2][2]=='0.00' and data['deleted_record_ids']==[4]
    before=Path(source['file']).read_bytes();after=Path(output['output']).read_bytes();assert len(before)==len(after)
    fields=5;header=33+fields*32;row_width=45;grade_start=1+5+20
    edited_positions={header+row*row_width+grade_start+i for row in range(3) for i in range(10)}
    assert all(a==b for i,(a,b) in enumerate(zip(before,after)) if i not in edited_positions)
    assert file_sha(Path(source['file']))==source['expected_sha256']


def test_dbf_precision_loss_leaves_task_copy_unchanged(tmp_path):
    bridge,native=native_source(tmp_path);source=dbf_source(tmp_path)
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(True),'join');joined=bridge.apply('join')
    bridge.prepare(joined['output'],joined['candidate_sha256'],[1,5],'edit',attribute_updates={'1':{'GRADE':1.234},'5':{'GRADE':1.234}});edited=bridge.apply('edit')
    params={'native_key':'KEY','table_key':'ID','key_mode':'text','unmatched':'ignore','native_duplicates':'identical','columns':[{'native':'GRADE','database':'GRADE','null_flag':'GRADE_NULL'}]}
    with pytest.raises(BridgeError,match='DATABASE_DBF_CONVERSION'):prepare_database_writeback(bridge,edited['output'],edited['candidate_sha256'],source,params,'bad-write')
    snapshot=Path(bridge.status('bad-write')['stage'])/'database-snapshot/database.dbf'
    assert file_sha(snapshot)==source['expected_sha256'] and not Path(bridge.status('bad-write')['output']).exists()


def test_sqlite_constraint_failure_rolls_back_an_earlier_row_in_the_same_batch(tmp_path):
    bridge,native=native_source(tmp_path);source=sqlite_source(tmp_path)
    with sqlite3.connect(source['file']) as con:con.execute("CREATE TRIGGER grade_limit BEFORE UPDATE OF GRADE ON '样品' WHEN NEW.GRADE>50 BEGIN SELECT RAISE(ABORT,'grade limit'); END")
    con.close();source['expected_sha256']=file_sha(Path(source['file']))
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(),'join');joined=bridge.apply('join')
    bridge.prepare(joined['output'],joined['candidate_sha256'],[1,2,5],'edit',attribute_updates={'1':{'GRADE':2.5},'2':{'GRADE':99.},'5':{'GRADE':2.5}});edited=bridge.apply('edit')
    params={'native_key':'KEY','table_key':'ID','key_mode':'text','unmatched':'ignore','native_duplicates':'identical','columns':[{'native':'GRADE','database':'GRADE','null_flag':'GRADE_NULL'}]}
    with pytest.raises(sqlite3.IntegrityError,match='grade limit'):prepare_database_writeback(bridge,edited['output'],edited['candidate_sha256'],source,params,'failed')
    copy=Path(bridge.status('failed')['stage'])/'database-snapshot/database.sqlite'
    with sqlite3.connect(copy) as con:assert con.execute('SELECT GRADE FROM "样品" WHERE ID="001"').fetchone()==(1.25,)
    con.close();assert file_sha(Path(source['file']))==source['expected_sha256'] and not Path(bridge.status('failed')['output']).exists()


def test_sqlite_trigger_cannot_write_an_unrelated_table(tmp_path):
    bridge,native=native_source(tmp_path);source=sqlite_source(tmp_path)
    with sqlite3.connect(source['file']) as con:
        con.execute('CREATE TABLE other (v INTEGER)');con.execute('INSERT INTO other VALUES (7)')
        con.execute('CREATE TRIGGER side_effect AFTER UPDATE OF GRADE ON "样品" BEGIN UPDATE other SET v=99; END')
    con.close();source['expected_sha256']=file_sha(Path(source['file']))
    prepare_database_join(bridge,str(native),file_sha(native),source,join_options(),'join');joined=bridge.apply('join')
    params={'native_key':'KEY','table_key':'ID','key_mode':'text','unmatched':'ignore','native_duplicates':'identical','columns':[{'native':'GRADE','database':'GRADE','null_flag':'GRADE_NULL'}]}
    with pytest.raises(sqlite3.DatabaseError,match='authoriz'):prepare_database_writeback(bridge,joined['output'],joined['candidate_sha256'],source,params,'failed')
    copy=Path(bridge.status('failed')['stage'])/'database-snapshot/database.sqlite'
    with sqlite3.connect(copy) as con:
        assert con.execute('SELECT * FROM other').fetchall()==[(7,)] and con.execute('SELECT GRADE FROM "样品" WHERE ID="001"').fetchone()==(1.25,)
    con.close()


@pytest.mark.skipif(os.name!='nt',reason='actual installed Windows ACE')
def test_access_join_and_isolated_table_writeback_preserve_civil_datetime(tmp_path):
    from mapgis_agent.database_sources import read_snapshot
    bridge,native=native_source(tmp_path);source=access_source(tmp_path);options=join_options();options['columns']=options['columns'][:3];options['columns'][2]['source']='DAY'
    prepare_database_join(bridge,str(native),file_sha(native),source,options,'join');joined=bridge.apply('join');reader=Reader(joined['output'])
    assert reader.data['TM'].iloc[0]=='2026-11-02T12:34:56' and reader.data['TM_NULL'].tolist()==[0,1,-1,-1,0]
    bridge.prepare(joined['output'],joined['candidate_sha256'],[1,5],'edit',attribute_updates={'1':{'TM':'2027-03-04T15:06:07'},'5':{'TM':'2027-03-04T15:06:07'}});edited=bridge.apply('edit')
    params={'native_key':'KEY','table_key':'ID','key_mode':'text','unmatched':'ignore','native_duplicates':'identical','access_output':'isolated_table_copy',
        'columns':[{'native':'TM','database':'DAY','null_flag':'TM_NULL','value_mode':'datetime_iso'}]}
    prepare_database_writeback(bridge,edited['output'],edited['candidate_sha256'],source,params,'write');output=bridge.apply('write')
    _,data,_=read_snapshot(source|{'file':output['output'],'expected_sha256':output['candidate_sha256']},tmp_path/'result-snapshot')
    lookup={row[0]:row for row in data['rows']};assert lookup['001']==['001','石英脉',1.25,'2027-03-04T15:06:07',True]
    assert lookup['002']==['002','',0.,None,False] and file_sha(Path(source['file']))==source['expected_sha256']
    report=json.loads(Path(output['output']).with_name('database-lineage.json').read_text(encoding='utf8'))
    assert 'only this table data' in report['output_scope']
