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
