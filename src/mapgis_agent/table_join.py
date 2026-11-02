"""Fingerprint-bound XLSX left joins using existing byte-preserving editors."""
from __future__ import annotations

from decimal import Decimal,InvalidOperation
from pathlib import Path
import shutil
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field
from pymapgis import reader as native

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .attributes import prepare_attribute_schema
from .core import Bridge,BridgeError,json_text,native_ids,read_native,scalar,sha
from .tables import read_table


class JoinTable(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    file:str=Field(min_length=1,description='Local original XLSX path; read only')
    expected_sha256:str=Field(pattern=r'^[0-9a-f]{64}$',description='SHA256 of the original workbook')
    sheet:str=Field(min_length=1,description='Exact worksheet name')


class JoinColumn(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    source:str=Field(min_length=1,description='Existing workbook column')
    name:str=Field(min_length=1,description='New native field name, at most 19 GBK bytes; existing names cannot be overwritten')
    type:Literal['string','byte','short','int','float','double','date','time']
    length:int|None=Field(default=None,ge=1,le=32000,description='Required GBK byte capacity for string; omit for other types')
    missing_value:str|int|float|bool|None=Field(default=None,description='Explicit value for an unmatched native key; required with unmatched=fill')
    null_value:str|int|float|bool|None=Field(default=None,description='Explicit replacement for a matched empty spreadsheet cell')


class JoinParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    native_key:str=Field(min_length=1,description='Native attribute used as join key; may repeat across physical records')
    table_key:str=Field(min_length=1,description='Workbook key column; must be unique and non-null')
    key_mode:Literal['text','numeric']=Field(description='text requires exact strings; numeric explicitly compares finite decimal values, including numeric strings')
    unmatched:Literal['error','fill']=Field(default='error',description='Reject missing native keys or use every column\'s explicit missing_value')
    columns:list[JoinColumn]=Field(min_length=1,max_length=1000,description='New typed fields, in output order')


def _key(value,mode):
    if value is None:raise BridgeError('JOIN_NULL_KEY: choose a complete non-null key column')
    if mode=='text':
        if not isinstance(value,str):raise BridgeError('JOIN_TEXT_KEY: choose a string key or key_mode=numeric; no implicit string conversion')
        return value
    if type(value) not in (str,int,float):raise BridgeError('JOIN_NUMERIC_KEY: choose finite decimal keys or key_mode=text')
    try:
        number=Decimal(str(value))
        if not number.is_finite():raise InvalidOperation
        return number
    except InvalidOperation as exc:raise BridgeError('JOIN_NUMERIC_KEY: a key is not a finite decimal; use key_mode=text for identifiers') from exc


def prepare_table_join(bridge:Bridge,file:str,expected_sha256:str,table:dict|JoinTable,
                       parameters:dict|JoinParameters,request_id:str)->dict:
    binding=JoinTable.model_validate(table);options=JoinParameters.model_validate(parameters)
    path=Path(file).resolve(strict=True);before,reader,sections=read_native(path)
    if sha(before)!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if not 1<=len(reader.geom)<=200000:raise BridgeError('JOIN_NATIVE_ROW_LIMIT: use a non-empty layer of at most 200000 features')
    if len(reader.geom)*len(options.columns)>2000000:raise BridgeError('JOIN_VALUE_LIMIT: at most 2000000 assignments per request; split the selected columns')
    table_path,rows=read_table(binding.file,binding.expected_sha256,binding.sheet)
    if options.native_key not in reader.data.columns or options.table_key not in rows['columns']:raise BridgeError('JOIN_KEY_FIELD: inspect native fields and workbook columns before joining')
    occupied={str(name).casefold() for name in reader.data.columns}
    for column in options.columns:
        if column.source not in rows['columns']:raise BridgeError('JOIN_SOURCE_COLUMN: '+column.source)
        if column.name.casefold() in occupied:raise BridgeError('JOIN_TARGET_EXISTS: choose a new field name; use attribute editing for explicit overwrites')
        occupied.add(column.name.casefold())
    lookup={};key_index=rows['columns'].index(options.table_key)
    for number,row in zip(rows['worksheet_rows'],rows['rows']):
        key=_key(row[key_index],options.key_mode)
        if key in lookup:raise BridgeError(f'JOIN_DUPLICATE_TABLE_KEY: worksheet rows {lookup[key][0]} and {number}; make the workbook key unique')
        lookup[key]=(number,row)
    identities=native_ids(reader)
    keys=[_key(scalar(value),options.key_mode) for value in reader.data[options.native_key]]
    missing=[rid for rid,key in zip(identities,keys) if key not in lookup]
    if missing and options.unmatched=='error':raise BridgeError(f'JOIN_MISSING_KEYS: {len(missing)} native records have no table row; correct the keys or explicitly choose unmatched=fill')
    specs=[column.model_dump(exclude_unset=True) for column in options.columns]
    for spec in specs:
        if missing and 'missing_value' not in spec:raise BridgeError('JOIN_MISSING_VALUE: explicit missing_value required for '+spec['name'])
    updates={};provenance=[];matched_table_rows=set()
    for rid,key in zip(identities,keys):
        record={};match=lookup.get(key)
        for column,spec in zip(options.columns,specs):
            value=match[1][rows['columns'].index(column.source)] if match else spec['missing_value']
            if match and value is None:
                if 'null_value' not in spec:raise BridgeError(f'JOIN_NULL_VALUE: worksheet row {match[0]}, {column.source}; supply null_value explicitly')
                value=spec['null_value']
            record[column.name]=value
        updates[str(rid)]=record
        provenance.append({'record_id':rid,'worksheet_row':match[0] if match else None})
        if match:matched_table_rows.add(match[0])
    fields=[{'name':str(name),'source':str(name)} for name in reader.data.columns]
    placeholders={'string':'','byte':0,'short':0,'int':0,'float':0,'double':0,'date':None,'time':'00:00:00'}
    for column in options.columns:
        field={'name':column.name,'type':column.type,'default':placeholders[column.type]}
        if column.length is not None:field['length']=column.length
        fields.append(field)
    payload={'operation':'table.join','file':str(path),'expected_sha256':expected_sha256,
             'table':binding.model_dump(),'parameters':options.model_dump(exclude_unset=True)}
    sources=[{'file':str(path),'sha256':expected_sha256},{'file':str(table_path),'sha256':binding.expected_sha256}]
    stage,receipt,new=begin_bundle(bridge,payload,request_id,sources,path.name)
    if not new:return receipt
    try:
        encoder=Bridge(stage/'encoder');input_folder=encoder.root/'work/input';input_folder.mkdir(parents=True)
        snapshot=input_folder/path.name;shutil.copyfile(path,snapshot)
        prepare_attribute_schema(encoder,str(snapshot),expected_sha256,fields,'schema')
        schema=encoder.apply('schema')
        encoder.prepare(schema['output'],schema['candidate_sha256'],identities,'values',attribute_updates=updates)
        populated=encoder.apply('values')
        files=stage/'files';files.mkdir();target=files/path.name;shutil.copyfile(populated['output'],target)
        after,checked,new_sections=read_native(target);attribute_index=native._ATTR_HEADER_INDEX[reader.shapeType]
        if native_ids(checked)!=identities or any(before[a:a+n]!=after[b:b+m] for i,((a,n),(b,m)) in enumerate(zip(sections,new_sections)) if i!=attribute_index):raise BridgeError('JOIN_NON_ATTRIBUTE_SECTION_CHANGED')
        metrics={'native_features':len(identities),'matched_features':len(identities)-len(missing),'unmatched_features':len(missing),
                 'table_rows':len(rows['rows']),'used_table_rows':len(matched_table_rows),'unused_table_rows':len(rows['rows'])-len(matched_table_rows)}
        (files/(path.name+'.join.json')).write_text(json_text({'schema':'mapgis-agent/table-join/1','native_sha256':sha(after),
            'sources':sources,'parameters':payload['parameters'],'metrics':metrics,'row_mapping':provenance}),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'openpyxl and existing native byte-preserving schema/attribute editors',
            'metrics':metrics,'source_preserved':True,'physical_record_ids_preserved':True,'non_attribute_sections_byte_identical':True,
            'native_save_reopen':'separate native operation; not inferred from file readback'})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
