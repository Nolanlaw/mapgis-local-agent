import datetime,json,os,sqlite3,struct
import subprocess,sys
from pathlib import Path

import pytest

from mapgis_agent.core import BridgeError
from mapgis_agent.database_sources import read_snapshot,source_references
from mapgis_agent.raster import file_sha


def sqlite_source(tmp_path):
    file=tmp_path/'original.sqlite'
    with sqlite3.connect(file) as con:
        con.execute('CREATE TABLE "样品" ("ID" TEXT PRIMARY KEY,"中文" TEXT,"GRADE" REAL,"TIME" TEXT,"RAW" BLOB,"BIG" INTEGER)')
        con.executemany('INSERT INTO "样品" VALUES (?,?,?,?,?,?)',[
            ('001','石英脉',1.25,'2026-11-02T12:34:56',b'\x00\xff',2**60+1),('002','',0.,None,None,-2**60-1),('003',None,None,'00:00:00',b'',0)])
    con.close()
    return {'file':str(file),'expected_sha256':file_sha(file),'format':'sqlite','table':'样品'}


def dbf_source(tmp_path):
    fields=[('ID','C',5,0),('NOTE','C',20,0),('GRADE','N',10,2),('DAY','D',8,0),('FLAG','L',1,0)]
    header=bytearray(32);header[0]=3;header[1:4]=bytes([126,11,2]);width=1+sum(f[2] for f in fields)
    struct.pack_into('<IHH',header,4,4,33+32*len(fields),width);descriptors=[]
    for name,kind,size,precision in fields:
        descriptor=bytearray(32);descriptor[:len(name)]=name.encode();descriptor[11]=ord(kind);descriptor[16:18]=bytes([size,precision]);descriptors.append(descriptor)
    rows=[b' '+b'001  '+'石英脉'.encode('gbk').ljust(20)+b'      1.25'+b'20261102'+b'T',
        b' '+b'002  '+b' '*20+b'*'*10+b'00000000'+b'?',
        b' '+b'003  '+b'\x00'*20+b'      0.00'+b' '*8+b'F',
        b'*'+b'004  '+b'deleted'.ljust(20)+b'      3.00'+b'20261101'+b'T']
    assert all(len(row)==width for row in rows)
    file=tmp_path/'original.dbf';file.write_bytes(bytes(header)+b''.join(descriptors)+b'\r'+b''.join(rows)+b'\x1a')
    return {'file':str(file),'expected_sha256':file_sha(file),'format':'dbf','table':None,'encoding':'gbk'}


def test_sqlite_snapshot_preserves_exact_nullable_large_integer_binary_and_empty_values(tmp_path):
    source=sqlite_source(tmp_path);copy,data,refs=read_snapshot(source,tmp_path/'copy')
    assert data['rows'][0]==['001','石英脉',1.25,'2026-11-02T12:34:56',b'\x00\xff',2**60+1]
    assert data['rows'][1]==['002','',0.,None,None,-2**60-1]
    assert data['rows'][2]==['003',None,None,'00:00:00',b'',0]
    assert file_sha(copy)==source['expected_sha256']==file_sha(Path(source['file']))
    assert len(refs)==4 and all(r['must_be_absent'] for r in refs[1:])


def test_sqlite_selected_columns_and_local_base_table_scope(tmp_path):
    source=sqlite_source(tmp_path);_,data,_=read_snapshot(source,tmp_path/'copy',['GRADE','ID'])
    assert [c['name'] for c in data['columns']]==['GRADE','ID'] and data['rows'][1]==[0.,'002']
    with pytest.raises(BridgeError,match='DATABASE_COLUMN'):read_snapshot(source,tmp_path/'unknown',['missing'])
    with sqlite3.connect(source['file']) as con:con.execute('CREATE VIEW v AS SELECT * FROM "样品"')
    con.close();source['expected_sha256']=file_sha(Path(source['file']))
    with pytest.raises(BridgeError,match='DATABASE_LOCAL_TABLE'):read_snapshot(source|{'table':'v'},tmp_path/'view')


def test_database_hash_and_offline_companions_are_verified_without_opening_source(tmp_path):
    source=sqlite_source(tmp_path)
    with pytest.raises(BridgeError,match='STALE_SOURCE'):read_snapshot(source|{'expected_sha256':'0'*64},tmp_path/'stale')
    wal=Path(source['file']+'-wal');wal.touch()
    with pytest.raises(BridgeError,match='STALE_SOURCE'):source_references(source)
    wal.unlink();assert file_sha(Path(source['file']))==source['expected_sha256']


def test_sqlite_nonfinite_and_large_cells_fail_with_bounded_reading(tmp_path):
    source=sqlite_source(tmp_path)
    with sqlite3.connect(source['file']) as con:con.execute('UPDATE "样品" SET GRADE=? WHERE ID=?',(float('inf'),'001'))
    con.close();source['expected_sha256']=file_sha(Path(source['file']))
    with pytest.raises(BridgeError,match='DATABASE_NONFINITE'):read_snapshot(source,tmp_path/'nonfinite')
    with sqlite3.connect(source['file']) as con:con.execute('UPDATE "样品" SET GRADE=1,RAW=zeroblob(4194305) WHERE ID="001"')
    con.close();source['expected_sha256']=file_sha(Path(source['file']))
    with pytest.raises(BridgeError,match='DATABASE_LIMIT'):read_snapshot(source,tmp_path/'oversize')


def test_dbf_physical_slots_null_markers_empty_string_zero_dates_and_logical(tmp_path):
    source=dbf_source(tmp_path);copy,data,_=read_snapshot(source,tmp_path/'copy')
    assert data['row_ids']==[1,2,3] and data['deleted_record_ids']==[4]
    assert data['rows']==[['001','石英脉','1.25','2026-11-02',True],['002','',None,None,None],['003',None,'0.00',None,False]]
    assert file_sha(copy)==source['expected_sha256']==file_sha(Path(source['file']))
    # Independent bundled GDAL reader: numeric, civil date, deleted-record count.
    import pyogrio
    frame=pyogrio.read_dataframe(copy,encoding='gbk',datetime_as_string=True)
    assert len(frame)==3 and frame['ID'].tolist()==['001','002','003'] and frame['GRADE'].iloc[0]==1.25 and frame['GRADE'].iloc[2]==0
    assert frame['NOTE'].iloc[0]=='石英脉' and str(frame['DAY'].iloc[0]).startswith('2026-11-02')


def test_dbf_declared_encoding_versions_and_invalid_layout_are_not_guessed(tmp_path):
    source=dbf_source(tmp_path)
    with pytest.raises(BridgeError,match='DATABASE_DBF_PARAMETERS'):read_snapshot(source|{'encoding':None},tmp_path/'encoding')
    original=Path(source['file']);raw=bytearray(original.read_bytes());raw[0]=0x30;original.write_bytes(raw)
    with pytest.raises(BridgeError,match='DATABASE_DBF_VERSION'):read_snapshot(source|{'expected_sha256':file_sha(original)},tmp_path/'version')


def access_source(tmp_path):
    import pythoncom,win32com.client
    file=tmp_path/'original.accdb';pythoncom.CoInitialize()
    try:
        catalog=win32com.client.Dispatch('ADOX.Catalog');catalog.Create(f'Provider=Microsoft.ACE.OLEDB.16.0;Data Source="{file}";')
        con=catalog.ActiveConnection;con.Execute('CREATE TABLE [Samples] ([ID] TEXT(20) PRIMARY KEY,[中文] TEXT(100),[GRADE] DOUBLE,[DAY] DATETIME,[FLAG] YESNO)')
        con.Execute("INSERT INTO [Samples] VALUES ('001','石英脉',1.25,#2026-11-02 12:34:56#,True)")
        con.Execute("INSERT INTO [Samples] ([ID],[中文],[GRADE]) VALUES ('002','',0)")
        con.Close();con=None;catalog=None
    finally:pythoncom.CoUninitialize()
    return {'file':str(file),'expected_sha256':file_sha(file),'format':'access','table':'Samples'}


@pytest.mark.skipif(os.name!='nt',reason='actual installed Windows ACE provider')
def test_actual_access_engine_read_uses_stored_civil_time_and_preserves_nulls(tmp_path):
    source=access_source(tmp_path);file=Path(source['file'])
    _,data,_=read_snapshot(source,tmp_path/'copy')
    lookup={row[0]:row for row in data['rows']}
    assert lookup['001']==['001','石英脉',1.25,'2026-11-02T12:34:56',True]
    assert lookup['002']==['002','',0.,None,False]
    assert file_sha(file)==source['expected_sha256'] and not file.with_suffix('.laccdb').exists()


@pytest.mark.skipif(os.name!='nt',reason='actual installed Windows ACE and minimal MCP child environment')
def test_access_works_in_the_real_mcp_minimal_environment_without_inheriting_host_secrets(tmp_path):
    from mcp.client.stdio import get_default_environment
    source=access_source(tmp_path);env=get_default_environment();env['OPENBLAS_NUM_THREADS']='1'
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
    assert 'CommonProgramFiles' not in env and 'COMMONPROGRAMFILES' not in env
    code="import json,sys;from pathlib import Path;from mapgis_agent.database_sources import read_snapshot;s=json.loads(sys.stdin.read());_,d,_=read_snapshot(s,Path(sys.argv[1]));print(json.dumps({'rows':len(d['rows']),'day':d['rows'][0][3]}))"
    result=subprocess.run([sys.executable,'-s','-B','-X','utf8','-c',code,str(tmp_path/'minimal')],input=json.dumps(source),env=env,
        capture_output=True,text=True,encoding='utf8',timeout=60)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'rows':2,'day':'2026-11-02T12:34:56'}
