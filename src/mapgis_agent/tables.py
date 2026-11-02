"""Hash-bound XLSX tables and raw XY samples; Excel need not be running."""
from __future__ import annotations

import datetime
import math
import shutil
from pathlib import Path

from openpyxl import Workbook,load_workbook
from openpyxl.styles import Font,Border,Side

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text,sha
from .creation import prepare_creation


def write_literal_workbook(file,sheets):
    if not file.exists():
        book=Workbook();book.remove(book.active)
        for name,rows in sheets.items():
            ws=book.create_sheet(name)
            for row in rows:
                ws.append(row)
                for cell,value in zip(ws[ws.max_row],row):
                    if isinstance(value,str):cell.data_type='s'
                    cell.font=Font(name='宋体',size=11,color='000000',bold=ws.max_row==1)
            for cell in ws[1]:cell.border=Border(top=Side(style='thin',color='000000'),bottom=Side(style='thin',color='000000'))
            ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
            for col in ws.columns:ws.column_dimensions[col[0].column_letter].width=min(40,max(12,max(len(str(c.value or '')) for c in col)+2))
        book.save(file);book.close()
    check=load_workbook(file,read_only=True,data_only=False,keep_links=False)
    try:
        if set(check.sheetnames)!=set(sheets):raise BridgeError('TABLE_OUTPUT_READBACK')
        for name,expected in sheets.items():
            actual=list(check[name].iter_rows(values_only=True))
            if len(actual)!=len(expected):raise BridgeError('TABLE_OUTPUT_READBACK')
            for a,b in zip(actual,expected):
                for x,y in zip(a,b):
                    if y=='':equal=x in (None,'')
                    elif type(y) in (int,float):equal=type(x) in (int,float) and math.isclose(x,y,rel_tol=2e-15,abs_tol=1e-10)
                    else:equal=x==y
                    if not equal:raise BridgeError('TABLE_OUTPUT_READBACK')
    finally:check.close()


def read_table(file,expected_sha256,sheet):
    path=Path(file).resolve(strict=True)
    if sha(path.read_bytes())!=expected_sha256:raise BridgeError('STALE_SOURCE')
    if path.suffix.lower()!='.xlsx':raise BridgeError('TABLE_FORMAT: XLSX required')
    book=load_workbook(path,read_only=True,data_only=True,keep_links=False)
    try:
        names=list(book.sheetnames)
        if sheet is None:
            if len(names)!=1:raise BridgeError('TABLE_SHEET_REQUIRED: '+', '.join(names))
            sheet=names[0]
        if sheet not in names:raise BridgeError('TABLE_SHEET_NOT_FOUND')
        ws=book[sheet]
        # The producer's cached dimensions can be wrong. Stream actual rows.
        ws.reset_dimensions()
        iterator=ws.iter_rows(values_only=True)
        header=list(next(iterator,()))
        while header and header[-1] is None:header.pop()
        if (not header or len(header)>512 or any(not isinstance(c,str) or not c for c in header)
                or len(set(header))!=len(header)):raise BridgeError('TABLE_HEADER: unique nonempty string columns required')
        rows=[];physical=[]
        for number,row in enumerate(iterator,2):
            if number>200002:raise BridgeError('TABLE_WINDOW: at most 200000 rows per input table')
            if all(v is None for v in row):continue
            if any(v is not None for v in row[len(header):]):raise BridgeError('TABLE_UNNAMED_COLUMN')
            values=list(row[:len(header)])+[None]*max(0,len(header)-len(row))
            for i,value in enumerate(values):
                if isinstance(value,(datetime.date,datetime.time)):values[i]=value.isoformat()
                elif value is not None and type(value) not in (str,int,float,bool):raise BridgeError('TABLE_CELL_TYPE')
                elif isinstance(value,float) and not math.isfinite(value):raise BridgeError('TABLE_NONFINITE_CELL')
            rows.append(values);physical.append(number)
        if not rows:raise BridgeError('TABLE_EMPTY')
    finally:book.close()
    if sha(path.read_bytes())!=expected_sha256:raise BridgeError('STALE_DURING_TABLE_READ')
    return path,{'sheet':sheet,'sheets':names,'columns':header,'rows':rows,'worksheet_rows':physical}


def table_info(file:str,expected_sha256:str,sheet:str|None=None,limit:int=5):
    if type(limit) is not int or not 0<=limit<=20:raise BridgeError('TABLE_PREVIEW_LIMIT')
    path,table=read_table(file,expected_sha256,sheet)
    columns=table['columns'];numeric=[];missing={}
    for i,name in enumerate(columns):
        values=[r[i] for r in table['rows']]
        missing[name]=sum(v is None for v in values)
        if all(type(v) in (int,float) for v in values if v is not None) and any(v is not None for v in values):numeric.append(name)
    return {'file':str(path),'sha256':expected_sha256,'sheet':table['sheet'],'sheets':table['sheets'],
            'columns':columns,'numeric_columns':numeric,'missing_values':missing,'row_count':len(table['rows']),
            'preview':table['rows'][:limit],'source_preserved':True}


def prepare_table_points(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,template:dict,request_id:str):
    if set(parameters)!={'sheet','x_field','y_field','attribute_fields'}:raise BridgeError('UNEXPECTED_PARAMETERS')
    if Path(template.get('name','')).suffix.lower()!='.wt':raise BridgeError('INVALID_TEMPLATE_NAME')
    path,table=read_table(file,expected_sha256,parameters['sheet'])
    columns=table['columns'];fields=parameters['attribute_fields']
    if (not isinstance(fields,list) or not fields or len(set(fields))!=len(fields)
            or any(f not in columns for f in [parameters['x_field'],parameters['y_field'],*fields])):
        raise BridgeError('TABLE_FIELDS')
    xi,yi=(columns.index(parameters[k]) for k in ('x_field','y_field'))
    features=[]
    for number,row in zip(table['worksheet_rows'],table['rows']):
        x,y=row[xi],row[yi]
        if type(x) not in (int,float) or type(y) not in (int,float):raise BridgeError(f'TABLE_XY: numeric coordinates required at worksheet row {number}')
        properties={f:row[columns.index(f)] for f in fields}
        if any(v is None for v in properties.values()):raise BridgeError(f'TABLE_ATTRIBUTE_NULL: declare a replacement before import, row {number}')
        features.append({'type':'Feature','id':f'row-{number}','geometry':{'type':'Point','coordinates':[x,y]},'properties':properties})
    payload={'operation':'table.xy_points','file':str(path),'expected_sha256':expected_sha256,'parameters':parameters,'template':template}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],template['name'])
    if not new:return receipt
    try:
        encoder=Bridge(stage/'encoder')
        prepare_creation(encoder,{'type':'FeatureCollection','features':features},template|{'provenance':{
            'source_file':str(path),'source_sha256':expected_sha256,'sheet':table['sheet'],'worksheet_rows':table['worksheet_rows'],
            'unfiltered_raw_samples':True}},'encode')
        encoded=encoder.apply('encode')
        files=stage/'files';files.mkdir()
        target=files/template['name'];shutil.copyfile(encoded['output'],target)
        metadata={'backend':'openpyxl/native vector encoder','metrics':{'points':len(features),'fields':fields},'source_preserved':True}
        (files/(template['name']+'.json')).write_text(json_text({'schema':'mapgis-agent/table-points/1','source':str(path),
            'source_sha256':expected_sha256,'native_sha256':sha(target.read_bytes()),'parameters':parameters,
            'worksheet_rows':table['worksheet_rows'],'crs':template['crs'],'unfiltered_raw_samples':True}),encoding='utf8')
        return finish_bundle(bridge,request_id,files,metadata)
    except Exception as exc:
        fail_bundle(bridge,request_id,exc)
        raise
