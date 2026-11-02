"""Explicit domain-wise geochemical estimators; screening is not geological truth."""
from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Literal

import numpy as np
from openpyxl import Workbook,load_workbook
from openpyxl.cell import WriteOnlyCell
from pydantic import BaseModel,ConfigDict,Field,StrictFloat,StrictInt

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import BridgeError,json_text
from .tables import read_table
from .geochemistry_grouped import GroupedParameters,grouped_estimate


class BackgroundParameters(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    sheet:str|None=None
    elements:list[str]=Field(min_length=1,max_length=64)
    id_field:str
    domain_fields:list[str]=Field(default_factory=list,max_length=8)
    units:dict[str,str|None]
    method:Literal['iterative_sigma','median_mad','quantile','grouped']
    transform:Literal['raw','log10']
    trim_sides:Literal['upper','both']='both'
    screen_sigma:StrictFloat|None=Field(default=None,gt=0)
    threshold_sigma:StrictFloat|None=Field(default=None,gt=0)
    mad_scale:StrictFloat|None=Field(default=None,gt=0)
    quantile:StrictFloat|None=Field(default=None,gt=.5,le=1)
    max_iterations:StrictInt=Field(default=100,ge=1,le=10000)
    minimum_n:StrictInt=Field(default=3,ge=2,le=200000)
    grouped:GroupedParameters|None=None


def _native(value,transform):
    if value is None:return None
    try:result=float(value) if transform=='raw' else 10.**float(value)
    except OverflowError as exc:raise BridgeError('GEOCHEM_THRESHOLD_RANGE') from exc
    if not math.isfinite(result) or transform=='log10' and result<=0:raise BridgeError('GEOCHEM_THRESHOLD_RANGE')
    return result


def _estimate(values,rows,p):
    active=np.arange(len(values));rounds=[];mad=None
    for iteration in range(p.max_iterations):
        selected=values[active]
        if len(selected)<p.minimum_n:raise BridgeError('GEOCHEM_BACKGROUND_TOO_SMALL')
        peak=float(np.max(np.abs(selected)))
        scale=peak if peak>1e150 or 0<peak<1e-150 else 1.
        normalized=selected/scale
        mean=float(np.mean(normalized)*scale);sd=float(np.std(normalized,ddof=1)*scale)
        if not all(math.isfinite(v) for v in (mean,sd)):raise BridgeError('GEOCHEM_NUMERIC_RANGE')
        if p.method in {'iterative_sigma','grouped'}:
            centre=mean;spread=sd
            lower=mean-p.screen_sigma*sd if p.trim_sides=='both' else None;upper=mean+p.screen_sigma*sd
            removed=(selected>upper) | ((selected<lower) if lower is not None else False)
        elif p.method=='median_mad':
            centre=float(np.median(normalized)*scale)
            mad=float(np.median(np.abs(normalized-centre/scale))*scale);spread=mad*p.mad_scale
            lower=centre-p.threshold_sigma*spread if p.trim_sides=='both' else None;upper=centre+p.threshold_sigma*spread
            removed=np.zeros(len(selected),dtype=bool)
        else:
            centre=float(np.median(normalized)*scale);spread=None
            lower=float(np.quantile(normalized,1-p.quantile,method='linear')*scale) if p.trim_sides=='both' else None
            upper=float(np.quantile(normalized,p.quantile,method='linear')*scale);removed=np.zeros(len(selected),dtype=bool)
        if not all(v is None or math.isfinite(v) for v in (centre,spread,lower,upper)):
            raise BridgeError('GEOCHEM_NUMERIC_RANGE')
        rounds.append({'iteration':iteration,'n':len(active),'mean_analysis':mean,'sample_sd_analysis':sd,
            'centre_analysis':centre,'spread_analysis':spread,'lower_analysis':lower,'upper_analysis':upper,
            'removed_worksheet_rows':[rows[i] for i in active[removed]],
            'converged':not bool(removed.any())})
        if not removed.any():break
        active=active[~removed]
    else:raise BridgeError('GEOCHEM_ITERATION_LIMIT: incomplete screening is not a final estimate')
    histogram=None
    if p.method=='iterative_sigma':
        lower=centre-p.threshold_sigma*sd if p.trim_sides=='both' else None;upper=centre+p.threshold_sigma*sd
    elif p.method=='grouped':
        centre,spread,lower,upper,histogram=grouped_estimate(selected,p.grouped,p.threshold_sigma,p.trim_sides)
    result={'raw_n':len(values),'background_n':len(active),'centre_analysis':centre,'sample_mean_analysis':mean,
        'sample_sd_analysis':sd,'mad_unscaled_analysis':mad,'spread_analysis':spread,
        'lower_threshold_analysis':lower,'upper_threshold_analysis':upper,
        'background_centre_native':_native(centre,p.transform),
        'lower_threshold_native':_native(lower,p.transform),'upper_threshold_native':_native(upper,p.transform),
        'retained_worksheet_rows':[rows[i] for i in active],'rounds':rounds}
    if histogram is not None:result['histogram']=histogram
    return result,set(active.tolist())


def _write_workbook(file,sheets):
    book=Workbook(write_only=True)
    for name,rows in sheets.items():
        sheet=book.create_sheet(name)
        for row in rows:
            cells=[]
            for value in row:
                cell=WriteOnlyCell(sheet,value=value)
                if isinstance(value,str):
                    if len(value)>32767:raise BridgeError('GEOCHEM_EXCEL_TEXT_LIMIT')
                    cell.data_type='s'
                cells.append(cell)
            sheet.append(cells)
    book.save(file);book.close()
    check=load_workbook(file,read_only=True,data_only=False,keep_links=False)
    try:
        if check.sheetnames!=list(sheets):raise BridgeError('GEOCHEM_WORKBOOK_READBACK')
        for name,expected in sheets.items():
            count=0
            for index,row in enumerate(check[name].iter_rows(values_only=True)):
                count+=1
                if index>=len(expected):raise BridgeError('GEOCHEM_WORKBOOK_READBACK')
                if any(value is not None for value in row[len(expected[index]):]):raise BridgeError('GEOCHEM_WORKBOOK_READBACK')
                for column,value in enumerate(expected[index]):
                    actual=row[column] if column<len(row) else None
                    equal=(actual in (None,'') if value=='' else
                        type(actual) in (int,float) and math.isclose(actual,value,rel_tol=2e-15,abs_tol=0.)
                        if type(value) in (int,float) else actual==value and type(actual)==type(value))
                    if not equal:raise BridgeError('GEOCHEM_WORKBOOK_READBACK')
            if count!=len(expected):raise BridgeError('GEOCHEM_WORKBOOK_READBACK')
    finally:check.close()


def prepare_statistics(bridge,file,expected_sha256,parameters,request_id):
    p=parameters if isinstance(parameters,BackgroundParameters) else BackgroundParameters.model_validate(parameters)
    if (p.method in {'iterative_sigma','grouped'} and (p.screen_sigma is None or p.threshold_sigma is None or p.mad_scale is not None or p.quantile is not None)
        or p.method=='median_mad' and (p.mad_scale is None or p.threshold_sigma is None or p.screen_sigma is not None or p.quantile is not None)
        or p.method=='quantile' and (p.quantile is None or any(v is not None for v in (p.screen_sigma,p.threshold_sigma,p.mad_scale)))):
        raise BridgeError('GEOCHEM_METHOD_PARAMETERS: supply only explicit parameters of the chosen method')
    if (p.method=='grouped')!=(p.grouped is not None):raise BridgeError('GEOCHEM_METHOD_PARAMETERS: grouped settings require method=grouped')
    path,table=read_table(file,expected_sha256,p.sheet);columns=table['columns'];rows=table['rows'];physical=table['worksheet_rows']
    if (len(set(p.elements))!=len(p.elements) or len(set(p.domain_fields))!=len(p.domain_fields)
        or not set([p.id_field,*p.elements,*p.domain_fields])<=set(columns)
        or p.id_field in p.elements or set(p.elements)&set(p.domain_fields)):
        raise BridgeError('GEOCHEM_FIELDS')
    if set(p.units)!=set(p.elements) or any(value is not None and not value.strip() for value in p.units.values()):
        raise BridgeError('GEOCHEM_UNITS: each element needs an explicit common unit or explicit null for unknown')
    if len(rows)*len(p.elements)>500000:raise BridgeError('GEOCHEM_PAIR_LIMIT: at most500000sample-element pairs per request')
    if any(name.startswith('_G') for name in columns):raise BridgeError('GEOCHEM_RESERVED_FIELDS: _G prefix belongs to derived classification fields')
    book=load_workbook(path,read_only=True,data_only=False,keep_links=False)
    try:
        if any(cell.data_type=='f' for row in book[table['sheet']] for cell in row):
            raise BridgeError('GEOCHEM_FORMULA: freeze evaluated literal input values before statistical analysis')
    finally:book.close()
    ids=[row[columns.index(p.id_field)] for row in rows]
    if (any(value is None or type(value) not in (str,int,float) or isinstance(value,str) and not value.strip() for value in ids)
        or len({json_text(value) for value in ids})!=len(ids)):
        raise BridgeError('GEOCHEM_SAMPLE_ID: unique nonempty literal sample keys required')
    groups={};row_domains={}
    for index,row in enumerate(rows):
        domain=[row[columns.index(field)] for field in p.domain_fields]
        if any(value is None or isinstance(value,str) and not value.strip() for value in domain):raise BridgeError('GEOCHEM_DOMAIN_REQUIRED')
        key=json_text(domain)
        if key not in groups:
            if len(groups)>=1000:raise BridgeError('GEOCHEM_DOMAIN_LIMIT')
            groups[key]={'id':f'D{len(groups)+1:04d}','values':dict(zip(p.domain_fields,domain)),'indices':[]}
        groups[key]['indices'].append(index)
        row_domains[index]=groups[key]['id']
    results=[];samples=[];classifications={};round_table=[];removed_table=[];histogram_table=[]
    for group in groups.values():
        indices=group['indices'];source_rows=[physical[i] for i in indices]
        for element_index,element in enumerate(p.elements,1):
            raw=[rows[i][columns.index(element)] for i in indices]
            if any(type(value) not in (int,float) or not math.isfinite(value) for value in raw):
                raise BridgeError('GEOCHEM_FINITE_MEASUREMENTS: resolve missing/censored/nonmeasurement values explicitly; no substitution')
            if p.transform=='log10' and any(value<=0 for value in raw):raise BridgeError('GEOCHEM_LOG_POSITIVE: zero and negative are not silently imputed')
            values=np.asarray(raw,dtype=np.float64);values=np.log10(values) if p.transform=='log10' else values
            result,retained=_estimate(values,source_rows,p)
            result.update(domain_id=group['id'],domain=group['values'],element=element,unit=p.units[element],
                negative_observations=sum(value<0 for value in raw),zero_observations=sum(value==0 for value in raw))
            results.append(result)
            if 'histogram' in result:
                histogram=result['histogram']
                if len(histogram_table)+len(histogram['counts'])>500000:raise BridgeError('GEOCHEM_HISTOGRAM_LIMIT')
                histogram_table.extend([group['id'],element,i,histogram['edges'][i],histogram['edges'][i+1],histogram['centres'][i],count]
                    for i,count in enumerate(histogram['counts']))
            for step in result['rounds']:
                round_table.append([group['id'],element,step['iteration'],step['n'],step['mean_analysis'],step['sample_sd_analysis'],
                    step['centre_analysis'],step['spread_analysis'],step['lower_analysis'],step['upper_analysis'],len(step['removed_worksheet_rows']),step['converged']])
                removed_table.extend([group['id'],element,step['iteration'],number] for number in step['removed_worksheet_rows'])
            for local,index in enumerate(indices):
                value=float(values[local]);lower=result['lower_threshold_analysis'];upper=result['upper_threshold_analysis']
                code=1 if value>upper else -1 if lower is not None and value<lower else 0
                keep=local in retained
                samples.append([physical[index],ids[index],group['id'],element,p.units[element],raw[local],value,keep,code])
                classifications[index,element_index]=[code,keep,result['upper_threshold_native'],result['lower_threshold_native']]
    serialized=p.model_dump(exclude={'grouped'} if p.grouped is None else set())
    payload={'operation':'geochemistry.background-statistics','file':str(path),'expected_sha256':expected_sha256,
        'parameters':serialized,'statistics_protocol':2 if p.grouped is not None else 1}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],'analysis.json')
    if not new:return receipt
    try:
        folder=stage/'files';folder.mkdir()
        method={'method':p.method,'transform':p.transform,'standard_deviation':'sample;ddof=1',
            'screening':'Independent monotone per-domain/element; remove strictly outside bounds, ties retained; constant data converge.',
            'quantile_interpolation':'linear','normality_tested':False,'geological_background_or_mineralization_validated':False,
            'classification':'Compare every original sample in analysis scale to final thresholds; screened-out samples remain in outputs.',
            'legacy_macro_equivalence':False,'legacy_boundary':'Installed rawmacro reuses prior element loop count; this estimator resets per element. No macro execution, stale header rounding or unconditional substitution.',
            'unresolved_values':'Reject missing/censored/formula/nonfinite measurements; log requires strictly positive values; no unit or domain inference.'}
        if p.grouped is not None:method['grouped_estimation']={'settings':p.grouped.model_dump(),
            'order':'Independent sigma screening, then equal-width grouped estimator on retained samples.',
            'spread':'Estimator spread is grouped sample SD or the explicitly named lower-half mode equation; diagnostic sample_sd_analysis still refers to the retained original values.',
            'legacy_equivalence':'Uses in-useSheet8/Sheet10 algebra; noVBA Single/productrounding,Findstate,zero-widthfallback orwrongcross-elementstate equivalence.'}
        analysis={'schema':'mapgis-agent/geochemistry-statistics/1','source':str(path),'source_sha256':expected_sha256,
            'sheet':table['sheet'],'sample_count':len(rows),'parameters':serialized,'method':method,'results':results,
            'derived_field_map':{str(i):{'element':element,'fields':[f'_G{i}_CLASS',f'_G{i}_KEEP',f'_G{i}_UPPER',f'_G{i}_LOWER']} for i,element in enumerate(p.elements,1)}}
        (folder/'analysis.json').write_text(json_text(analysis),encoding='utf8')
        summary_header=['Domain','Element','Unit','Raw N','Background N','Centre analysis','Sample SD analysis','MAD analysis','Centre native','Lower native','Upper native']
        summary=[[r['domain_id'],r['element'],r['unit'],r['raw_n'],r['background_n'],r['centre_analysis'],r['sample_sd_analysis'],
            r['mad_unscaled_analysis'],r['background_centre_native'],r['lower_threshold_native'],r['upper_threshold_native']] for r in results]
        if p.grouped is not None:
            summary_header.append('Estimator spread analysis')
            for row,result in zip(summary,results):row.append(result['spread_analysis'])
        sample_header=['Worksheet row','Sample ID','Domain','Element','Unit','Raw value','Analysis value','Screening retained','class']
        for name,header,values in [('summary.csv',summary_header,summary),('samples.csv',sample_header,samples)]:
            with (folder/name).open('x',encoding='utf-8-sig',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(header);writer.writerows(values)
        derived=[field for i in range(1,len(p.elements)+1) for field in [f'_G{i}_CLASS',f'_G{i}_KEEP',f'_G{i}_UPPER',f'_G{i}_LOWER']]
        classified=[columns+['_G_ROW','_G_DOMAIN']+derived]
        for index,row in enumerate(rows):
            classified.append(row+[physical[index],row_domains[index]]+[v for i in range(1,len(p.elements)+1) for v in classifications[index,i]])
        sheets={'Raw':[columns,*rows],'Summary':[summary_header,*summary],
            'Samples':[sample_header,*samples],'Rounds':[['Domain','Element','Iteration','N','Mean','Sample SD','Centre','Spread','Lower','Upper','Removed N','Converged'],*round_table],
            'Removed':[['Domain','Element','Iteration','Worksheet row'],*removed_table],'Classified':classified}
        if histogram_table:sheets['Histogram']=[['Domain','Element','Bin','Left inclusive','Right exclusive','Centre','Frequency'],*histogram_table]
        _write_workbook(folder/'analysis.xlsx',sheets)
        return finish_bundle(bridge,request_id,folder,{'backend':'openpyxl/NumPy; explicit estimators, no Excel macro execution',
            'sample_count':len(rows),'domain_count':len(groups),'result_count':len(results),'summary':summary,
            'source_preserved':True,'all_raw_samples_preserved':True,'method':method,'derived_field_map':analysis['derived_field_map']})
    except Exception as exc:fail_bundle(bridge,request_id,exc);raise
