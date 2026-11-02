from pathlib import Path

import pytest
from shapely.geometry import Point, LineString, box, mapping

from mapgis_agent.core import Bridge, BridgeError, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_project import prepare_native_project, native_project_info
from test_native_project import LOCAL
from test_native_raster import native_settings


def project_case(tmp_path):
    bridge = Bridge(tmp_path)
    native_settings(tmp_path)
    inputs = []
    for i, (geometry, suffix) in enumerate([(Point(1, 2), 'wt'),
            (LineString([(0, 0), (3, 4)]), 'wl'), (box(0, 0, 10, 10), 'wp'),
            (LineString([(0, 10), (10, 0)]), 'wl')], 1):
        key = 'source-' + str(i)
        prepare_creation(bridge, {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'id': '1', 'geometry': mapping(geometry), 'properties': {'ID': i}}]},
            {'name': key + '.' + suffix, 'crs': LOCAL, 'map_scale': 1000}, key)
        output = Path(bridge.apply(key)['output'])
        inputs.append({'file': str(output), 'expected_sha256': sha(output.read_bytes())})
    prepare_native_project(bridge, inputs[:3], {'name': 'map.mpj', 'coordinate_frame': 'native'}, 'initial')
    project = Path(bridge.apply('initial')['output'])
    return bridge, project, inputs


def test_existing_project_layer_delete_reorder_append_and_native_state_survive_relocation(tmp_path):
    from mapgis_agent.native_project import prepare_project_edit
    bridge, source, inputs = project_case(tmp_path)
    original = source.read_bytes()
    original_members=set(source.parent.iterdir())
    frozen = {Path(layer['file']): Path(layer['file']).read_bytes() for layer in native_project_info(
        tmp_path, str(source), sha(original))['layers']}
    plan = [{'source_index': 3, 'state': 0}, {'source_index': 1, 'state': 2},
            {**inputs[3], 'state': 3}]
    prepared = prepare_project_edit(bridge, str(source), sha(original), plan, 'edit-project')
    result = bridge.apply('edit-project')
    assert prepared['portable_relocation'] == 'PASS_WITH_OLD_DIRECTORY_HIDDEN'
    assert prepared['native_execution']['pid'] != prepared['native_reopen']['pid']
    assert bridge.status('edit-project')['output_matches']
    info = native_project_info(tmp_path, result['output'], result['candidate_sha256'])
    assert [layer['kind'] for layer in info['layers']] == [2, 1, 0]
    assert [layer['state'] for layer in info['layers']] == [0, 2, 3]
    assert [layer['sha256'] for layer in info['layers']] == [inputs[i]['expected_sha256'] for i in [2, 0, 3]]
    assert all(Path(layer['file']).parent == Path(result['output']).parent for layer in info['layers'])
    assert source.read_bytes() == original and all(path.read_bytes() == data for path, data in frozen.items())
    assert set(source.parent.iterdir()) == original_members
    # Completed retries reconcile the same publication instead of reordering again.
    assert prepare_project_edit(bridge, str(source), sha(original), plan, 'edit-project')['state'] == 'APPLIED'


def test_project_edit_rejects_ambiguous_source_layer_identity_and_stale_source(tmp_path):
    from mapgis_agent.native_project import prepare_project_edit
    bridge, source, _ = project_case(tmp_path)
    original = source.read_bytes()
    for i, plan in enumerate([[{'source_index': 1}, {'source_index': 1}],
                              [{'source_index': 99}], [{'source_index': 1, 'state': 7}]]):
        with pytest.raises(BridgeError, match='PROJECT_'):
            prepare_project_edit(bridge, str(source), sha(original), plan, 'bad-' + str(i))
    with pytest.raises(BridgeError, match='STALE_SOURCE'):
        prepare_project_edit(bridge, str(source), '0' * 64, [{'source_index': 1}], 'stale')
    assert source.read_bytes() == original


def test_native_project_display_limits_save_reopen_and_control_actual_output(tmp_path):
    import cv2
    import numpy as np
    from mapgis_agent.native_project import prepare_project_edit,prepare_project_plot
    bridge,source,_=project_case(tmp_path);before=source.read_bytes()
    plan=[{'source_index':3,'state':1,'display_limits':[6,10]},
          {'source_index':2,'state':1,'display_limits':[0,0]}]
    prepared=prepare_project_edit(bridge,str(source),sha(before),plan,'limits')
    saved=bridge.apply('limits')
    info=native_project_info(tmp_path,saved['output'],saved['candidate_sha256'])
    assert [x['display_limits'] for x in info['layers']]==[[6,10],[0,0]]
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    for identity,paper,visible in [('off',[100,100],False),('on',[200,200],True)]:
        preview=prepare_project_plot(bridge,saved['output'],saved['candidate_sha256'],
            {'native_bounds':[0,0,20,20],'paper_mm':paper,'coordinate_frame':'native'},identity)
        output=bridge.apply(identity)
        pixels=cv2.imdecode(np.frombuffer(Path(output['output']).with_name('preview.png').read_bytes(),np.uint8),1)
        height,width=pixels.shape[:2]
        assert bool(np.any(pixels[int(.75*height),int(.25*width)]<245))==visible
        assert preview['native_display_scale']==paper[0]/20
    assert source.read_bytes()==before


@pytest.mark.parametrize('limits',[[10,2],[float('nan'),1],[True,2],[1]])
def test_project_display_limits_reject_invalid_ranges(tmp_path,limits):
    from mapgis_agent.native_project import prepare_project_edit
    bridge,source,_=project_case(tmp_path)
    with pytest.raises(BridgeError,match='PROJECT_DISPLAY_LIMITS'):
        prepare_project_edit(bridge,str(source),sha(source.read_bytes()),[{'source_index':1,'display_limits':limits}],'invalid-limits')


def test_existing_project_can_replace_saved_layer_content_without_losing_native_item_state(tmp_path):
    from mapgis_agent.native_project import prepare_project_edit
    bridge,source,inputs=project_case(tmp_path);before=source.read_bytes()
    prepared=prepare_project_edit(bridge,str(source),sha(before),[
        {'source_index':1,'state':2},
        {'source_index':2,'replacement':inputs[3],'state':3,'display_limits':[1,20]},
        {'source_index':3,'state':0}],'replace-content')
    output=bridge.apply('replace-content');actual=native_project_info(tmp_path,output['output'],output['candidate_sha256'])
    assert [r['sha256'] for r in actual['layers']]==[inputs[i]['expected_sha256'] for i in [0,3,2]]
    assert [r['state'] for r in actual['layers']]==[2,3,0]
    assert actual['layers'][1]['display_limits']==[1,20]
    assert prepared['portable_relocation']=='PASS_WITH_OLD_DIRECTORY_HIDDEN' and source.read_bytes()==before


def test_replaced_layer_outside_previous_extent_updates_native_project_range(tmp_path):
    from mapgis_agent.native_project import prepare_project_edit
    from mapgis_agent.native_backend import prepare_native_operation
    bridge,source,inputs=project_case(tmp_path)
    prepare_native_operation(bridge,'point.move',inputs[0]['file'],inputs[0]['expected_sha256'],
        {'updates':[{'record_id':1,'x':52,'y':53}]},'far-point')
    moved=bridge.apply('far-point')
    prepare_project_edit(bridge,str(source),sha(source.read_bytes()),[
        {'source_index':1,'replacement':{'file':moved['output'],'expected_sha256':moved['candidate_sha256']}},
        {'source_index':2},{'source_index':3}],'far-project')
    saved=bridge.apply('far-project');actual=native_project_info(tmp_path,saved['output'],saved['candidate_sha256'])
    assert actual['native_bounds'][2]>=52 and actual['native_bounds'][3]>=53
    assert actual['layers'][0]['native_bounds'][0]<=52<=actual['layers'][0]['native_bounds'][2]
    assert actual['layers'][0]['native_bounds'][1]<=53<=actual['layers'][0]['native_bounds'][3]


def test_msi_replacement_preserves_item_settings_and_reopens_changed_extent_pixels(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from mapgis_agent.native_raster import prepare_native_raster
    from mapgis_agent.native_project import prepare_project_edit
    bridge=Bridge(tmp_path);native_settings(tmp_path);images=[]
    for index,(x,y) in enumerate([(400,900),(-50,650)]):
        source=tmp_path/f'height-{index}.tif'
        with rasterio.open(source,'w',driver='GTiff',width=7,height=7,count=1,dtype='float32',crs='EPSG:32650',
            transform=from_origin(x,y,100,100)) as ds:ds.write((np.arange(49,dtype='float32')+index*100).reshape(1,7,7))
        prepare_native_raster(bridge,'to_msi',str(source),sha(source.read_bytes()),{},f'msi-{index}')
        images.append(bridge.apply(f'msi-{index}'))
    prepare_native_project(bridge,[{'file':images[0]['output'],'expected_sha256':images[0]['candidate_sha256']}],
        {'name':'images.mpj','coordinate_frame':'native'},'images')
    old=bridge.apply('images');before=Path(old['output']).read_bytes()
    result=prepare_project_edit(bridge,old['output'],old['candidate_sha256'],[
        {'source_index':1,'replacement':{'file':images[1]['output'],'expected_sha256':images[1]['candidate_sha256']},
         'state':0,'display_limits':[1,20]}],'replace-msi')
    saved=bridge.apply('replace-msi');info=native_project_info(tmp_path,saved['output'],saved['candidate_sha256'])
    layer=info['layers'][0]
    assert layer['kind']==11 and layer['state']==0 and layer['display_limits']==[1,20]
    assert layer['sha256']==images[1]['candidate_sha256'] and layer['native_bounds']==pytest.approx([-50,-50,650,650])
    assert Path(layer['file']+'.json').is_file() and result['portable_relocation']=='PASS_WITH_OLD_DIRECTORY_HIDDEN'
    assert Path(old['output']).read_bytes()==before
    prepare_native_raster(bridge,'to_tiff',layer['file'],layer['sha256'],{},'replaced-tiff')
    back=bridge.apply('replaced-tiff')
    with rasterio.open(back['output']) as ds:assert ds.read(1)[3,3]==124 and ds.crs.to_epsg()==32650
