"""Literal assay lineage and unit/censoring QA, without statistical imputation."""
from __future__ import annotations

from collections import Counter
import csv
from decimal import Decimal,DecimalException,InvalidOperation
import math
from pathlib import Path
import re
from typing import Literal

from openpyxl import load_workbook
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictFloat,model_validator

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import BridgeError,json_text
from .geochemistry_statistics import _write_workbook
from .tables import read_table


State=Literal['measured','estimated','left','right','interval','missing','not_analyzed','rejected']
# Decimal factors to kg/kg or g/L. ppm/ppb require an explicit mass-fraction
# declaration; neither is interpreted as a water concentration or oxide factor.
UNITS={u:('mass_fraction',Decimal(f)) for u,f in {
    'kg/kg':'1','%':'.01','wt%':'.01','g/kg':'.001','mg/g':'.001',
    'ppm':'.000001','mg/kg':'.000001','ug/g':'.000001','g/t':'.000001',
    'ppb':'.000000001','ng/g':'.000000001'}.items()}
UNITS.update({u:('mass_concentration',Decimal(f)) for u,f in {'g/L':'1','mg/L':'.001','ug/L':'.000001'}.items()})
NUMBER=r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
RESULT=re.compile(rf'\s*(<=|>=|<|>|≤|≥)?\s*({NUMBER})\s*')


def _unit(value):
    return value.strip().replace('µ','u').replace('μ','u') if isinstance(value,str) else None


class AssayColumn(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    name:str=Field(min_length=1,max_length=80)
    value_field:str
    quantity:Literal['mass_fraction','mass_concentration']
    measurement_basis:str=Field(min_length=1,max_length=240)
    input_unit:str|None=None
    unit_field:str|None=None
    target_unit:str
    qualifier_field:str|None=None
    qualifier_states:dict[str,State]=Field(default_factory=dict)
    token_states:dict[str,State]=Field(default_factory=dict)
    qualified_bound_source:Literal['reported','detection_limit','quantitation_limit','upper_limit']='reported'
    detection_limit:StrictFloat|None=Field(default=None,gt=0)
    detection_limit_field:str|None=None
    quantitation_limit:StrictFloat|None=Field(default=None,gt=0)
    quantitation_limit_field:str|None=None
    upper_limit:StrictFloat|None=Field(default=None,gt=0)
    upper_limit_field:str|None=None
    numeric_strings:StrictBool=True
    allow_negative_measurements:StrictBool=False

    @model_validator(mode='after')
    def unambiguous(self):
        if (self.input_unit is None)==(self.unit_field is None):raise ValueError('exactly one input_unit or unit_field is required')
        target=UNITS.get(_unit(self.target_unit))
        if not target or target[0]!=self.quantity:raise ValueError('target_unit must match explicit quantity')
        if self.input_unit is not None and not self.input_unit.strip():raise ValueError('input_unit must be explicit, for example UNKNOWN')
        for name in ('detection_limit','quantitation_limit','upper_limit'):
            if getattr(self,name) is not None and getattr(self,name+'_field') is not None:raise ValueError('limit value and field are mutually exclusive')
        if self.qualifier_states and not self.qualifier_field:raise ValueError('qualifier_states need qualifier_field')
        for mapping in (self.qualifier_states,self.token_states):
            if any(not k or k!=k.strip() for k in mapping):raise ValueError('qualifier/token keys must be nonempty exact trimmed strings')
        return self


class AssayQAParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    sheet:str|None=None
    id_fields:list[str]=Field(min_length=1,max_length=8)
    batch_fields:list[str]=Field(max_length=8)
    coordinate_fields:list[str]|None=Field(default=None,min_length=2,max_length=2)
    analytes:list[AssayColumn]=Field(min_length=1,max_length=64)


def _number(value,strings=True):
    if type(value) in (int,float):text=str(value)
    elif strings and isinstance(value,str) and re.fullmatch(NUMBER,value.strip()):text=value.strip()
    else:return None
    try:result=Decimal(text)
    except InvalidOperation:return None
    return result if result.is_finite() else None


def _convert(value,ratio):
    if value is None or ratio is None:return None
    try:result=float(value*ratio)
    except (DecimalException,OverflowError) as exc:raise ValueError('numeric_range') from exc
    if not math.isfinite(result) or result==0 and value!=0:raise ValueError('numeric_range')
    return result


def _measurement(row,spec):
    raw=row[spec.value_field];unit=row.get(spec.unit_field) if spec.unit_field else spec.input_unit
    qualifier=row.get(spec.qualifier_field) if spec.qualifier_field else None
    result={'analyte':spec.name,'raw_value':raw,'qualifier':qualifier,'input_unit':unit,
        'target_unit':spec.target_unit,'quantity':spec.quantity,'measurement_basis':spec.measurement_basis,
        'state':'invalid','reported_value':None,'normalized_value':None,'lower_bound':None,'upper_bound':None,
        'lower_closed':None,'upper_closed':None,'detection_limit':None,'quantitation_limit':None,'upper_limit':None}
    issues=[];source=UNITS.get(_unit(unit));target=UNITS[_unit(spec.target_unit)]
    if source is None:issues.append('unknown_unit');ratio=None
    elif source[0]!=spec.quantity:issues.append('unit_dimension_mismatch');ratio=None
    else:ratio=source[1]/target[1]
    bounds={}
    for name in ('detection_limit','quantitation_limit','upper_limit'):
        value=row.get(getattr(spec,name+'_field')) if getattr(spec,name+'_field') else getattr(spec,name)
        parsed=_number(value,spec.numeric_strings)
        if value not in (None,'') and (parsed is None or parsed<=0):issues.append('invalid_'+name)
        bounds[name]=parsed
        try:result[name]=_convert(parsed,ratio)
        except (ValueError,OverflowError):issues.append('numeric_range')
    qstate=None
    if qualifier not in (None,''):
        qstate=spec.qualifier_states.get(str(qualifier).strip())
        if qstate is None:issues.append('unknown_qualifier')
    token=raw.strip() if isinstance(raw,str) else None
    tstate=spec.token_states.get(token)
    numeric=_number(raw,spec.numeric_strings);operator=None
    match=RESULT.fullmatch(raw) if isinstance(raw,str) and spec.numeric_strings else None
    if match:operator=match[1];numeric=Decimal(match[2])
    inline=('left' if operator in ('<','<=','≤') else 'right') if operator else None
    if qstate is not None and ((inline is not None and qstate!=inline) or (tstate is not None and qstate!=tstate)):
        issues.append('qualifier_conflict')
    state=qstate or tstate or inline or ('missing' if raw is None else 'blank' if token=='' else 'measured' if numeric is not None else 'invalid')
    result['state']=state
    try:
        result['reported_value']=_convert(numeric,ratio)
        if state in ('measured','estimated'):
            if numeric is None:issues.append('unparsed_result')
            elif numeric<0 and not spec.allow_negative_measurements:issues.append('negative_measurement')
            else:result['normalized_value']=_convert(numeric,ratio)
        elif state in ('left','right'):
            bound=numeric if inline or spec.qualified_bound_source=='reported' else bounds[spec.qualified_bound_source]
            if bound is None or bound<=0:issues.append('missing_censor_bound')
            else:
                side='upper' if state=='left' else 'lower'
                result[side+'_bound']=_convert(bound,ratio)
                result[side+'_closed']=operator in ('<=','>=','≤','≥') if inline else False
        elif state=='interval':
            lo,hi=bounds['detection_limit'],bounds['quantitation_limit']
            if lo is None or hi is None or not 0<lo<hi:issues.append('invalid_interval')
            else:
                result.update(lower_bound=_convert(lo,ratio),upper_bound=_convert(hi,ratio),lower_closed=True,upper_closed=False)
            result['normalized_value']=None
        elif state=='invalid':issues.append('unparsed_result')
    except (ValueError,OverflowError):issues.append('numeric_range')
    if issues:result['normalized_value']=None
    return result,list(dict.fromkeys(issues))


def _text(m):
    state=m['state']
    if m['quality']!='valid':return '[invalid]'
    if state=='measured':return format(m['normalized_value'],'.17g')
    if state=='estimated':return '[estimated:'+format(m['normalized_value'],'.17g')+']'
    if state=='left':return ('<=' if m['upper_closed'] else '<')+format(m['upper_bound'],'.17g')
    if state=='right':return ('>=' if m['lower_closed'] else '>')+format(m['lower_bound'],'.17g')
    if state=='interval':return '['+format(m['lower_bound'],'.17g')+','+format(m['upper_bound'],'.17g')+')'
    return '['+state+']'


def prepare_assay_qa(bridge,file,expected_sha256,parameters,request_id):
    p=parameters if isinstance(parameters,AssayQAParameters) else AssayQAParameters.model_validate(parameters)
    path,table=read_table(file,expected_sha256,p.sheet);columns=table['columns'];rows=table['rows'];physical=table['worksheet_rows']
    fields=[*p.id_fields,*p.batch_fields,*(p.coordinate_fields or [])]
    for s in p.analytes:fields.extend([s.value_field,*[getattr(s,n) for n in ('unit_field','qualifier_field','detection_limit_field','quantitation_limit_field','upper_limit_field') if getattr(s,n)]])
    if (not set(fields)<=set(columns) or len(set(p.id_fields))!=len(p.id_fields)
        or len(set(p.batch_fields))!=len(p.batch_fields) or len({s.name for s in p.analytes})!=len(p.analytes)
        or any(c.startswith('_A') for c in columns)):raise BridgeError('ASSAY_FIELDS: existing unique fields and unused _A prefix required')
    if len(rows)*len(p.analytes)>500000:raise BridgeError('ASSAY_PAIR_LIMIT')
    book=load_workbook(path,read_only=True,data_only=False,keep_links=False)
    try:
        sheet=book[table['sheet']];sheet.reset_dimensions()
        if any(cell.data_type=='f' for row in sheet for cell in row):raise BridgeError('ASSAY_FORMULA: freeze literal results, preserve original formulas separately')
    finally:book.close()
    records=[dict(zip(columns,row)) for row in rows]
    keys=[json_text([r[f] for f in [*p.id_fields,*p.batch_fields]]) for r in records];counts=Counter(keys)
    measurements=[];issues=[];wide=[columns+['_A_ROW',*[f'_A{i}_{n}' for i in range(1,len(p.analytes)+1) for n in ('VALUE','STATE','QA','UNIT')]]]
    maps=[['SAMPLE','BATCH','X','Y',*[f'A{i}_{n}' for i in range(1,len(p.analytes)+1) for n in ('NAME','STATE','VALUE','QA','UNIT')]]]
    batches={}
    for number,row,key,values in zip(physical,records,keys,rows):
        ids=[row[f] for f in p.id_fields];batch=[row[f] for f in p.batch_fields]
        common=[]
        if any(v is None or type(v) not in (str,int,float) or isinstance(v,str) and not v.strip() for v in ids):common.append('missing_sample_key')
        batch_complete=bool(batch) and all(v is not None and type(v) in (str,int,float) and (not isinstance(v,str) or bool(v.strip())) for v in batch)
        if not batch_complete:common.append('missing_batch_metadata')
        if counts[key]>1:common.append('duplicate_sample_key')
        coordinates=[row[f] for f in p.coordinate_fields] if p.coordinate_fields else ['', '']
        if p.coordinate_fields and any(type(v) not in (int,float) for v in coordinates):common.append('invalid_coordinate')
        bkey=json_text(batch)
        if bkey not in batches:batches[bkey]={'id':f'B{len(batches)+1:04d}','values':dict(zip(p.batch_fields,batch)),'metadata_complete':batch_complete,'rows':0,'counts':Counter()}
        summary=batches[bkey];summary['rows']+=1
        w=[*values,number];mp=[json_text(ids),bkey,*coordinates]
        for index,spec in enumerate(p.analytes,1):
            m,flags=_measurement(row,spec);flags=list(dict.fromkeys([*common,*flags]))
            m.update(worksheet_row=number,sample_key=dict(zip(p.id_fields,ids)),batch_id=summary['id'],batch_values=summary['values'],
                quality='invalid' if flags else 'valid',issues=flags)
            m['usable_exact']=m['quality']=='valid' and m['state']=='measured'
            m['representation']=_text(m);measurements.append(m)
            for flag in flags:issues.append({'worksheet_row':number,'analyte':spec.name,'issue':flag})
            summary['counts'][spec.name+'|'+m['state']+'|'+m['quality']]+=1
            w.extend([m['normalized_value'] if m['usable_exact'] else m['representation'],m['state'],m['quality'],spec.target_unit])
            mp.extend([spec.name,m['state'],m['representation'],m['quality'],spec.target_unit])
        wide.append(w);maps.append(mp)
    report={'schema':'mapgis-agent/assay-qa/1','source_file':str(path),'source_sha256':expected_sha256,
        'sheet':table['sheet'],'source_preserved':True,'parameters':p.model_dump(mode='json'),
        'metrics':{'source_rows':len(rows),'measurements':len(measurements),'issues':len(issues),
            'usable_exact':sum(m['usable_exact'] for m in measurements),'batches':len(batches)},
        'measurements':measurements,'issues':issues,'batches':list(batches.values()),
        'scope':'No substitution, deletion, pooling, batch correction, inferred oxide/element or dry/wet/density conversion. Bounds are explicit; blanks, missing, not-analyzed, rejected, estimated and measured zero remain distinct. Wide includes every row and non-exact cells as text so existing strict statistical tools reject implicit imputation. MapReady is textual QA and needs explicit coordinate CRS/native import separately.'}
    stage,receipt,new=begin_bundle(bridge,{'operation':'geochemistry.assay-qa','protocol':1,'file':str(path),
        'expected_sha256':expected_sha256,'parameters':p.model_dump(mode='json')},request_id,
        [{'file':str(path),'sha256':expected_sha256}],'assay-qa.xlsx')
    if not new:return receipt
    try:
        files=stage/'files';files.mkdir()
        pair_columns=['worksheet_row','analyte','state','quality','usable_exact','raw_value','qualifier','input_unit','target_unit',
            'measurement_basis','reported_value','normalized_value','lower_bound','upper_bound','lower_closed','upper_closed',
            'detection_limit','quantitation_limit','upper_limit','batch_id','sample_key','issues']
        pair_rows=[[m.get(c) if type(m.get(c)) in (int,float,bool) or m.get(c) is None else json_text(m[c]) for c in pair_columns] for m in measurements]
        issue_columns=['worksheet_row','analyte','issue'];issue_rows=[[i[c] for c in issue_columns] for i in issues]
        batch_rows=[[b['id'],json_text(b['values']),b['rows'],json_text(b['counts'])] for b in batches.values()]
        _write_workbook(files/'assay-qa.xlsx',{'Raw':[columns,*rows],'Wide':wide,'MapReady':maps,
            'Measurements':[pair_columns,*pair_rows],'Issues':[issue_columns,*issue_rows],
            'Batches':[['batch_id','values','rows','state_counts'],*batch_rows]})
        for name,header,data in [('measurements.csv',pair_columns,pair_rows),('issues.csv',issue_columns,issue_rows)]:
            with (files/name).open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(header)
                writer.writerows([[json_text(v) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row] for row in data])
        (files/'assay-qa.json').write_text(json_text(report),encoding='utf8')
        return finish_bundle(bridge,request_id,files,{'backend':'literal_openpyxl_decimal_units','metrics':report['metrics'],
            'source_preserved':True,'no_imputation':True,'no_batch_correction':True})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc);raise
