"""Grouped background equations checked against closed-form scalar examples."""
import math
import statistics

import pytest

from test_geochemistry_statistics import run,params
from mapgis_agent.core import BridgeError


def grouped(**extra):
    return params(method='grouped',screen_sigma=100.,trim_sides='upper',
        grouped={'estimator':'mean_sd','intervals':4,'precision':'full','mode_tie':'error',**extra})


def test_grouped_centres_and_counts_reproduce_exact_integer_sample_stats(tmp_path):
    values=[0]+[1]*3+[2]*6+[3]*3+[4]
    _,data,_=run(tmp_path,values,grouped())
    result=data['results'][0];hist=result['histogram']
    assert hist['counts']==[1,3,6,3,1]
    assert hist['centres']==[0,1,2,3,4]
    assert result['centre_analysis']==statistics.mean(values)
    assert result['spread_analysis']==pytest.approx(statistics.stdev(values))
    assert result['upper_threshold_native']==pytest.approx(statistics.mean(values)+2*statistics.stdev(values))


def test_left_mode_uses_lower_bin_edges_and_partial_modal_bin_formula(tmp_path):
    _,data,_=run(tmp_path,[0]+[1]*3+[2]*6+[3]*3+[4],grouped(estimator='left_mode'))
    result=data['results'][0];hist=result['histogram']
    assert result['centre_analysis']==2 and hist['mode_bin']==2
    assert hist['left_effective_n']==7
    expected=math.sqrt((1*2.5**2+3*1.5**2+6*.25**2*.5)/(7-.5))
    assert result['spread_analysis']==pytest.approx(expected)
    assert result['upper_threshold_native']==pytest.approx(2+2*expected)
    assert result['lower_threshold_native'] is None


def test_grouped_log_threshold_is_backtransformed_as_a_whole(tmp_path):
    values=[1]+[10]*3+[100]*6+[1000]*3+[10000]
    _,data,_=run(tmp_path,values,grouped(estimator='left_mode')|{'transform':'log10'})
    result=data['results'][0]
    assert result['centre_analysis']==2
    assert result['upper_threshold_native']==pytest.approx(10**result['upper_threshold_analysis'])


def test_section_decimal_variant_records_its_rounding_instead_of_hiding_it(tmp_path):
    _,data,_=run(tmp_path,[0]+[1]*3+[2]*6+[3]*3+[4],grouped(estimator='left_mode',precision='section_decimals'))
    result=data['results'][0]
    assert result['spread_analysis']==1.424
    assert result['upper_threshold_native']==4.848
    assert result['histogram']['precision']=='section_decimals'


@pytest.mark.parametrize('values,options,reason',[
    ([1,1,1],{},'GEOCHEM_HISTOGRAM_WIDTH'),
    ([.001,.002,.003],{'precision':'section_decimals'},'GEOCHEM_HISTOGRAM_WIDTH'),
    ([0,1,2],{'intervals':2,'estimator':'left_mode'},'GEOCHEM_MODE_TIE')])
def test_undefined_legacy_fallbacks_are_not_arbitrary_background_values(tmp_path,values,options,reason):
    with pytest.raises(BridgeError,match=reason):run(tmp_path,values,grouped(**options))


def test_first_modal_bin_keeps_the_distinct_source_equation(tmp_path):
    _,data,_=run(tmp_path,[0]*8+[1]*2+[4],grouped(estimator='left_mode'))
    r=data['results'][0];mode=-.5+8/14;left_n=8*(mode+.5)
    expected=math.sqrt(8*((mode+.5)/2)**2/(left_n-.5))
    assert r['histogram']['mode_bin']==0 and r['centre_analysis']==pytest.approx(mode)
    assert r['spread_analysis']==pytest.approx(expected)


def test_explicit_lowest_bin_tie_policy_is_saved(tmp_path):
    _,data,_=run(tmp_path,[0,1,2],grouped(estimator='left_mode',intervals=2,mode_tie='lowest'))
    r=data['results'][0]
    assert r['histogram']['mode_ties']==[0,1,2] and r['histogram']['mode_bin']==0


def test_decimal_width_rounding_cannot_silently_omit_retained_samples(tmp_path):
    with pytest.raises(BridgeError,match='GEOCHEM_HISTOGRAM_COVERAGE'):
        run(tmp_path,[0,.07,.149],grouped(intervals=10,precision='section_decimals'))
