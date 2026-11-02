from pathlib import Path
import pytest
from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.native_plot import prepare_native_plot,validate_eps

@pytest.mark.parametrize('view',[{'native_bounds':[-1e308,0,1e308,10],'paper_mm':[100,100],'coordinate_frame':'native'}, {}, {'native_bounds':[0,0,0,10],'paper_mm':[100,100],'coordinate_frame':'native'}, {'native_bounds':[0,0,10,10],'paper_mm':[float('nan'),100],'coordinate_frame':'native'}])
def test_plot_requires_explicit_finite_frame(tmp_path,view):
    with pytest.raises(BridgeError,match='PLOT_VIEW'):
        prepare_native_plot(Bridge(tmp_path),[],view,'invalid')

def test_plot_rejects_truncated_or_wrong_page_eps(tmp_path):
    p=tmp_path/'candidate.eps'
    p.write_bytes(b'%!PS-Adobe-3.0 EPSF-3.0\n%%HiResBoundingBox: 0 0 283.46 283.46\n')
    with pytest.raises(BridgeError,match='EPS_INCOMPLETE'):
        validate_eps(p,[100,100])
    p.write_bytes(p.read_bytes()+b'%%EOF')
    with pytest.raises(BridgeError,match='EPS_PAGE'):
        validate_eps(p,[140,80])


def test_dense_native_tiling_is_checked_at_print_resolution_when_screen_pixels_alias(tmp_path):
    from mapgis_agent.native_plot import _ghostscript,normalize_native_ps
    paper=[210.88535365766916,268.04247962841]
    source=tmp_path/'dense.ps'
    source.write_text('''\n%%BoundingBox: 0.00 0.00 597.79 759.81
<</PageSize [597.79 759.81]>>setpagedevice
72 2540 div dup scale
<< /PatternType 1 /PaintType 1 /TilingType 1
/BBox [0 0 200 200] /XStep 200 /YStep 200
/PaintProc { begin
0 0 0 0 setcmykcolor 0 0 200 200 rectfill
0 0 0 1 setcmykcolor 10 setlinewidth 1 setlinecap
0 50 moveto 200 50 lineto 0 100 moveto 200 100 lineto
0 150 moveto 200 150 lineto 0 0 moveto 200 0 lineto stroke
50 0 moveto 50 50 lineto 150 0 moveto 150 50 lineto
0 50 moveto 0 100 lineto 100 50 moveto 100 100 lineto
50 100 moveto 50 150 lineto 150 100 moveto 150 150 lineto
0 150 moveto 0 200 lineto 100 150 moveto 100 200 lineto stroke
end } >> matrix makepattern setpattern
7569.86 10169.86 1000 10000 rectfill
16569.86 10169.86 1000 10000 rectfill
showpage
%EOF''',encoding='ascii')
    result=normalize_native_ps(_ghostscript(tmp_path),tmp_path,source,tmp_path/'dense.eps',paper)
    assert result['comparison_dpi']==300
    assert result['screen_comparison']['pixel_mean_absolute_difference']>1
    assert result['unmatched_ink_pixels_beyond_one_pixel']==0
    assert result['pixel_mean_absolute_difference']<=1


@pytest.mark.parametrize('defect',['missing_region','changed_fill'])
def test_print_recheck_still_rejects_real_missing_marks_or_changed_colors(tmp_path,monkeypatch,defect):
    import mapgis_agent.native_plot as plot
    source=tmp_path/'original.ps'
    source.write_text('''
%%BoundingBox: 0 0 144 144
<</PageSize [144 144]>>setpagedevice
0 0 0 setrgbcolor 10 10 30 80 rectfill 90 10 30 80 rectfill
showpage
%EOF''',encoding='ascii')
    run=plot.subprocess.run
    def broken_conversion(command,*args,**kwargs):
        result=run(command,*args,**kwargs)
        if '-sDEVICE=eps2write' in command:
            target=Path(next(v.split('=',1)[1] for v in command if v.startswith('-sOutputFile=')))
            color='0 1 0' if defect=='changed_fill' else '0 0 0'
            second='90 10 30 80 rectfill' if defect=='changed_fill' else ''
            target.write_text('%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 144 144\n'
                '%%HiResBoundingBox: 0 0 144 144\n'+color+' setrgbcolor 10 10 30 80 rectfill '+second+'\nshowpage\n%%EOF',encoding='ascii')
        return result
    monkeypatch.setattr(plot.subprocess,'run',broken_conversion)
    with pytest.raises(BridgeError,match='POSTSCRIPT_CONVERSION_PIXELS'):
        plot.normalize_native_ps(plot._ghostscript(tmp_path),tmp_path,source,tmp_path/'wrong.eps',[50.8,50.8])
    import json
    comparison=json.loads((tmp_path/'native-ps-pixel-comparison.json').read_text(encoding='utf8'))
    assert comparison['comparison_dpi']==300
    if defect=='missing_region':assert comparison['unmatched_ink_pixels_beyond_one_pixel']>0
    else:assert comparison['overlapping_mark_color_mean_difference']>2
