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


@pytest.mark.parametrize('parameters',[{'formats':['exe']},{'formats':['pdf','pdf']},
    {'formats':['tiff'],'dpi':0},{'formats':['pdf'],'dpi':True}])
def test_eps_export_refuses_invalid_format_before_creating_request(tmp_path,parameters):
    eps=tmp_path/'map.eps';digest=sample_eps(eps);bridge=Bridge(tmp_path)
    with pytest.raises(BridgeError):prepare_plot_export(bridge,str(eps),digest,parameters,'invalid')
    with pytest.raises(BridgeError,match='Unknown request_id'):bridge.status('invalid')
