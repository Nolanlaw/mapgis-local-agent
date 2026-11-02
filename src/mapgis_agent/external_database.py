"""Native left joins and explicit task-copy writeback over offline database tables."""
from __future__ import annotations

from contextlib import closing
import datetime as dt
from decimal import Decimal
import hashlib
import json,math,shutil,sqlite3,tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .attributes import prepare_attribute_schema
from .core import Bridge,BridgeError,json_text,native_ids,read_native,scalar
from .database_sources import DatabaseSource,read_snapshot,source_references,_identifier,_dbf_read,_access_read,_access_value,_append_row,write_dbf_copy
from .geochemistry_statistics import _write_workbook
from .raster import file_sha
from .table_join import JoinColumn,JoinParameters,_key


ValueMode=Literal['identity','hex','boolean01','datetime_iso','date_component','time_component']


class DatabaseJoinColumn(JoinColumn):
    null_flag:str|None=Field(default=None,min_length=1,description='New native short field:1=source NULL,0=non-null,-1=unmatched row; required when selected source contains NULL')
    value_mode:ValueMode='identity'


class DatabaseJoinParameters(JoinParameters):
    columns:list[DatabaseJoinColumn]=Field(min_length=1,max_length=500)


class WritebackColumn(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    native:str=Field(min_length=1)
    database:str=Field(min_length=1)
    null_flag:str|None=None
    value_mode:ValueMode='identity'


class WritebackParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    native_key:str=Field(min_length=1)
    table_key:str=Field(min_length=1)
    key_mode:Literal['text','numeric']
    unmatched:Literal['error','ignore']
    native_duplicates:Literal['error','identical']
    columns:list[WritebackColumn]=Field(min_length=1,max_length=500)
    access_output:Literal['isolated_table_copy']|None=None


def _convert(value,mode,reverse=False):
    if value is None:return None
    try:
        if mode=='hex':
            if reverse:
                if not isinstance(value,str) or len(value)%2:raise ValueError('literal even-length hex required')
                return bytes.fromhex(value)
            if not isinstance(value,bytes):raise ValueError('binary source required')
            return value.hex()
        if mode=='boolean01':
            if not isinstance(value,(bool,int)) or value not in (0,1):raise ValueError('explicit boolean or0/1 required')
            return bool(value) if reverse else int(value)
        if mode in {'datetime_iso','date_component','time_component'}:
            if not isinstance(value,str):raise ValueError('ISO source text required')
            if mode=='time_component' and 'T' not in value and ' ' not in value:
                parsed=dt.time.fromisoformat(value)
            else:parsed=dt.datetime.fromisoformat(value)
            if parsed.tzinfo is not None:raise ValueError('source timezone cannot be silently discarded')
            if mode=='date_component':return parsed.date().isoformat()
            if mode=='time_component':return (parsed.time() if isinstance(parsed,dt.datetime) else parsed).isoformat()
            return parsed.isoformat()
        if isinstance(value,(bytes,bool)):raise ValueError('binary/boolean values need explicit hex/boolean01 mode')
        if type(value) not in (str,int,float) or isinstance(value,float) and not math.isfinite(value):raise ValueError('unsupported scalar')
        return value
    except (ValueError,TypeError,OverflowError) as exc:raise BridgeError('DATABASE_VALUE_MODE: '+str(exc)) from exc


def _literal(value):
    # The human audit workbook keeps literal repr strings for binary64/64bit
    # numbers. Calculations use the original database values directly.
    if type(value) is float:return repr(value)
    if type(value) is int:return str(value)
    return value


def _encoded(value):
    if isinstance(value,bytes):return {'type':'bytes','hex':value.hex()}
    if type(value) is int and abs(value)>2**53-1:return {'type':'integer','decimal':str(value)}
    return value


def database_info(bridge,source,limit=20):
    if type(limit) is not int or not 0<=limit<=1000:raise BridgeError('DATABASE_PREVIEW_LIMIT')
    source=DatabaseSource.model_validate(source);base=bridge.root/'work/database-info';base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inspect-',dir=base) as temp:
        _,data,refs=read_snapshot(source,Path(temp)/'snapshot',row_limit=limit)
    preview=[];size=0;truncated=False
    for rid,row in zip(data['row_ids'],data['rows']):
        values=[]
        for value in row:
            if isinstance(value,str) and len(value)>1024:value={'type':'text','length':len(value),'prefix':value[:1024],'truncated':True}
            elif isinstance(value,bytes) and len(value)>512:value={'type':'bytes','length':len(value),'prefix_hex':value[:512].hex(),'sha256':hashlib.sha256(value).hexdigest(),'truncated':True}
            else:value=_encoded(value)
            values.append(value)
        entry={'source_row_id':rid,'values':values};size+=len(json_text(entry).encode('utf8'))
        if size>2*1024**2:truncated=True;break
        preview.append(entry)
    return {'source':source.model_dump(),'columns':data['columns'],'rows':data['total_rows'],'row_identity':data['row_identity'],
        'preview':preview,'preview_truncated_by_bytes':truncated,'content_validation_scope':'Only the requested preview rows decoded; counts/schema and original snapshot hash verified. Full selected-row validation occurs in prepare tools.',
        'source_references':refs,'source_preserved':True,'semantics':{k:v for k,v in data.items() if k not in {'columns','rows','row_ids'}}}


def prepare_database_join(bridge,file,expected_sha256,database,parameters,request_id):
    source=DatabaseSource.model_validate(database);p=DatabaseJoinParameters.model_validate(parameters)
    path=Path(file).resolve(strict=True);_,refs=source_references(source)
    payload={'operation':'database.join','protocol':1,'file':str(path),'expected_sha256':expected_sha256,
        'database':source.model_dump(),'parameters':p.model_dump(exclude_unset=True)}
    sources=[{'file':str(path),'sha256':expected_sha256},*refs]
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,path.name)
    if not new:return receipt
    try:
        selected=list(dict.fromkeys([p.table_key,*[c.source for c in p.columns]]))
        _,data,_=read_snapshot(source,stage/'database-snapshot',selected);names=[c['name'] for c in data['columns']];index={n:i for i,n in enumerate(names)}
        keys=[_key(row[index[p.table_key]],p.key_mode) for row in data['rows']]
        if len(set(keys))!=len(keys):raise BridgeError('JOIN_DUPLICATE_TABLE_KEY: external source key must be unique')
        columns=[];headers=['__DB_KEY'];cells=[[_literal(row[index[p.table_key]])] for row in data['rows']];converted=[]
        for i,c in enumerate(p.columns,1):
            values=[_convert(row[index[c.source]],c.value_mode) for row in data['rows']]
            converted.append(values)
            if any(v is None for v in values) and not c.null_flag:raise BridgeError('DATABASE_NULL_FLAG_REQUIRED: '+c.source)
            alias=f'__DB_{i:03d}';headers.append(alias)
            for row,value in zip(cells,values):row.append(_literal(value))
            spec=c.model_dump(exclude_unset=True);spec.pop('value_mode',None);spec.pop('null_flag',None);spec['source']=alias;columns.append(spec)
            if c.null_flag:
                flag=f'__DB_N{i:03d}';headers.append(flag)
                for row,value in zip(cells,values):row.append(int(value is None))
                columns.append({'source':flag,'name':c.null_flag,'type':'short','missing_value':-1})
        workbook=stage/'source-values.xlsx';_write_workbook(workbook,{'Data':[headers,*cells]})
        _,reader,_=read_native(path);identities=native_ids(reader)
        if p.native_key not in reader.data.columns or not 1<=len(identities)<=200000:raise BridgeError('DATABASE_NATIVE_KEY_OR_ROWS')
        if len(identities)*len(columns)>2000000:raise BridgeError('DATABASE_LIMIT')
        occupied={str(name).casefold() for name in reader.data.columns}
        for c in p.columns:
            for name in [c.name,*([c.null_flag] if c.null_flag else [])]:
                if name.casefold() in occupied:raise BridgeError('JOIN_TARGET_EXISTS')
                occupied.add(name.casefold())
        lookup={key:i for i,key in enumerate(keys)};updates={};mapping=[];used=set();missing=[]
        for position,rid in enumerate(identities):
            key=_key(scalar(reader.data[p.native_key].iloc[position]),p.key_mode);row_index=lookup.get(key)
            if row_index is None:
                missing.append(rid)
                if p.unmatched=='error':raise BridgeError('JOIN_MISSING_KEYS')
            else:used.add(row_index)
            record={}
            for i,c in enumerate(p.columns):
                spec=c.model_dump(exclude_unset=True)
                if row_index is None:
                    if 'missing_value' not in spec:raise BridgeError('JOIN_MISSING_VALUE: '+c.name)
                    value=c.missing_value
                else:
                    value=converted[i][row_index]
                    if value is None:
                        if 'null_value' not in spec:raise BridgeError('JOIN_NULL_VALUE: '+c.name)
                        value=c.null_value
                record[c.name]=value
                if c.null_flag:record[c.null_flag]=-1 if row_index is None else int(converted[i][row_index] is None)
            updates[str(rid)]=record
            mapping.append({'native_record_id':rid,'source_row_id':data['row_ids'][row_index] if row_index is not None else None,
                'database_key':_encoded(data['rows'][row_index][index[p.table_key]]) if row_index is not None else None})
        fields=[{'name':str(name),'source':str(name)} for name in reader.data.columns]
        defaults={'string':'','byte':0,'short':0,'int':0,'float':0.,'double':0.,'date':None,'time':'00:00:00'}
        for c in p.columns:
            fields.append({'name':c.name,'type':c.type,'default':defaults[c.type],**({'length':c.length} if c.length is not None else {})})
            if c.null_flag:fields.append({'name':c.null_flag,'type':'short','default':0})
        encoder=Bridge(stage/'encoder');input_folder=encoder.root/'work/input';input_folder.mkdir(parents=True)
        snapshot=input_folder/path.name;shutil.copyfile(path,snapshot)
        prepare_attribute_schema(encoder,str(snapshot),expected_sha256,fields,'join',attribute_updates=updates)
        joined=encoder.apply('join');folder=stage/'files';folder.mkdir();target=folder/path.name;shutil.copyfile(joined['output'],target)
        metrics={'native_features':len(identities),'matched_features':len(identities)-len(missing),'unmatched_features':len(missing),
            'table_rows':len(data['rows']),'used_table_rows':len(used),'unused_table_rows':len(data['rows'])-len(used)}
        report={'schema':'mapgis-agent/database-join/1','database':source.model_dump(),'parameters':payload['parameters'],'sources':sources,
            'columns':data['columns'],'selected_source_rows':[{'source_row_id':rid,'values':[_encoded(v) for v in row]} for rid,row in zip(data['row_ids'],data['rows'])],
            'row_mapping':mapping,'metrics':metrics,'null_semantics':'Explicit native state field1=NULL,0=non-null,-1=unmatched row; replacement value is never the state. Raw selected values retained.',
            'database_semantics':{k:v for k,v in data.items() if k not in {'columns','rows','row_ids'}},'source_preserved':True}
        (folder/'database-lineage.json').write_text(json_text(report),encoding='utf8')
        shutil.copyfile(workbook,folder/'source-values.xlsx')
        return finish_bundle(bridge,request_id,folder,{'backend':source.format+' task snapshot and existing exact native byte-preserving schema/attribute encoder',
            'metrics':report['metrics'],'source_preserved':True,'native_save_reopen':'separate original operation'})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise


def _keyed(data,key_field,mode):
    names=[f['name'] for f in data['columns']]
    if key_field not in names:raise BridgeError('DATABASE_KEY_FIELD')
    col=names.index(key_field);lookup={}
    for i,row in enumerate(data['rows']):
        key=_key(row[col],mode)
        if key in lookup:raise BridgeError('JOIN_DUPLICATE_TABLE_KEY')
        lookup[key]=i
    return names,col,lookup


def _assert_rows(expected,actual,key_column,mode):
    lookup={_key(row[key_column],mode):row for row in actual}
    if len(lookup)!=len(expected) or len(actual)!=len(expected):raise BridgeError('DATABASE_WRITEBACK_READBACK: row identity/count changed')
    if any(lookup.get(_key(row[key_column],mode))!=row for row in expected):raise BridgeError('DATABASE_WRITEBACK_READBACK: value changed or target type conversion lost information')


def _write_sqlite(path,source,data,key_column,updates,p):
    names=[f['name'] for f in data['columns']];expected=[row.copy() for row in data['rows']]
    with closing(sqlite3.connect(path)) as con:
        con.execute('PRAGMA journal_mode=DELETE');con.execute('PRAGMA foreign_keys=ON');con.execute('BEGIN IMMEDIATE')
        allowed={c.database for c in p.columns}
        def authorize(action,a,b,db,trigger):
            if action==sqlite3.SQLITE_UPDATE:return sqlite3.SQLITE_OK if a==source.table and b in allowed else sqlite3.SQLITE_DENY
            if action in {sqlite3.SQLITE_READ,sqlite3.SQLITE_SELECT,sqlite3.SQLITE_TRANSACTION,sqlite3.SQLITE_RECURSIVE}:return sqlite3.SQLITE_OK
            if action==sqlite3.SQLITE_FUNCTION and b!='load_extension':return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        con.set_authorizer(authorize)
        try:
            for row_index,record in updates.items():
                columns=list(record);values=[record[c] for c in columns]
                affected=con.execute('UPDATE '+_identifier(source.table)+' SET '+','.join(_identifier(c)+'=?' for c in columns)+' WHERE '+_identifier(names[key_column])+'=?',
                    [*values,data['rows'][row_index][key_column]]).rowcount
                if affected!=1:raise BridgeError('DATABASE_UPDATE_CARDINALITY: SQL collation/keys matched a different row count')
                for c,value in record.items():expected[row_index][names.index(c)]=value
            actual=[];budget=[0]
            for row in con.execute('SELECT '+','.join(_identifier(n) for n in names)+' FROM '+_identifier(source.table)):_append_row(actual,list(row),budget)
            _assert_rows(expected,actual,key_column,p.key_mode);con.commit()
        except Exception:
            con.rollback();raise
        finally:con.set_authorizer(None)


def _access_rows(con,table,names):
    rs,_=con.Execute('SELECT '+','.join(_identifier(name,True) for name in names)+' FROM '+_identifier(table,True));rows=[];budget=[0]
    try:
        while not rs.EOF:_append_row(rows,[_access_value(rs.Fields.Item(i).Value) for i in range(rs.Fields.Count)],budget);rs.MoveNext()
    finally:rs.Close()
    return rows


def _access_comparable(rows,fields):
    return [[Decimal(str(v)) if v is not None and f['type'] in (6,14,131) else v for v,f in zip(row,fields)] for row in rows]


def _access_parameter(command,name,value,field):
    import pythoncom,win32com.client
    if value is None:kind=field['type'];size=max(1,min(field['width'],32000));value=win32com.client.VARIANT(pythoncom.VT_NULL,None)
    elif field['type']==7:
        if not isinstance(value,str):raise BridgeError('DATABASE_ACCESS_DATETIME: explicit civil ISO datetime text required')
        parsed=dt.datetime.fromisoformat(value)
        if parsed.tzinfo is not None:raise BridgeError('DATABASE_ACCESS_DATETIME: no implicit timezone conversion')
        # An aware UTC wrapper preserves the engine civil fields. A naive Python
        # datetime gets converted through the Windows local timezone by pywin32.
        value=parsed.replace(tzinfo=dt.timezone.utc);kind=7;size=8
    elif isinstance(value,bool):kind=11;size=1
    elif isinstance(value,int):kind=3 if -2**31<=value<2**31 else 20;size=8
    elif isinstance(value,float):kind=5;size=8
    elif isinstance(value,bytes):kind=205;size=max(1,len(value));value=win32com.client.VARIANT(pythoncom.VT_ARRAY|pythoncom.VT_UI1,value)
    elif isinstance(value,str):kind=203 if len(value)>255 else 202;size=max(1,len(value))
    else:raise BridgeError('DATABASE_ACCESS_PARAMETER')
    command.Parameters.Append(command.CreateParameter(name,kind,1,size,value))


def _access_sql_type(field):
    kind=field['type'];width=field['width']
    fixed={2:'SMALLINT',3:'LONG',4:'SINGLE',5:'DOUBLE',6:'CURRENCY',7:'DATETIME',11:'YESNO',17:'BYTE',20:'BIGINT',72:'GUID',201:'MEMO',203:'MEMO',205:'LONGBINARY'}
    if kind in fixed:return fixed[kind]
    if kind in (129,130,200,202) and 1<=width<=255:return f'TEXT({width})'
    if kind in (128,204) and 1<=width<=255:return f'BINARY({width})'
    if kind in (14,131) and 1<=field.get('precision',0)<=28 and 0<=field.get('scale',-1)<=field['precision']:
        return f'DECIMAL({field["precision"]},{field["scale"]})'
    raise BridgeError('DATABASE_ACCESS_TYPE: field cannot be represented in the isolated table schema')


def _access_copy_table(con,source,data,key_column,key_mode):
    """Typed ordinary table + bound data; no source database objects copied."""
    import win32com.client
    definitions=[_identifier(f['name'],True)+' '+_access_sql_type(f) for f in data['columns']]
    con.Execute('CREATE TABLE '+_identifier(source.table,True)+' ('+','.join(definitions)+')')
    names=[f['name'] for f in data['columns']];con.BeginTrans()
    try:
        for row in data['rows']:
            command=win32com.client.Dispatch('ADODB.Command');command.ActiveConnection=con;command.CommandType=1
            command.CommandText='INSERT INTO '+_identifier(source.table,True)+' ('+','.join(_identifier(n,True) for n in names)+') VALUES ('+','.join('?' for _ in names)+')'
            for i,(value,field) in enumerate(zip(row,data['columns'])):_access_parameter(command,'p'+str(i),value,field)
            rs,affected=command.Execute();command=None
            if rs is not None and rs.State:rs.Close()
            rs=None
            if affected!=1:raise BridgeError('DATABASE_UPDATE_CARDINALITY')
        _assert_rows(_access_comparable(data['rows'],data['columns']),_access_comparable(_access_rows(con,source.table,names),data['columns']),
            key_column,key_mode)
        con.CommitTrans()
    except Exception:con.RollbackTrans();raise


def _write_access_isolated(path,source,data,key_column,updates,p):
    if p.access_output!='isolated_table_copy':raise BridgeError('DATABASE_ACCESS_OUTPUT: explicitly choose isolated_table_copy; source database objects/macros are never copied/executed')
    import pythoncom,win32com.client
    target=path.with_name('isolated-updated.accdb');fields=data['columns'];names=[f['name'] for f in fields]
    for column in p.columns:
        if fields[names.index(column.database)]['type']==7 and column.value_mode!='datetime_iso':
            raise BridgeError('DATABASE_ACCESS_DATETIME: choose datetime_iso explicitly for complete civil datetime writes')
    pythoncom.CoInitialize()
    try:
        catalog=win32com.client.Dispatch('ADOX.Catalog');catalog.Create(f'Provider={source.provider};Data Source="{target}";')
        con=catalog.ActiveConnection
        try:
            _access_copy_table(con,source,data,key_column,p.key_mode)
            _assert_rows(_access_comparable(data['rows'],fields),_access_comparable(_access_rows(con,source.table,names),fields),key_column,p.key_mode)
            con.BeginTrans();expected=[row.copy() for row in data['rows']]
            try:
                for row_index,record in updates.items():
                    command=win32com.client.Dispatch('ADODB.Command');command.ActiveConnection=con;command.CommandType=1
                    columns=list(record);command.CommandText='UPDATE '+_identifier(source.table,True)+' SET '+','.join(_identifier(c,True)+'=?' for c in columns)+' WHERE '+_identifier(names[key_column],True)+'=?'
                    for i,c in enumerate(columns):_access_parameter(command,'p'+str(i),record[c],fields[names.index(c)])
                    _access_parameter(command,'k',data['rows'][row_index][key_column],fields[key_column])
                    rs,affected=command.Execute();command=None
                    if rs is not None and rs.State:rs.Close()
                    rs=None
                    if affected!=1:raise BridgeError('DATABASE_UPDATE_CARDINALITY')
                    for c,value in record.items():expected[row_index][names.index(c)]=value
                _assert_rows(_access_comparable(expected,fields),_access_comparable(_access_rows(con,source.table,names),fields),key_column,p.key_mode)
                con.CommitTrans()
            except Exception:con.RollbackTrans();raise
        finally:
            if con.State:con.Close()
            con=None;catalog=None
    finally:pythoncom.CoUninitialize()
    return target


def prepare_database_writeback(bridge,file,expected_sha256,database,parameters,request_id):
    source=DatabaseSource.model_validate(database);p=WritebackParameters.model_validate(parameters)
    path=Path(file).resolve(strict=True);_,refs=source_references(source)
    output_name={'sqlite':'updated.sqlite','access':'updated.accdb','dbf':'updated.dbf'}[source.format]
    payload={'operation':'database.writeback','protocol':1,'file':str(path),'expected_sha256':expected_sha256,'database':source.model_dump(),'parameters':p.model_dump()}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256},*refs],output_name)
    if not new:return receipt
    try:
        _,reader,_=read_native(path)
        if p.native_key not in reader.data.columns or not 1<=len(reader.geom)<=200000:raise BridgeError('DATABASE_NATIVE_KEY_OR_ROWS')
        if len(reader.geom)*len(p.columns)>2000000:raise BridgeError('DATABASE_LIMIT')
        copy,data,_=read_snapshot(source,stage/'database-snapshot');names,key_column,lookup=_keyed(data,p.table_key,p.key_mode)
        if len({c.database for c in p.columns})!=len(p.columns) or any(c.database==p.table_key for c in p.columns):raise BridgeError('DATABASE_WRITEBACK_COLUMNS: distinct existing targets; key is immutable')
        for c in p.columns:
            if c.native not in reader.data.columns or c.database not in names or c.null_flag is not None and c.null_flag not in reader.data.columns:raise BridgeError('DATABASE_COLUMN')
        updates={};provenance=[];duplicates={};unmatched=[]
        for index,rid in enumerate(native_ids(reader)):
            key=_key(scalar(reader.data[p.native_key].iloc[index]),p.key_mode)
            if key not in lookup:
                unmatched.append(rid)
                if p.unmatched=='error':raise BridgeError('DATABASE_UNMATCHED_NATIVE_KEY')
                continue
            row_index=lookup[key];record={}
            for c in p.columns:
                flag=scalar(reader.data[c.null_flag].iloc[index]) if c.null_flag else 0
                if type(flag) is not int or flag not in (0,1):raise BridgeError('DATABASE_NULL_FLAG: only0/1 may write; -1=unmatched must be resolved explicitly')
                record[c.database]=None if flag==1 else _convert(scalar(reader.data[c.native].iloc[index]),c.value_mode,True)
            if row_index in updates:
                if p.native_duplicates=='error' or updates[row_index]!=record:raise BridgeError('DATABASE_DUPLICATE_NATIVE_KEY: explicit identical policy requires equal values/state')
                duplicates.setdefault(str(data['row_ids'][row_index]),[]).append(rid)
            else:updates[row_index]=record
            provenance.append({'native_record_id':rid,'source_row_id':data['row_ids'][row_index]})
        details={}
        if source.format=='sqlite':_write_sqlite(copy,source,data,key_column,updates,p)
        elif source.format=='dbf':details=write_dbf_copy(copy,source,data,updates)
        else:copy=_write_access_isolated(copy,source,data,key_column,updates,p)
        folder=stage/'files';folder.mkdir();target=folder/output_name;shutil.copyfile(copy,target)
        if source.format=='dbf':target.with_suffix('.cpg').write_text(source.encoding,encoding='ascii')
        report={'schema':'mapgis-agent/database-writeback/1','database':source.model_dump(),'parameters':payload['parameters'],'native_sha256':expected_sha256,
            'metrics':{'native_features':len(reader.geom),'database_rows':len(data['rows']),'updated_rows':len(updates),'unmatched_native_records':len(unmatched)},
            'row_mapping':provenance,'duplicate_native_records':duplicates,'unmatched_native_record_ids':unmatched,
            'source_preserved':True,'write_details':details,'output_scope':
                'New isolated Access file containing only this table data, field types and sizes; query/macro/default/index/relationship/other field properties not copied. Source database retained unchanged.' if source.format=='access' else
                'New task-owned whole-database/DBF copy; transaction or prevalidated fixed-field writes and exact target-table readback before publication.'}
        (folder/'database-lineage.json').write_text(json_text(report),encoding='utf8')
        return finish_bundle(bridge,request_id,folder,{'backend':source.format+' task-copy transaction','metrics':report['metrics'],'source_preserved':True,'source_database_written':False})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
