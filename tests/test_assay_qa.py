from pathlib import Path
import json

import pytest
from openpyxl import Workbook,load_workbook

from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.raster import file_sha
from mapgis_agent.assay_qa import AssayQAParameters,prepare_assay_qa


def source(tmp_path,rows):
    tmp_path.mkdir(parents=True,exist_ok=True)
    path=tmp_path/'assays.xlsx';book=Workbook();sheet=book.active;sheet.title='Lab'
    sheet.append(['SAMPLE','BATCH','X','Y','Cu','UNIT','DL','QL','QUAL'])
    for row in rows:sheet.append(row)
    book.save(path);book.close();return path


def parameters(**changes):
    return {'sheet':'Lab','id_fields':['SAMPLE'],'batch_fields':['BATCH'],'coordinate_fields':['X','Y'],
        'analytes':[{'name':'Cu','value_field':'Cu','quantity':'mass_fraction','measurement_basis':'dry sample',
            'unit_field':'UNIT','target_unit':'ppm','detection_limit_field':'DL','quantitation_limit_field':'QL',
            'qualifier_field':'QUAL','qualifier_states':{'D':'measured','ND':'left','I':'interval','R':'rejected','NA':'not_analyzed','J':'estimated'},
            'qualified_bound_source':'detection_limit','token_states':{'NA':'not_analyzed'}}],**changes}


def applied(tmp_path,rows,p=None):
    path=source(tmp_path,rows);digest=file_sha(path);bridge=Bridge(tmp_path/'agent')
    result=prepare_assay_qa(bridge,str(path),digest,p or parameters(),'qa')
    output=bridge.apply('qa')['output'];report=json.loads(Path(output).with_name('assay-qa.json').read_text(encoding='utf8'))
    assert file_sha(path)==digest
    return bridge,path,report,Path(output),result


def test_qualified_limits_units_missing_zero_and_interval_are_distinct(tmp_path):
    rows=[['a','labA',0,1,0,'ppm',.1,.5,'D'],['b','labA',1,1,'<0.1','%',.1,.5,None],
        ['c','labB',2,1,None,'ug/g',.2,.8,'ND'],['d','labB',3,1,.3,'mg/kg',.2,.8,'I'],
        ['e','labB',4,1,None,'ppm',.2,.8,None],['f','labB',5,1,'NA','ppm',.2,.8,None],
        ['g','labB',6,1,999,'ppm',.2,.8,'R'],['h','labB',7,1,.3,'ppm',.2,.8,'J']]
    bridge,path,report,output,_=applied(tmp_path,rows)
    pairs=report['measurements']
    assert [p['state'] for p in pairs]==['measured','left','left','interval','missing','not_analyzed','rejected','estimated']
    assert pairs[0]['normalized_value']==0 and pairs[0]['usable_exact'] is True
    assert pairs[1]['upper_bound']==1000 and pairs[1]['upper_closed'] is False
    assert pairs[2]['upper_bound']==.2 and pairs[2]['reported_value'] is None
    assert (pairs[3]['lower_bound'],pairs[3]['upper_bound'])==(.2,.8)
    assert pairs[3]['normalized_value'] is None
    assert len(report['batches'])==2 and report['source_preserved']
    wb=load_workbook(output,read_only=True,data_only=False)
    try:
        assert wb.sheetnames==['Raw','Wide','MapReady','Measurements','Issues','Batches']
        wide=list(wb['Wide'].values);assert wide[1][-4]==0
        assert isinstance(wide[2][-4],str) and wide[2][-4].startswith('<')
        assert list(wb['MapReady'].values)[1][6]=='0'
    finally:wb.close()
    assert prepare_assay_qa(bridge,str(path),file_sha(path),parameters(),'qa')['state']=='APPLIED'


@pytest.mark.parametrize(('unit','value','expected'),[('%',.01,100),('mg/kg',3,3),('ug/g',4,4),('g/t',5,5),
    ('ppb',6000,6),('ng/g',7000,7),('g/kg',.008,8),('kg/kg',.000009,9),('µg/g',10,10)])
def test_explicit_mass_fraction_conversion(tmp_path,unit,value,expected):
    _,_,r,_,_=applied(tmp_path,[['a','A',1,2,value,unit,None,None,None]])
    p=r['measurements'][0];assert p['normalized_value']==pytest.approx(expected,rel=1e-15)
    assert p['input_unit']==unit and p['measurement_basis']=='dry sample'


def test_right_bound_unicode_estimate_and_bad_rows_remain_visible(tmp_path):
    rows=[['a','A',1,2,'≥500','ppb',None,None,None],['b','A',1,2,'≤.2','ppm',None,None,None],
        ['c','A',1,2,'2,500','ppm',None,None,None],['d','A',1,2,-99,'ppm',None,None,None],
        ['e','A',1,2,'ND','ppm',None,None,None],['f','A',1,2,3,'wt percent',None,None,None]]
    _,_,r,_,_=applied(tmp_path,rows);m=r['measurements']
    assert m[0]['state']=='right' and m[0]['lower_bound']==.5 and m[0]['lower_closed']
    assert m[1]['upper_bound']==.2 and m[1]['upper_closed']
    assert all(not p['usable_exact'] for p in m)
    assert len(r['issues'])==4 and len(m)==6
    assert {i['issue'] for i in r['issues']}=={'unparsed_result','negative_measurement','unknown_unit'}


def test_duplicate_keys_and_unresolved_batch_are_flagged_without_deletion(tmp_path):
    rows=[['a','A',1,2,1,'ppm',None,None,None],['a','A',1,2,2,'ppm',None,None,None],
        ['a','B',1,2,3,'ppm',None,None,None],['b',None,1,2,4,'ppm',None,None,None]]
    _,_,r,_,_=applied(tmp_path,rows)
    assert len(r['measurements'])==4
    assert [m['usable_exact'] for m in r['measurements']]==[False,False,True,False]
    assert {i['issue'] for i in r['issues']}=={'duplicate_sample_key','missing_batch_metadata'}


def test_conflicting_qualifier_and_inline_censoring_and_reversed_intervals(tmp_path):
    rows=[['a','A',1,2,'>1','ppm',.1,.5,'ND'],['b','A',1,2,1,'ppm',2,1,'I'],
        ['c','A',1,2,None,'ppm',None,None,'ND'],['d','A',1,2,.2,'ppm',.1,.5,'unknown']]
    _,_,r,_,_=applied(tmp_path,rows)
    assert len(r['measurements'])==4 and all(m['quality']=='invalid' for m in r['measurements'])
    assert {i['issue'] for i in r['issues']}=={'qualifier_conflict','invalid_interval','missing_censor_bound','unknown_qualifier'}


def test_no_density_or_oxide_conversion_is_guessed(tmp_path):
    _,_,r,_,_=applied(tmp_path,[['a','A',1,2,1,'mg/L',None,None,None]])
    assert r['measurements'][0]['normalized_value'] is None
    assert r['issues'][0]['issue']=='unit_dimension_mismatch'
    p=parameters();p['analytes'][0].update(quantity='mass_concentration',input_unit='mg/L',unit_field=None,target_unit='ug/L')
    _,_,r,_,_=applied(tmp_path/'water',[['a','A',1,2,1,'mg/L',None,None,None]],p)
    assert r['measurements'][0]['normalized_value']==1000


def test_literal_formula_and_stale_sources_fail_without_request(tmp_path):
    path=source(tmp_path,[['a','A',1,2,'=1+2','ppm',None,None,None]]);bridge=Bridge(tmp_path/'agent')
    with pytest.raises(BridgeError,match='FORMULA'):
        prepare_assay_qa(bridge,str(path),file_sha(path),parameters(),'formula')
    with pytest.raises(BridgeError,match='STALE'):
        prepare_assay_qa(bridge,str(path),'0'*64,parameters(),'stale')
    with bridge.connect() as db:assert db.execute('SELECT COUNT(*) FROM commands').fetchone()[0]==0


def test_unknown_qualifiers_are_not_assumed_and_parameter_ambiguities_fail():
    p=parameters();p['analytes'][0]['input_unit']='ppm'
    with pytest.raises(ValueError,match='unit'):
        AssayQAParameters.model_validate(p)


def test_out_of_float_range_remains_an_issue_and_blank_is_not_missing(tmp_path):
    rows=[['a','A',1,2,'1e9999999','ppm',None,None,None],
        ['b','A',1,2,'1e-9999999','ppm',None,None,None],
        ['c','A',1,2,'   ','ppm',None,None,None],['d','A',1,2,False,'ppm',None,None,None]]
    _,_,r,_,_=applied(tmp_path,rows)
    assert [m['state'] for m in r['measurements']]==['measured','measured','blank','invalid']
    assert [i['issue'] for i in r['issues']].count('numeric_range')==2
    assert all(not m['usable_exact'] for m in r['measurements'])


def test_explicit_unknown_unit_and_absent_batch_metadata_need_no_fake_source_columns(tmp_path):
    p=parameters(batch_fields=[])
    p['analytes'][0].update(input_unit='UNKNOWN',unit_field=None)
    _,_,r,_,_=applied(tmp_path,[['a','unused',1,2,10,'unused',None,None,None]],p)
    assert r['measurements'][0]['normalized_value'] is None
    assert r['measurements'][0]['state']=='measured'
    assert set(r['measurements'][0]['issues'])=={'unknown_unit','missing_batch_metadata'}
    assert r['batches'][0]['metadata_complete'] is False
