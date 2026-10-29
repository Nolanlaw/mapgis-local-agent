from pathlib import Path
import cv2
import numpy as np
import rasterio
from rasterio.transform import from_origin

from mapgis_agent.core import Bridge,sha
from mapgis_agent.native_backend import _run_child
from mapgis_agent.native_raster import prepare_native_raster
from mapgis_agent.native_project import prepare_native_project
from mapgis_agent.native_plot import validate_eps,_ghostscript,_render_check
from test_native_raster import native_settings,ROOT


def test_native_project_eps_contains_actual_raster_colors(tmp_path):
    bridge=Bridge(tmp_path);executable,bas=native_settings(tmp_path)
    source=tmp_path/'colors.tif';data=np.zeros((3,24,32),np.uint8);data[0,:,:16]=255;data[2,:,16:]=255
    with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=3,dtype='uint8',crs='EPSG:32650',transform=from_origin(400,900,2,2)) as raster:raster.write(data)
    prepare_native_raster(bridge,'to_msi',str(source),sha(source.read_bytes()),{},'msi');msi=bridge.apply('msi')
    prepare_native_project(bridge,[{'file':msi['output'],'expected_sha256':msi['candidate_sha256']}],
        {'name':'colors.mpj','coordinate_frame':'native'},'project');project=bridge.apply('project')
    view=tmp_path/'view.txt';view.write_text('400 852 464 900 160 120\n',encoding='ascii')
    output=tmp_path/'map.eps'
    _,events=_run_child([str(executable),'project','plot',str(bas.parent),project['output'],str(view),str(output)],tmp_path,'project-plot.json')
    assert any(e.get('event')=='layer_drawn' and e['kind']==11 and e['records']==1 for e in events)
    validate_eps(output,[160,120]);_render_check(_ghostscript(ROOT),tmp_path,output,[160,120])
    pixels=cv2.imdecode(np.frombuffer((tmp_path/'preview.png').read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    height,width=pixels.shape[:2]
    left=pixels[height//4:height*3//4,width//8:width*3//8]
    right=pixels[height//4:height*3//4,width*5//8:width*7//8]
    assert np.mean((left[:,:,2]>240)&(left[:,:,0]<15)&(left[:,:,1]<15))>.99
    assert np.mean((right[:,:,0]>240)&(right[:,:,2]<15)&(right[:,:,1]<15))>.99


def test_project_plot_bundle_uses_native_polygon_walk_and_survives_stage_loss(tmp_path):
    from shapely.geometry import box,mapping
    from mapgis_agent.creation import prepare_creation
    from mapgis_agent.native_project import prepare_project_plot
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    local={'kind':'local','name':'native plot square','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}
    prepare_creation(bridge,{'type':'FeatureCollection','features':[{'type':'Feature','id':'square','geometry':mapping(box(0,0,10,10)),
        'properties':{'ID':1},'style':{'fill_color':8,'pattern':0}}]},{'name':'black.wp','crs':local,'map_scale':1000},'square')
    square=bridge.apply('square')
    prepare_native_project(bridge,[{'file':square['output'],'expected_sha256':square['candidate_sha256']}],
        {'name':'square.mpj','coordinate_frame':'native'},'project');project=bridge.apply('project')
    preview=prepare_project_plot(bridge,project['output'],project['candidate_sha256'],
        {'native_bounds':[0,0,10,10],'paper_mm':[100,100],'coordinate_frame':'native'},'plot')
    assert preview['render_check']['ink_pixels']>75000
    output=bridge.apply('plot');assert bridge.status('plot')['output_matches']
    pixels=cv2.imdecode(np.frombuffer(Path(output['output']).with_name('preview.png').read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    # Native EPS color conversion is not a literal RGB palette lookup. Compare
    # with the separately loaded standalone native layer, rather than assume
    # color index 8 must become RGB(0,0,0) after Ghostscript color management.
    from mapgis_agent.native_plot import prepare_native_plot
    reference=prepare_native_plot(bridge,[{'file':square['output'],'expected_sha256':square['candidate_sha256']}],
        {'native_bounds':[0,0,10,10],'paper_mm':[100,100],'coordinate_frame':'native'},'reference')
    reference_pixels=cv2.imdecode(np.frombuffer(Path(reference['render_check']['preview']).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    assert np.array_equal(pixels,reference_pixels)
    stage=Path(output['stage']);hidden=stage.with_name(stage.name+'-hidden')
    assert stage.resolve().is_relative_to((tmp_path/'work').resolve()) and hidden.resolve().is_relative_to((tmp_path/'work').resolve())
    stage.rename(hidden)
    assert bridge.apply('plot')['reconciled_existing_output'] and bridge.status('plot')['output_matches']
