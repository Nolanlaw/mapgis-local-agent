"""Empty field arrays must close normally, without adding hidden user fields."""
from pathlib import Path
import pytest
from mapgis_agent.core import Bridge, read_native, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import native_file_info, prepare_native_operation
from test_native_raster import native_settings
from test_native_wp import LOCAL


@pytest.mark.parametrize('kind,coordinates,extension',[
    ('Point',[1.,2.],'wt'),
    ('LineString',[[0.,0.],[1.,1.],[2.,0.]],'wl'),
    ('Polygon',[[[0.,0.],[2.,0.],[2.,2.],[0.,2.],[0.,0.]]],'wp'),
])
def test_zero_field_native_inspect_resave_and_fresh_close(tmp_path,kind,coordinates,extension):
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1',
        'geometry':{'type':kind,'coordinates':coordinates},'properties':{}}]},
        {'name':'empty.'+extension,'map_scale':1000,'crs':LOCAL},'create')
    source=Path(bridge.apply('create')['output']);before=source.read_bytes()
    expected=read_native(source)[1].data
    if extension!='wp':assert expected.shape==(1,0)
    assert native_file_info(tmp_path,str(source),sha(before))['active']==1
    prepared=prepare_native_operation(bridge,'resave',str(source),sha(before),{},'save')
    output=Path(bridge.apply('save')['output'])
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    assert read_native(output)[1].data.equals(expected) and source.read_bytes()==before
