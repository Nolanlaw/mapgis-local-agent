from pathlib import Path
import cv2
import numpy as np
import pytest

from mapgis_agent.core import Bridge,BridgeError,sha
from mapgis_agent.plot_export import prepare_plot_export


def sample_eps(path):
    path.write_bytes(b'%!PS-Adobe-3.0 EPSF-3.0\n%%HiResBoundingBox: 0.00 0.00 144.00 72.00\n%%BoundingBox: 0 0 144 72\n'
        b'1 0 0 setrgbcolor 0 0 72 72 rectfill\n0 0 1 setrgbcolor 72 0 72 72 rectfill\nshowpage\n%%EOF\n')
    return sha(path.read_bytes())


def test_eps_export_pdf_tiff_actual_page_and_pixels(tmp_path):
    from mapgis_agent.native_plot import _ghostscript
    root=Path(__file__).resolve().parents[1]
    executable=_ghostscript(root)
    (tmp_path/'config.local.json').write_text('{}',encoding='utf8')
    eps=tmp_path/'map.eps';digest=sample_eps(eps);bridge=Bridge(tmp_path)
    receipt=prepare_plot_export(bridge,str(eps),digest,{'formats':['pdf','tiff'],'dpi':144},'exports')
    assert receipt['checks']['tiff']['size_px']==[288,144]
    assert receipt['checks']['pdf']['size_px_72dpi']==[144,72]
    assert receipt['checks']['pdf']['preview_matches_eps']
    output=bridge.apply('exports');folder=Path(output['output']).parent
    assert (folder/'map.pdf').read_bytes().startswith(b'%PDF-')
    pixels=cv2.imdecode(np.frombuffer((folder/'map.tif').read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    assert np.all(pixels[20:120,20:120]==[0,0,255])
    assert np.all(pixels[20:120,164:264]==[255,0,0])
    assert sha(eps.read_bytes())==digest and bridge.status('exports')['output_matches']
    again=prepare_plot_export(bridge,str(eps),digest,{'formats':['pdf','tiff'],'dpi':144},'exports')
    assert again['state']=='APPLIED'


def test_eps_export_jpeg_uses_real_page_and_preserves_colors(tmp_path):
    eps=tmp_path/'map.eps';digest=sample_eps(eps);bridge=Bridge(tmp_path)
    p=prepare_plot_export(bridge,str(eps),digest,{'formats':['jpeg'],'dpi':144},'jpeg')
    output=bridge.apply('jpeg')
    assert Path(output['output']).suffix=='.jpg'
    image=cv2.imdecode(np.frombuffer(Path(output['output']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    assert image.shape==(144,288,3)
    assert np.max(np.abs(image[20:120,20:120].astype(int)-[0,0,255]))<=3
    assert np.max(np.abs(image[20:120,164:264].astype(int)-[255,0,0]))<=3
    assert p['checks']['jpeg']['size_px']==[288,144]
    assert sha(eps.read_bytes())==digest


def test_pdf_keeps_embedded_raster_pixels_instead_of_reencoding_lossy_jpeg(tmp_path):
    y,x=np.mgrid[:256,:320]
    pixels=np.stack([(x*255/319).astype('uint8'),(y*255/255).astype('uint8'),
        (127+100*np.sin(x/9)+20*np.cos(y/5)).clip(0,255).astype('uint8')],axis=2)
    eps=tmp_path/'raster.eps'
    header=b'%!PS-Adobe-3.0 EPSF-3.0\n%%HiResBoundingBox: 0 0 144 144\n%%BoundingBox: 0 0 144 144\n'
    image=b'144 144 scale\n320 256 8 [320 0 0 -256 0 256]\ncurrentfile /ASCIIHexDecode filter false 3 colorimage\n'
    eps.write_bytes(header+image+pixels.tobytes().hex().encode('ascii')+b'>\nshowpage\n%%EOF\n')
    receipt=prepare_plot_export(Bridge(tmp_path),str(eps),sha(eps.read_bytes()),{'formats':['pdf'],'dpi':300},'raster-pdf')
    assert receipt['checks']['pdf']['preview_matches_eps'] and receipt['checks']['pdf']['mean_channel_difference']<.1


def test_pdf_curved_tiling_rechecks_print_pixels_without_relaxing_visual_gate(tmp_path):
    from mapgis_agent.native_plot import _ghostscript, normalize_native_ps
    paper = [330., 150.]
    source = tmp_path/'curves.ps'
    chunks = ['\n%%BoundingBox: 0 0 935.43 425.20\n<</PageSize [935.43 425.20]>>setpagedevice\n72 2540 div dup scale\n']
    for i in range(4):
        paint = '0 1 1 0 setcmykcolor 89 setlinewidth 1 setlinecap 1 setlinejoin\n'
        if i in (1, 2):
            paint += '1 0 1 0 setcmykcolor newpath 750 750 450 0 360 arc fill\n'
        if i != 1:
            paint += f'0 1 1 0 setcmykcolor newpath 750 750 450 {"30 150" if i == 0 else "0 360"} arc stroke\n'
        chunks.append('<< /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 1500 1500] /XStep 1500 /YStep 1500\n'
            '/PaintProc { begin 0 0 0 0 setcmykcolor 0 0 1500 1500 rectfill\n'+paint+'end } >> matrix makepattern setpattern\n'
            f'{1500+7500*i} 1500 6000 4500 rectfill\n')
    source.write_text(''.join(chunks)+'showpage\n%EOF', encoding='ascii')
    eps = tmp_path/'curves.eps'
    normalize_native_ps(_ghostscript(tmp_path), tmp_path, source, eps, paper)
    result = prepare_plot_export(Bridge(tmp_path), str(eps), sha(eps.read_bytes()), {'formats': ['pdf'], 'dpi': 300}, 'curved-pdf')
    comparison = result['checks']['pdf']
    assert comparison['comparison_dpi'] == 300
    assert comparison['screen_comparison']['pixel_mean_absolute_difference'] > 1
    assert comparison['mean_channel_difference'] <= 1 and comparison['fraction_within_5'] >= .99
    assert comparison['unmatched_ink_pixels_beyond_one_pixel'] == 0


@pytest.mark.parametrize('defect', ['missing_region', 'changed_fill'])
def test_pdf_print_recheck_rejects_changed_color_and_missing_region(tmp_path, monkeypatch, defect):
    import json
    import mapgis_agent.plot_export as export
    source = tmp_path/'original.eps'
    digest = sample_eps(source)
    broken = tmp_path/'broken.eps'
    color = b'0 1 0' if defect == 'changed_fill' else b'1 0 0'
    second = b'0 0 1 setrgbcolor 72 0 72 72 rectfill\n' if defect == 'changed_fill' else b''
    broken.write_bytes(b'%!PS-Adobe-3.0 EPSF-3.0\n%%HiResBoundingBox: 0 0 144 72\n%%BoundingBox: 0 0 144 72\n'
        +color+b' setrgbcolor 0 0 72 72 rectfill\n'+second+b'showpage\n%%EOF\n')
    run = export.subprocess.Popen
    def changed(command, *args, **kwargs):
        if '-sDEVICE=pdfwrite' in command:
            command = [*command[:-1], str(broken)]
        return run(command, *args, **kwargs)
    monkeypatch.setattr(export.subprocess, 'Popen', changed)
    with pytest.raises(BridgeError, match='PLOT_EXPORT_PDF_VISUAL_CHANGE'):
        prepare_plot_export(Bridge(tmp_path), str(source), digest, {'formats': ['pdf'], 'dpi': 300}, 'broken')
    checks = list((tmp_path/'work/artifacts').glob('*/pdf-comparison.json'))
    assert len(checks) == 1
    result = json.loads(checks[0].read_text(encoding='utf8'))
    assert result['comparison_dpi'] == 300
    if defect == 'missing_region':
        assert result['unmatched_ink_pixels_beyond_one_pixel'] > 0
    else:
        assert result['overlapping_mark_color_mean_difference'] > 2
    assert sha(source.read_bytes()) == digest


@pytest.mark.parametrize('parameters',[{'formats':['exe']},{'formats':['pdf','pdf']},
    {'formats':['tiff'],'dpi':0},{'formats':['pdf'],'dpi':True}])
def test_eps_export_refuses_invalid_format_before_creating_request(tmp_path,parameters):
    eps=tmp_path/'map.eps';digest=sample_eps(eps);bridge=Bridge(tmp_path)
    with pytest.raises(BridgeError):prepare_plot_export(bridge,str(eps),digest,parameters,'invalid')
    with pytest.raises(BridgeError,match='Unknown request_id'):bridge.status('invalid')
