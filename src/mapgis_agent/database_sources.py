"""Offline task snapshots of SQLite, ACE Access and classic dBASE III tables."""
from __future__ import annotations

import codecs
from contextlib import contextmanager,closing
import datetime as dt
from decimal import Decimal,InvalidOperation,localcontext
from pathlib import Path
import math,os,shutil,sqlite3,struct
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field

from .artifacts import _check_source
from .core import BridgeError
from .raster import file_sha


class DatabaseSource(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    file:str=Field(min_length=1)
    expected_sha256:str=Field(pattern='^[0-9a-f]{64}$')
    format:Literal['sqlite','access','dbf']
    table:str|None=None
    encoding:str|None=None
    provider:Literal['Microsoft.ACE.OLEDB.16.0','Microsoft.ACE.OLEDB.12.0']='Microsoft.ACE.OLEDB.16.0'


def source_references(source):
    source=DatabaseSource.model_validate(source);path=Path(source.file).resolve(strict=True)
    if not path.is_file() or path.stat().st_size>512*1024**2:raise BridgeError('DATABASE_SIZE: one regular offline database <=512MiB required')
    if source.format=='dbf':
        if source.table is not None or not source.encoding:raise BridgeError('DATABASE_DBF_PARAMETERS: explicit encoding and table=null required')
        codecs.lookup(source.encoding)
    elif not source.table or source.encoding is not None:raise BridgeError('DATABASE_TABLE: exact local table and encoding=null required')
    companions=[]
    if source.format=='sqlite':companions=[Path(str(path)+s) for s in ('-wal','-shm','-journal')]
    elif source.format=='access':companions=[path.with_suffix(s) for s in ('.laccdb','.ldb')]
    refs=[{'file':str(path),'sha256':source.expected_sha256},*[{'file':str(p),'must_be_absent':True} for p in companions]]
    for item in refs:_check_source(item)
    return path,refs


def _identifier(name,access=False):
    if not isinstance(name,str) or not name or '\x00' in name:raise BridgeError('DATABASE_IDENTIFIER')
    if access:
        if ']' in name:raise BridgeError('DATABASE_ACCESS_IDENTIFIER: bracket in field/table name unsupported')
        return '['+name+']'
    return '"'+name.replace('"','""')+'"'


def _append_row(rows,row,budget):
    if len(row)>1000 or len(rows)>=200000 or len(row)*(len(rows)+1)>2000000:raise BridgeError('DATABASE_LIMIT')
    for value in row:
        if isinstance(value,(str,bytes)):
            size=len(value.encode('utf8') if isinstance(value,str) else value)
            if size>4*1024**2:raise BridgeError('DATABASE_LIMIT: selected cell exceeds4MiB')
            budget[0]+=size
        if isinstance(value,float) and not math.isfinite(value):raise BridgeError('DATABASE_NONFINITE_VALUE')
        if budget[0]>128*1024**2:raise BridgeError('DATABASE_LIMIT: selected text/binary exceeds128MiB')
    rows.append(row)


def _select(all_fields,columns):
    result=[dict(f) for f in all_fields] if columns is None else []
    if columns is not None:
        if len(set(columns))!=len(columns):raise BridgeError('DATABASE_DUPLICATE_COLUMN')
        lookup={f['name']:f for f in all_fields}
        for name in columns:
            if name not in lookup:raise BridgeError('DATABASE_COLUMN: '+name)
            result.append(dict(lookup[name]))
    if not result:raise BridgeError('DATABASE_COLUMNS_REQUIRED')
    if len(result)>1000:raise BridgeError('DATABASE_LIMIT: at most1000selected columns')
    return result


def _sqlite_read(path,table,columns,row_limit=None):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as con:
        found=con.execute('SELECT type,sql FROM sqlite_schema WHERE name=?',(table,)).fetchone()
        if found is None or found[0]!='table' or table.startswith('sqlite_') or found[1].lstrip().upper().startswith('CREATE VIRTUAL'):
            raise BridgeError('DATABASE_LOCAL_TABLE: ordinary local base table required')
        all_fields=[{'name':row[1],'type':row[2],'nullable':not bool(row[3]),'primary_key_position':row[5]} for row in con.execute('PRAGMA table_info('+_identifier(table)+')')]
        fields=_select(all_fields,columns);count=con.execute('SELECT count(*) FROM '+_identifier(table)).fetchone()[0]
        wanted=count if row_limit is None else min(count,row_limit)
        if count>200000 or wanted*len(fields)>2000000:raise BridgeError('DATABASE_LIMIT')
        if row_limit is None:
            for field in fields:
                if con.execute('SELECT 1 FROM '+_identifier(table)+' WHERE length('+_identifier(field['name'])+')>? LIMIT 1',(4*1024**2,)).fetchone():
                    raise BridgeError('DATABASE_LIMIT: selected cell exceeds4MiB')
        rows=[];budget=[0]
        query='SELECT '+','.join(_identifier(f['name']) for f in fields)+' FROM '+_identifier(table)+(f' LIMIT {wanted}' if row_limit is not None else '')
        for row in con.execute(query):_append_row(rows,list(row),budget)
    return {'columns':fields,'rows':rows,'total_rows':count,'row_ids':list(range(1,len(rows)+1)),'row_identity':'snapshot query ordinal; exact key separately retained'}


@contextmanager
def access_connection(path,provider,read_only=False):
    # No Access application, workbook or macro is launched. ACE opens only our copy.
    _common_program_files()
    import pythoncom,win32com.client
    pythoncom.CoInitialize();con=None
    try:
        con=win32com.client.Dispatch('ADODB.Connection');con.Mode=1 if read_only else 3
        con.Open(f'Provider={provider};Data Source="{path}";Persist Security Info=False;')
        yield con
    finally:
        if con is not None:
            if con.State:con.Close()
            con=None
        pythoncom.CoUninitialize()


def _common_program_files():
    # MCP's default child environment intentionally inherits few variables.
    # ACE's metadata classes depend on this Windows runtime directory. Resolve
    # that one OS-owned directory; never import the full host environment.
    if os.name=='nt' and 'CommonProgramFiles' not in os.environ:
        from win32com.shell import shell,shellcon
        folder=shell.SHGetFolderPath(0,shellcon.CSIDL_PROGRAM_FILES_COMMON,0,0)
        if not folder or not Path(folder).is_dir():raise BridgeError('DATABASE_WINDOWS_COMMON_FILES')
        os.environ.setdefault('CommonProgramFiles',folder)


def _access_value(value):
    if isinstance(value,dt.datetime):
        # Pywin32 labels returned VT_DATE as UTC. The engine civil fields are
        # unchanged: independently checked against Access Format() and CDbl().
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,(bytearray,memoryview)):return bytes(value)
    return value


def _access_read(path,source,columns,row_limit=None):
    import win32com.client
    table=source.table
    with access_connection(path,source.provider,True) as con:
        catalog=win32com.client.Dispatch('ADOX.Catalog');catalog.ActiveConnection=con
        try:definition=catalog.Tables.Item(table)
        except Exception as exc:raise BridgeError('DATABASE_LOCAL_TABLE: Access table metadata lookup failed: '+str(exc)) from exc
        if definition.Type!='TABLE':raise BridgeError('DATABASE_LOCAL_TABLE: Access base table required, no query/system/link')
        for i in range(definition.Properties.Count):
            prop=definition.Properties.Item(i)
            if prop.Name in {'Jet OLEDB:Link Datasource','Jet OLEDB:Link Provider String'} and prop.Value:
                raise BridgeError('DATABASE_LOCAL_TABLE: linked external tables require their own explicit source binding')
        prop=None;definition=None;catalog=None
        rs,_=con.Execute('SELECT * FROM '+_identifier(table,True)+' WHERE 1=0')
        all_fields=[{'name':rs.Fields.Item(i).Name,'type':rs.Fields.Item(i).Type,'width':rs.Fields.Item(i).DefinedSize,
            'precision':rs.Fields.Item(i).Precision,'scale':rs.Fields.Item(i).NumericScale,
            'fixed_length':bool(rs.Fields.Item(i).Attributes&0x10),
            'nullable':bool(rs.Fields.Item(i).Attributes&0x20)} for i in range(rs.Fields.Count)]
        rs.Close();rs=None;fields=_select(all_fields,columns)
        rs,_=con.Execute('SELECT count(*) FROM '+_identifier(table,True));count=int(rs.Fields.Item(0).Value);rs.Close();rs=None
        wanted=count if row_limit is None else min(count,row_limit)
        if count>200000 or wanted*len(fields)>2000000:raise BridgeError('DATABASE_LIMIT')
        rows=[];budget=[0]
        if wanted:
            rs,_=con.Execute('SELECT '+(f'TOP {wanted} ' if row_limit is not None else '')+','.join(_identifier(f['name'],True) for f in fields)+' FROM '+_identifier(table,True))
            while not rs.EOF:
                if any(rs.Fields.Item(i).ActualSize>4*1024**2 for i in range(rs.Fields.Count)):raise BridgeError('DATABASE_LIMIT: selected cell exceeds4MiB')
                _append_row(rows,[_access_value(rs.Fields.Item(i).Value) for i in range(rs.Fields.Count)],budget);rs.MoveNext()
            rs.Close();rs=None
    return {'columns':fields,'rows':rows,'total_rows':count,'row_ids':list(range(1,len(rows)+1)),
        'row_identity':'snapshot query ordinal; exact key separately retained','datetime_semantics':'Access engine civil datetime as ISO; no timezone inference or naive Python VT_DATE parameter conversion'}


def _dbf_layout(path,encoding):
    with path.open('rb') as stream:
        header=stream.read(32)
        if len(header)!=32 or header[0]!=3:raise BridgeError('DATABASE_DBF_VERSION: classic dBASE III without memo required; other versions retained as gaps')
        count,header_size,row_size=struct.unpack_from('<IHH',header,4)
        if header_size<33 or (header_size-33)%32 or row_size<1 or header_size+count*row_size>path.stat().st_size:
            raise BridgeError('DATABASE_DBF_LAYOUT')
        fields=[];offset=1
        for index in range((header_size-33)//32):
            descriptor=stream.read(32);name=descriptor[:11].split(b'\x00',1)[0].decode(encoding,'strict');kind=chr(descriptor[11]);width=descriptor[16];precision=descriptor[17]
            if not name or kind not in {'C','N','F','D','L'} or not width:raise BridgeError('DATABASE_DBF_FIELD: only C/N/F/D/L classic fields supported')
            if kind=='D' and width!=8 or kind=='L' and width!=1:raise BridgeError('DATABASE_DBF_FIELD_WIDTH')
            fields.append({'name':name,'type':kind,'width':width,'precision':precision,'offset':offset,'nullable':True});offset+=width
        if stream.read(1)!=b'\r' or offset!=row_size or len({f['name'] for f in fields})!=len(fields):raise BridgeError('DATABASE_DBF_LAYOUT')
    return fields,count,header_size,row_size


def _dbf_value(raw,field,encoding):
    kind=field['type']
    if kind=='C':
        if set(raw)=={0}:return None
        value=raw.rstrip(b' \x00').decode(encoding,'strict')
        if '\x00' in value:raise BridgeError('DATABASE_DBF_EMBEDDED_NUL')
        return value
    token=raw.strip()
    if kind in {'N','F'}:
        if not token or set(token)<={42}:return None
        try:
            value=Decimal(token.decode('ascii'))
            if not value.is_finite():raise InvalidOperation
            return str(value)
        except (ValueError,InvalidOperation) as exc:raise BridgeError('DATABASE_DBF_NUMERIC') from exc
    if kind=='D':
        if token in (b'',b'0',b'00000000') or raw[:1]==b'\x00':return None
        if len(token)!=8:raise BridgeError('DATABASE_DBF_DATE')
        return dt.date(int(token[:4]),int(token[4:6]),int(token[6:])).isoformat()
    if token.upper() in (b'?',b''):return None
    if token.upper() in (b'T',b'Y'):return True
    if token.upper() in (b'F',b'N'):return False
    raise BridgeError('DATABASE_DBF_LOGICAL')


def _dbf_read(path,source,columns,row_limit=None):
    all_fields,count,header,row_size=_dbf_layout(path,source.encoding);fields=_select(all_fields,columns)
    wanted=count if row_limit is None else min(count,row_limit)
    if count>200000 or wanted*len(fields)>2000000:raise BridgeError('DATABASE_LIMIT')
    rows=[];ids=[];deleted=[];budget=[0]
    with path.open('rb') as stream:
        stream.seek(header)
        for identity in range(1,count+1):
            row=stream.read(row_size)
            if row[:1]==b'*':deleted.append(identity);continue
            if row[:1]!=b' ':raise BridgeError('DATABASE_DBF_RECORD_FLAG')
            if row_limit is None or len(rows)<row_limit:
                _append_row(rows,[_dbf_value(row[f['offset']:f['offset']+f['width']],f,source.encoding) for f in fields],budget);ids.append(identity)
    return {'columns':fields,'rows':rows,'total_rows':count-len(deleted),'row_ids':ids,'row_identity':'1based physical DBF slot; deleted slots retained in file, excluded from joins',
        'deleted_record_ids':deleted,'string_semantics':'Explicit byte policy: classic C space-padding trims to empty text; all-NUL marker is None. These physical byte patterns stay distinct; no universal legacy DBF NULL convention inferred.','encoding':codecs.lookup(source.encoding).name}


def read_snapshot(source,folder,columns=None,row_limit=None):
    if row_limit is not None and (type(row_limit) is not int or not 0<=row_limit<=1000):raise BridgeError('DATABASE_PREVIEW_LIMIT')
    source=DatabaseSource.model_validate(source);path,refs=source_references(source)
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=False)
    suffix={'sqlite':'.sqlite','access':'.accdb','dbf':'.dbf'}[source.format]
    if source.format=='access' and path.suffix.lower()=='.mdb':suffix='.mdb'
    copy=folder/('database'+suffix);shutil.copyfile(path,copy)
    if file_sha(copy)!=source.expected_sha256:raise BridgeError('STALE_SOURCE')
    if source.format=='sqlite':data=_sqlite_read(copy,source.table,columns,row_limit)
    elif source.format=='access':data=_access_read(copy,source,columns,row_limit)
    else:data=_dbf_read(copy,source,columns,row_limit)
    for item in refs:_check_source(item)
    return copy,data,refs


def encode_dbf_value(value,field,encoding):
    """Exact classic-field encoding for a new task copy; never round/truncate."""
    kind=field['type'];width=field['width']
    if value is None:return {'C':b'\x00','N':b'*','F':b'*','D':b'0','L':b'?'}[kind]*width
    try:
        if kind=='C':
            if not isinstance(value,str) or value.rstrip(' \x00')!=value or '\x00' in value:raise ValueError('DBF text padding/embedded NUL cannot roundtrip')
            encoded=value.encode(encoding,'strict')
        elif kind in {'N','F'}:
            if isinstance(value,bool):raise ValueError('boolean is not a numeric measurement')
            number=Decimal(str(value));precision=field['precision']
            if not number.is_finite() or number.adjusted()+precision+2>width+1:raise ValueError('DBF numeric width')
            with localcontext() as context:
                context.prec=width+precision+32;fixed=number.quantize(Decimal(1).scaleb(-precision))
            if fixed!=number:raise ValueError('DBF decimal precision would change the value')
            encoded=format(fixed,'f').encode('ascii')
        elif kind=='D':
            encoded=dt.date.fromisoformat(value).strftime('%Y%m%d').encode('ascii')
        else:
            if type(value) is not bool:raise ValueError('explicit boolean01 mode required')
            encoded=b'T' if value else b'F'
        if len(encoded)>width:raise ValueError('DBF field width would truncate')
        return encoded.rjust(width,b' ') if kind in {'N','F'} else encoded.ljust(width,b' ')
    except (ValueError,TypeError,InvalidOperation,UnicodeError) as exc:raise BridgeError('DATABASE_DBF_CONVERSION: '+str(exc)) from exc


def write_dbf_copy(path,source,data,updates):
    fields,count,header,row_size=_dbf_layout(path,source.encoding);lookup={f['name']:f for f in fields}
    patches=[];expected=[row.copy() for row in data['rows']];names=[f['name'] for f in data['columns']]
    for row_index,record in updates.items():
        rid=data['row_ids'][row_index]
        for name,value in record.items():
            f=lookup[name];encoded=encode_dbf_value(value,f,source.encoding)
            expected[row_index][names.index(name)]=_dbf_value(encoded,f,source.encoding)
            patches.append((header+(rid-1)*row_size+f['offset'],encoded))
    # Validate every new byte string before any write. Invalid conversion leaves
    # this task copy unchanged; later I/O failure never publishes a partial set.
    patches.sort()
    with path.open('r+b') as stream:
        for offset,encoded in patches:stream.seek(offset);stream.write(encoded)
        stream.flush();os.fsync(stream.fileno())
    checked=_dbf_read(path,source,None)
    if checked['rows']!=expected or checked['row_ids']!=data['row_ids'] or checked['deleted_record_ids']!=data['deleted_record_ids']:
        raise BridgeError('DATABASE_WRITEBACK_READBACK')
    # Stream-check all bytes outside edited fields, including deleted records,
    # descriptors, original header date and opaque trailing bytes.
    with Path(source.file).open('rb') as original,path.open('rb') as edited:
        position=0
        for offset,encoded in [*patches,(path.stat().st_size,b'')]:
            remaining=offset-position
            while remaining:
                chunk=min(1024*1024,remaining)
                if original.read(chunk)!=edited.read(chunk):raise BridgeError('DATABASE_DBF_UNCHANGED_BYTES')
                remaining-=chunk
            original.seek(len(encoded),1);edited.seek(len(encoded),1);position=offset+len(encoded)
    return {'physical_slots_preserved':True,'nonedited_bytes_identical':True,'changed_fields':len(patches)}
