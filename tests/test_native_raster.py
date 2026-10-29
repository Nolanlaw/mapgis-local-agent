"""Actual legacy MSI conversion in isolated native children, with GDAL pixel readback."""
import json
from pathlib import Path
import sys

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mapgis_agent.core import Bridge, sha
from mapgis_agent.native_backend import backend_configuration, _run_child

ROOT=Path(__file__).resolve().parents[1]


def native_settings(root):
    config=ROOT/'config.local.json'
    if sys.platform!='win32' or not config.is_file():pytest.skip('Local licensed 6.7 installation required')
    settings=json.loads(config.read_text(encoding='utf8'))
    if not settings.get('native_backend_legacy_dep_compatibility'):pytest.skip('Explicit process-only legacy host opt-in required')
    (root/'config.local.json').write_text(json.dumps({k:settings[k] for k in
        ('mapgis_installation','native_backend_legacy_dep_compatibility')}),encoding='utf8')
    return backend_configuration(root)


@pytest.mark.parametrize('bands',[1,3])
def test_native_tiff_msi_roundtrip_is_lossless_and_uses_new_process(tmp_path,bands):
    Bridge(tmp_path)
    executable,bas=native_settings(tmp_path)
    data=np.arange(bands*24*32,dtype=np.uint8).reshape(bands,24,32)
    source=tmp_path/'original.tif'
    with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=bands,dtype='uint8',
                       transform=from_origin(400,900,2,2),crs='EPSG:32650',interleave='pixel') as raster:
        raster.write(data)
    original=source.read_bytes()
    msi=tmp_path/'converted.msi';target=tmp_path/'reopened.tif'
    first,a=_run_child([str(executable),'image',str(bas),str(bas.parent/'msibase.dll'),
                        'to_msi',str(source),str(msi)],tmp_path,'to-msi.json')
    second,b=_run_child([str(executable),'image',str(bas),str(bas.parent/'msibase.dll'),
                         'to_tiff',str(msi),str(target)],tmp_path,'to-tiff.json')
    assert first['pid']!=second['pid']
    assert msi.is_file() and msi.stat().st_size>0
    assert a[-2]['event']=='image_saved' and a[-2]['native_result']==0
    assert b[-2]['event']=='image_saved' and b[-2]['native_result']==0
    with rasterio.open(target) as raster:
        assert raster.count==bands and (raster.width,raster.height)==(32,24)
        assert raster.dtypes==('uint8',)*bands
        assert np.array_equal(raster.read(),data)
    assert source.read_bytes()==original


def test_native_msi_control_points_survive_close_and_fresh_process(tmp_path):
    from rasterio.control import GroundControlPoint
    Bridge(tmp_path);executable,bas=native_settings(tmp_path)
    data=np.arange(24*32,dtype=np.uint8).reshape(1,24,32)
    points=[(0,0,400,900),(32,0,464,900),(32,24,464,852),(0,24,400,852)]
    source=tmp_path/'gcps.tif'
    with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=1,dtype='uint8',
        gcps=[GroundControlPoint(col=x,row=y,x=X,y=Y) for x,y,X,Y in points],crs='EPSG:32650') as raster:
        raster.write(data)
    original=source.read_bytes()
    command=tmp_path/'gcps.txt';command.write_text(''.join(' '.join(map(str,p))+'\n' for p in points),encoding='ascii')
    msi=tmp_path/'registered.msi';target=tmp_path/'reopened.tif'
    first,a=_run_child([str(executable),'image',str(bas),str(bas.parent/'msibase.dll'),
        'to_msi',str(source),str(msi),str(command)],tmp_path,'msi-register.json')
    second,b=_run_child([str(executable),'image',str(bas),str(bas.parent/'msibase.dll'),
        'to_tiff',str(msi),str(target)],tmp_path,'msi-reopen-gcps.json')
    assert first['pid']!=second['pid']
    actual=[(p['pixel_x'],p['pixel_y'],p['world_x'],p['world_y']) for p in b if p['event']=='gcp']
    assert len(actual)==4
    assert np.allclose(actual,points,rtol=0,atol=1e-9)
    with rasterio.open(target) as raster:assert np.array_equal(raster.read(),data)
    assert source.read_bytes()==original


@pytest.mark.parametrize('use_gcps',[False,True])
def test_raster_tool_publishes_fingerprinted_coordinates_and_lossless_pixels(tmp_path,use_gcps):
    from rasterio.control import GroundControlPoint
    from mapgis_agent.native_raster import prepare_native_raster,raster_info
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    data=np.arange(3*24*32,dtype=np.uint8).reshape(3,24,32)
    source=tmp_path/'original.tif'
    points=[(0,0,400,900),(32,0,464,900),(32,24,464,852),(0,24,400,852)]
    spatial={'gcps':[GroundControlPoint(col=x,row=y,x=X,y=Y) for x,y,X,Y in points]} if use_gcps else {'transform':from_origin(400,900,2,2)}
    with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=3,dtype='uint8',crs='EPSG:32650',**spatial) as raster:raster.write(data)
    before=source.read_bytes()
    preview=prepare_native_raster(bridge,'to_msi',str(source),sha(before),{},'import')
    assert preview['pixel_check']=='EXACT'
    assert preview['native_execution']['pid']!=preview['native_reopen']['pid']
    msi=bridge.apply('import')
    assert {Path(msi['output']).name,Path(msi['output']).name+'.json'} <= {p.name for p in Path(msi['output']).parent.iterdir()}
    assert bridge.status('import')['output_matches']
    repeat=prepare_native_raster(bridge,'to_msi',str(source),sha(before),{},'import')
    assert repeat['state']=='APPLIED' and repeat['native_execution']['pid']==preview['native_execution']['pid']
    exported=prepare_native_raster(bridge,'to_tiff',msi['output'],msi['candidate_sha256'],{},'export')
    target=Path(bridge.apply('export')['output'])
    original,actual=raster_info(source),raster_info(target)
    assert original['pixel_sha256']==actual['pixel_sha256']
    assert original['crs_wkt']==actual['crs_wkt']
    assert original['gcps']==actual['gcps']
    assert original['gcp_crs_wkt']==actual['gcp_crs_wkt']
    assert original['transform']==actual['transform']
    assert source.read_bytes()==before
