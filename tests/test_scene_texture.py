import struct
from pathlib import Path
import numpy as np
import pytest
import rasterio
from affine import Affine
from mapgis_agent.core import BridgeError
from mapgis_agent.scene_texture import _texture_pixels,texture_info

FRAME={'definition':'EPSG:4549','axis_order':'xy'}
GRID={'bounds':[500000,3000000,500006,3000004]}

def image(tmp_path,**change):
    profile={'driver':'GTiff','width':6,'height':4,'count':3,'dtype':'uint8','crs':'EPSG:4549','transform':Affine(1,0,500000,0,-1,3000004)}
    profile.update(change);file=tmp_path/'rgb.tif'
    with rasterio.open(file,'w',**profile) as ds:ds.write(np.zeros((profile['count'],profile['height'],profile['width']),dtype='uint8'))
    return file

def test_registered_black_rgb_remains_valid_and_original_pixels_are_unchanged(tmp_path):
    file=image(tmp_path);before=file.read_bytes()
    values,profile,info=_texture_pixels(file,FRAME,GRID)
    assert values.shape==(3,4,6) and not values.any()
    assert profile['transform']==Affine(1,0,500000,0,-1,3000004)
    assert info['bounds']==GRID['bounds'] and file.read_bytes()==before

@pytest.mark.parametrize(('change','error'),[
    ({'count':4},'SCENE_TEXTURE_RGB'),
    ({'nodata':0},'SCENE_TEXTURE_MISSING_PIXELS'),
    ({'crs':'EPSG:3857'},'SCENE_TEXTURE_CRS_MISMATCH'),
    ({'transform':Affine(1,.1,500000,0,-1,3000004)},'SCENE_TEXTURE_RECTIFIED_AFFINE_REQUIRED'),
    ({'transform':Affine(1,0,500001,0,-1,3000004)},'SCENE_TEXTURE_COVERAGE'),
    ({'width':1025},'SCENE_TEXTURE_SIZE'),
])
def test_texture_does_not_guess_rgb_validity_registration_or_resolution(tmp_path,change,error):
    file=image(tmp_path,**change)
    with pytest.raises(BridgeError,match=error):_texture_pixels(file,FRAME,GRID)

def original_texture(tmp_path):
    # Header created by the installed original TexDbs from synthetic input.
    blob=bytes.fromhex((Path(__file__).parent/'fixtures/native_texture_header.hex').read_text())
    (tmp_path/'texture.tdb').write_bytes(blob);(tmp_path/'texture_DAT').mkdir()
    (tmp_path/'texture_DAT/Dat0.py').write_bytes(bytes(98304))
    return {'texture.tdb':'a'*64,'texture_DAT/Dat0.py':'b'*64}

def test_original_native_header_keeps_full_terrain_domain_and_relative_data(tmp_path):
    members=original_texture(tmp_path)
    info=texture_info(tmp_path,members,{'bounds':[500005,3000005,500645,3000485]})
    assert info['tile_width']==128 and info['tile_height']==128
    assert info['tiles']==2 and info['tile_bytes']==49152
    assert info['bounds']==[500005,3000005,500645,3000485]

@pytest.mark.parametrize(('offset','format','value','error'),[
    (334,'H',2048,'SCENE_TEXTURE_PROFILE'),
    (346,'h',32767,'SCENE_TEXTURE_PROFILE'),
    (348,'I',0x7fffffff,'SCENE_TEXTURE_PROFILE'),
    (352,'I',2,'SCENE_TEXTURE_PROFILE'),
    (356,'I',2,'SCENE_TEXTURE_TILES'),
    (377,'I',1,'SCENE_TEXTURE_INDEX'),
])
def test_old_native_reader_never_receives_oversized_or_inconsistent_tables(tmp_path,offset,format,value,error):
    members=original_texture(tmp_path);file=tmp_path/'texture.tdb';blob=bytearray(file.read_bytes())
    struct.pack_into('<'+format,blob,offset,value);file.write_bytes(blob)
    with pytest.raises(BridgeError,match=error):texture_info(tmp_path,members)

def test_external_image_names_truncated_data_and_wrong_terrain_frame_are_refused(tmp_path):
    members=original_texture(tmp_path);file=tmp_path/'texture.tdb';blob=file.read_bytes()
    changed=bytearray(blob);changed[12:26]=b'../private.msi\0';file.write_bytes(changed)
    with pytest.raises(BridgeError,match='SCENE_TEXTURE_IMAGE_NAME'):texture_info(tmp_path,members)
    file.write_bytes(blob)
    with pytest.raises(BridgeError,match='SCENE_TEXTURE_TERRAIN_ALIGNMENT'):texture_info(tmp_path,members,GRID)
    (tmp_path/'texture_DAT/Dat0.py').write_bytes(b'bad')
    with pytest.raises(BridgeError,match='SCENE_TEXTURE_DATA_LENGTH'):texture_info(tmp_path,members)
