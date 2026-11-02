import json
import numpy as np
import pytest
import rasterio
from affine import Affine
from mapgis_agent.core import BridgeError
from mapgis_agent.raster import file_sha
from mapgis_agent.tin_scene import _texture_description,FORMAT,_scene_source,_camera_bytes,NativeTinCamera

FRAME={'definition':'EPSG:4549','axis_order':'xy'}
ORIGIN=[500000.125,3000000.375]
BOUNDS=ORIGIN+[500100.125,3000100.375]

def image(tmp_path):
    file=tmp_path/'texture.tif'
    with rasterio.open(file,'w',driver='GTiff',width=8,height=4,count=3,dtype='uint8',crs='EPSG:4549',
        transform=Affine(12.5,0,ORIGIN[0],0,-25,BOUNDS[3])) as ds:ds.write(np.full((3,4,8),120,dtype='uint8'))
    return file

def test_tin_texture_keeps_world_rgb_and_declares_only_native_display_translation(tmp_path):
    file=image(tmp_path);before=file.read_bytes();info=_texture_description(file,FRAME,BOUNDS)
    assert info['display_shift']==ORIGIN and info['native_bounds']==[0.,0.,100.,100.]
    assert info['bounds']==BOUNDS and info['native']=='texture.msi' and info['source']=='texture.tif'
    assert file.read_bytes()==before
    with pytest.raises(BridgeError,match='COVERAGE'):_texture_description(file,FRAME,[*ORIGIN,500101.,BOUNDS[3]])

def test_scene_refuses_forged_texture_coordinate_binding_or_missing_pair(tmp_path):
    file=image(tmp_path);model=tmp_path/'model.tin';model.write_bytes(b'WTIN owned')
    msi=tmp_path/'texture.msi';msi.write_bytes(b'owned MSI candidate; no parser reached')
    pose=NativeTinCamera(origin=ORIGIN,scales=[1.,1.,1.],eye_model=[100.,100.,100.],target_model=[50.,50.,0.],up=[0.,0.,1.],fov_degrees=90.)
    camera=tmp_path/'camera.bin';camera.write_bytes(_camera_bytes(pose))
    record={'format':FORMAT,'native_tin':'model.tin','frame':FRAME,'camera':pose.model_dump(),
        'metrics':{'bounds':BOUNDS},'texture':_texture_description(file,FRAME,BOUNDS),
        'members':{v.name:file_sha(v) for v in [model,camera,file,msi]}}
    scene=tmp_path/'test.scene.json';scene.write_text(json.dumps(record))
    assert _scene_source(scene,file_sha(scene))[0]['texture']['display_shift']==ORIGIN
    record['texture']['display_shift'][0]+=1;scene.write_text(json.dumps(record))
    with pytest.raises(BridgeError,match='TEXTURE_BINDING'):_scene_source(scene,file_sha(scene))
    record['texture']=None;scene.write_text(json.dumps(record))
    with pytest.raises(BridgeError,match='TEXTURE_BINDING'):_scene_source(scene,file_sha(scene))
