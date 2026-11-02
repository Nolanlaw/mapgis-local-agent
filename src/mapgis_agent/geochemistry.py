"""Reproducible upper-boxplot screening and anomaly thresholds from raw XLSX."""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
from openpyxl import Workbook

from .artifacts import begin_bundle,finish_bundle,fail_bundle
from .core import Bridge,BridgeError,json_text
from .tables import read_table


def prepare_geochemistry(bridge:Bridge,file:str,expected_sha256:str,parameters:dict,request_id:str):
    if set(parameters)!={'sheet','elements','id_field'}:raise BridgeError('UNEXPECTED_PARAMETERS')
    path,table=read_table(file,expected_sha256,parameters['sheet'])
    elements=parameters['elements'];columns=table['columns'];id_field=parameters['id_field']
    if (not isinstance(elements,list) or not elements or len(set(elements))!=len(elements)
            or any(e not in columns for e in [*elements,id_field])):raise BridgeError('GEOCHEM_FIELDS')
    ids=[r[columns.index(id_field)] for r in table['rows']]
    if any(i is None for i in ids):raise BridgeError('GEOCHEM_SAMPLE_ID_REQUIRED')
    results={};summary={}
    for element in elements:
        values=[r[columns.index(element)] for r in table['rows']]
        if len(values)<2 or any(type(v) not in (int,float) for v in values):raise BridgeError('GEOCHEM_FINITE_NUMERIC_VALUES_REQUIRED')
        raw=np.array(values,dtype=np.float64);active=np.arange(len(raw));rounds=[]
        while True:
            current=raw[active]
            if len(current)<2:raise BridgeError('GEOCHEM_BACKGROUND_TOO_SMALL')
            q1,median,q3=np.quantile(current,[.25,.5,.75],method='linear')
            fence=q3+1.5*(q3-q1)
            removed=current>fence
            rounds.append({'round':len(rounds),'n':len(current),'q1':float(q1),'median':float(median),'q3':float(q3),
                           'upper_fence':float(fence),'removed_n':int(removed.sum()),'removed_ids':[ids[i] for i in active[removed]],
                           'removed_worksheet_rows':[table['worksheet_rows'][i] for i in active[removed]]})
            if not np.any(removed):break
            active=active[~removed]
        current=raw[active];mean=float(np.mean(current));sd=float(np.std(current,ddof=1));threshold=mean+2*sd
        summary[element]={'raw_n':len(raw),'background_n':len(active),'removed_n':len(raw)-len(active),
                          'mean':mean,'sample_sd':sd,'C':threshold,'bands_124':[threshold,2*threshold,4*threshold],
                          'bands_1416':[threshold,4*threshold,16*threshold]}
        results[element]=summary[element]|{'rounds':rounds,'retained_ids':[ids[i] for i in active],
                                          'retained_worksheet_rows':[table['worksheet_rows'][i] for i in active]}
    payload={'operation':'geochemistry.background','file':str(path),'expected_sha256':expected_sha256,'parameters':parameters}
    stage,receipt,new=begin_bundle(bridge,payload,request_id,[{'file':str(path),'sha256':expected_sha256}],'analysis.json')
    if not new:return receipt
    try:
        files=stage/'files';files.mkdir()
        analysis={'schema':'mapgis-agent/geochemistry/1','source':str(path),'source_sha256':expected_sha256,
                  'sheet':table['sheet'],'sample_count':len(table['rows']),
                  'method':{'screening':'iterative upper fence Q3+1.5*IQR; raw values strictly above the fence removed',
                            'quartiles':'linear inclusive','standard_deviation':'sample; ddof=1','threshold':'mean+2*sample_sd',
                            'normality_tested':False,'map_input':'all original unfiltered samples'},'elements':results}
        (files/'analysis.json').write_text(json_text(analysis),encoding='utf8')
        header=['Element','Raw N','Background N','Mean','Sample SD','C','2C','4C','16C']
        rows=[[e,s['raw_n'],s['background_n'],s['mean'],s['sample_sd'],s['C'],2*s['C'],4*s['C'],16*s['C']] for e,s in summary.items()]
        with (files/'summary.csv').open('x',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(header);writer.writerows(rows)
        book=Workbook(write_only=True)
        raw_sheet=book.create_sheet('Raw');raw_sheet.append(columns)
        for row in table['rows']:raw_sheet.append(row)
        summary_sheet=book.create_sheet('Summary');summary_sheet.append(header)
        for row in rows:summary_sheet.append(row)
        book.save(files/'analysis.xlsx');book.close()
        return finish_bundle(bridge,request_id,files,{'backend':'openpyxl/NumPy','summary':summary,'source_preserved':True,
            'raw_samples_preserved':True,'method':analysis['method']})
    except Exception as exc:
        fail_bundle(bridge,request_id,exc)
        raise
