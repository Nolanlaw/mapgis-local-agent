"""Explicit background estimators, independent scalar statistics and raw lineage."""
import csv
import json
import math
import statistics

from openpyxl import Workbook,load_workbook
import pytest

from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.raster import file_sha


def params(**extra):
    return {'sheet':'Raw','elements':['Cu'],'id_field':'Sample','domain_fields':[],
        'units':{'Cu':'mg/kg'},'method':'iterative_sigma','transform':'raw',
        'screen_sigma':1.5,'threshold_sigma':2.,'trim_sides':'both','minimum_n':2,**extra}


def run(folder,values,parameters=None,domains=None):
    from mapgis_agent.geochemistry_statistics import prepare_statistics
    source=folder/'source.xlsx';book=Workbook();sheet=book.active;sheet.title='Raw'
    sheet.append(['Sample','Domain','Cu'])
    for i,value in enumerate(values):sheet.append([f'S{i+1}',domains[i] if domains else 'A',value])
    book.save(source);book.close();digest=file_sha(source);bridge=Bridge(folder)
    receipt=prepare_statistics(bridge,str(source),digest,parameters or params(),'stats')
    output=bridge.apply('stats')['output'];assert file_sha(source)==digest
    return receipt,json.loads(open(output,encoding='utf8').read()),output


def test_sigma_removal_trace_and_strict_ties_preserve_constant_background(tmp_path):
    _,data,path=run(tmp_path,[10,10,10,10,11,12,100])
    result=data['results'][0]
    assert result['retained_worksheet_rows']==[2,3,4,5]
    assert [r['removed_worksheet_rows'] for r in result['rounds']]==[[8],[7],[6],[]]
    assert result['upper_threshold_native']==10 and result['sample_sd_analysis']==0
    samples=list(csv.DictReader(open(str(path).replace('analysis.json','samples.csv'),encoding='utf-8-sig')))
    assert len(samples)==7 and [int(r['class']) for r in samples]==[0,0,0,0,1,1,1]


def test_domains_have_independent_thresholds_and_all_rows_remain(tmp_path):
    _,data,path=run(tmp_path,[1,2,3,100,200,300],params(domain_fields=['Domain'],screen_sigma=5.),
        domains=['A']*3+['B']*3)
    results=data['results'];assert len(results)==2
    assert results[0]['upper_threshold_native']==4
    assert results[1]['upper_threshold_native']==400
    book=load_workbook(str(path).replace('analysis.json','analysis.xlsx'),read_only=True,data_only=False)
    assert len(list(book['Raw'].values))==7
    assert len(list(book['Classified'].values))==7;book.close()


def test_log_threshold_is_antilog_of_mean_plus_sd_in_log_domain(tmp_path):
    _,data,_=run(tmp_path,[1,10,100,1000],params(transform='log10',screen_sigma=5.))
    r=data['results'][0];expected=10**(statistics.mean([0,1,2,3])+2*statistics.stdev([0,1,2,3]))
    assert r['upper_threshold_native']==pytest.approx(expected,rel=2e-15)
    assert r['background_centre_native']==pytest.approx(10**1.5)
    assert r['sample_sd_analysis']==pytest.approx(math.sqrt(5/3))


def test_mad_scale_is_explicit_and_not_confused_with_sample_sd(tmp_path):
    p=params(method='median_mad',screen_sigma=None,mad_scale=1.,threshold_sigma=2.)
    _,data,_=run(tmp_path,[1,2,3,4,100],p);r=data['results'][0]
    assert r['centre_analysis']==3 and r['mad_unscaled_analysis']==1
    assert r['lower_threshold_native']==1 and r['upper_threshold_native']==5
    assert r['background_n']==5 and len(r['rounds'])==1


def test_quantile_interpolation_and_units_unknown_are_explicit(tmp_path):
    p=params(method='quantile',screen_sigma=None,threshold_sigma=None,quantile=.75,units={'Cu':None})
    _,data,_=run(tmp_path,[1,2,3,4,100],p);r=data['results'][0]
    assert r['lower_threshold_native']==2 and r['upper_threshold_native']==4
    assert r['unit'] is None and data['method']['quantile_interpolation']=='linear'


@pytest.mark.parametrize('values,p,reason',[
    ([0,1,2],params(transform='log10'),'GEOCHEM_LOG_POSITIVE'),
    ([None,1,2],params(),'GEOCHEM_FINITE_MEASUREMENTS'),
    (['<0.01',1,2],params(),'GEOCHEM_FINITE_MEASUREMENTS'),
    ([True,1,2],params(),'GEOCHEM_FINITE_MEASUREMENTS'),
    ([1,2,3],params(units={}), 'GEOCHEM_UNITS'),
    ([10,10,10,10,11,12,100],params(max_iterations=1),'GEOCHEM_ITERATION_LIMIT'),
    (['=1+1',2,3],params(),'GEOCHEM_FORMULA')])
def test_unresolved_measurements_and_incomplete_iterations_are_not_results(tmp_path,values,p,reason):
    with pytest.raises(BridgeError,match=reason):run(tmp_path,values,p)


def test_threshold_overflow_is_rejected_instead_of_inf_or_underflow_zero(tmp_path):
    with pytest.raises(BridgeError,match='GEOCHEM_THRESHOLD_RANGE'):
        run(tmp_path,[1e-300,1e300,1e300],params(transform='log10',screen_sigma=5.))


@pytest.mark.parametrize('value',[1e300,1e-300,0.])
def test_constant_large_tiny_and_zero_measurements_do_not_overflow_variance(tmp_path,value):
    _,data,_=run(tmp_path,[value]*4,params(screen_sigma=5.))
    r=data['results'][0];assert r['background_n']==4
    assert r['upper_threshold_native']==value and r['sample_sd_analysis']==0.
