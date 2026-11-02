import json
from pathlib import Path
import numpy as np
import pytest
import rasterio
from affine import Affine
from pydantic import ValidationError
from mapgis_agent.core import BridgeError
from mapgis_agent.native_scene import SceneParameters,_surface,_transport,_scene_source,FORMAT
from mapgis_agent.raster import file_sha
FRAME={'definition':'EPSG:4549','axis_order':'xy'}

def raster(tmp_path,values=None,**changes):
    values=np.array([[0,1,2],[3,4,5]],dtype='float64') if values is None else values
    file=tmp_path/'elevation.tif';profile={'driver':'GTiff','width':values.shape[1],'height':values.shape[0],
        'count':1,'dtype':str(values.dtype),'crs':'EPSG:4549','transform':Affine(10,0,500000,0,-10,3000020)}|changes
    with rasterio.open(file,'w',**profile) as ds:ds.write(values,1)
    return file

def test_native_dem_uses_source_cell_centres_south_up_and_preserves_valid_zero(tmp_path):
    values,grid=_surface(raster(tmp_path),SceneParameters(vertical_unit='metre'),FRAME)
    np.testing.assert_array_equal(values,np.array([[3,4,5],[0,1,2]],dtype='float32'))
    assert grid['bounds']==[500005.,3000005.,500025.,3000015.] and grid['range_m']==[0.,5.]
    assert grid['rounding_error_m']==0 and len(_transport(values,grid))==40+6*4

def test_elevation_calibration_and_explicit_float32_rounding_are_in_metres(tmp_path):
    file=raster(tmp_path)
    with rasterio.open(file,'r+') as ds:ds.scales=(2.,);ds.offsets=(5.,)
    with pytest.raises(BridgeError,match='FLOAT32_ROUNDING'):_surface(file,SceneParameters(vertical_unit='foot'),FRAME)
    native,grid=_surface(file,SceneParameters(vertical_unit='foot',max_elevation_rounding_error_m=1e-6),FRAME)
    expected=((np.array([[0,1,2],[3,4,5]])*2+5)*.3048).astype('float32')[::-1]
    np.testing.assert_array_equal(native,expected)
    assert 0<grid['rounding_error_m']<=1e-6 and grid['source_scale']==2 and grid['source_offset']==5

@pytest.mark.parametrize('change,message',[
    ({'nodata':0.},'MISSING_ELEVATION'),
    ({'transform':Affine(10,1,500000,0,-10,3000020)},'NORTH_UP'),
    ({'crs':'EPSG:3857'},'CRS_MISMATCH'),
])
def test_native_scene_does_not_guess_missing_pixels_rectification_or_crs(tmp_path,change,message):
    with pytest.raises(BridgeError,match=message):_surface(raster(tmp_path,**change),SceneParameters(vertical_unit='metre'),FRAME)

def test_native_scene_rejects_one_dimensional_and_nonfinite_heights(tmp_path):
    with pytest.raises(BridgeError,match='GRID_BOUND'):_surface(raster(tmp_path,np.ones((1,3))),SceneParameters(vertical_unit='metre'),FRAME)
    with pytest.raises(BridgeError,match='MISSING_ELEVATION'):_surface(raster(tmp_path,np.array([[1,np.nan],[2,3]])),SceneParameters(vertical_unit='metre'),FRAME)

def test_camera_requires_complete_pair_and_native_nonvertical_direction():
    for args in [{'eye':[0.,0.,10.]},{'eye':[0.,0.,10.],'target':[0.,0.,0.]},{'width':True},{'quality':float('nan')}]:
        with pytest.raises(ValidationError):SceneParameters(**args)
    assert SceneParameters(eye=[1.,2.,30.],target=[10.,20.,0.]).mode=='overview'

def test_scene_document_rejects_unbound_or_escaping_native_dependencies(tmp_path):
    header=tmp_path/'terrain.dem';header.write_bytes(b'not a trusted DEM')
    source=tmp_path/'elevation.tif';source.write_bytes(b'canonical')
    outside=tmp_path/'outside.dat';outside.write_bytes(b'untouched')
    unsafe='terrain/../../outside.dat'
    record={'format':FORMAT,'native_dem':'terrain.dem','elevation':'elevation.tif',
        'native_members':{'terrain.dem':file_sha(header),unsafe:file_sha(outside)},
        'members':{'terrain.dem':file_sha(header),'elevation.tif':file_sha(source),unsafe:file_sha(outside)}}
    scene=tmp_path/'map.scene.json';scene.write_text(json.dumps(record))
    with pytest.raises(BridgeError):_scene_source(scene,file_sha(scene))
    assert outside.read_bytes()==b'untouched'
