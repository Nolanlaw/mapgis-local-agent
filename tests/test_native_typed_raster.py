from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mapgis_agent.core import Bridge,sha
from mapgis_agent.native_raster import prepare_native_raster
from test_native_raster import native_settings


@pytest.mark.parametrize('dtype,bands', [('uint16',1),('int16',1),('int8',1),('uint32',1),
    ('int32',1),('float32',1),('float64',1),('uint16',3)])
def test_typed_multitile_msi_roundtrip_preserves_every_sample_and_native_type(tmp_path,dtype,bands):
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    source=tmp_path/'typed.tif';width,height=137,139
    values=np.arange(bands*width*height,dtype=np.float64).reshape(bands,height,width)
    if dtype=='uint16':values=(values*13)%65536
    elif dtype=='int16':values=(values*11)%65536-32768
    elif dtype=='uint32':values=values*101+4_000_000_000
    elif dtype=='int32':values=values*101-2_000_000_000
    else:values=values/17.3-3
    pixels=values.astype(dtype)
    with rasterio.open(source,'w',driver='GTiff',width=width,height=height,count=bands,dtype=dtype,
        crs='EPSG:32650',transform=from_origin(400,900,2,2)) as dataset:dataset.write(pixels)
    before=source.read_bytes()
    prepared=prepare_native_raster(bridge,'to_msi',str(source),sha(before),{},'typed-import')
    result=bridge.apply('typed-import')
    expected_type={'uint16':1,'uint32':2,'int8':3,'int16':4,'int32':5,'float32':6,'float64':7}[dtype]
    assert prepared['native_profile']['cell_type']==expected_type
    assert prepared['native_profile']['cell_bits']==np.dtype(dtype).itemsize*8
    assert prepared['native_display']['numeric_storage_exact'] is True
    assert prepared['native_display']['rgb_byte'] is False
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    assert prepared['native_pyramids']
    for level in prepared['native_pyramids']:
        shift=level['level'];band=level['band']
        assert (level['width'],level['height'])==((width+(1<<shift)-1)>>shift,(height+(1<<shift)-1)>>shift)
        for sample in level['samples']:
            x,y=sample['pixel']
            assert sample['value']==float(pixels[band,(y>>shift)<<shift,(x>>shift)<<shift])
    exported=prepare_native_raster(bridge,'to_tiff',result['output'],result['candidate_sha256'],{},'typed-export')
    target=Path(bridge.apply('typed-export')['output'])
    with rasterio.open(target) as actual:
        assert actual.dtypes==(dtype,)*bands and np.array_equal(actual.read(),pixels)
        assert actual.crs.to_epsg()==32650 and actual.transform==from_origin(400,900,2,2)
    assert source.read_bytes()==before
    assert exported['pixel_check']=='EXACT_AGAINST_BOUND_SIDECAR'
    # The optional coordinate witness is not required to retain native type
    # and samples. Its missing CRS identifier must stay unknown.
    if dtype=='float32':
        standalone=tmp_path/'unbound.msi';standalone.write_bytes(Path(result['output']).read_bytes())
        unbound=prepare_native_raster(bridge,'to_tiff',str(standalone),sha(standalone.read_bytes()),{},'without-witness')
        saved=bridge.apply('without-witness')
        with rasterio.open(saved['output']) as actual:
            assert actual.dtypes==('float32',) and np.array_equal(actual.read(),pixels)
            assert actual.crs is None
        assert unbound['coordinate_metadata']=='native affine extent; CRS identifier unknown'


def test_native_numeric_nodata_and_valid_zero_survive_tile_roundtrip(tmp_path):
    bridge=Bridge(tmp_path);native_settings(tmp_path);source=tmp_path/'nodata.tif'
    pixels=np.arange(137*139,dtype='float32').reshape(1,139,137);pixels[0,40:70,60:80]=-9999
    with rasterio.open(source,'w',driver='GTiff',width=137,height=139,count=1,dtype='float32',nodata=-9999,
        crs='EPSG:32650',transform=from_origin(400,900,2,2)) as raster:raster.write(pixels)
    imported=prepare_native_raster(bridge,'to_msi',str(source),sha(source.read_bytes()),{},'nodata-native')
    saved=bridge.apply('nodata-native')
    assert imported['native_nodata']==-9999
    prepare_native_raster(bridge,'to_tiff',saved['output'],saved['candidate_sha256'],{},'nodata-tiff')
    output=bridge.apply('nodata-tiff')
    with rasterio.open(output['output']) as actual:
        assert actual.nodata==-9999 and np.array_equal(actual.read(),pixels)
        assert actual.read_masks(1)[0,0]==255 and actual.read(1)[0,0]==0
        assert not np.any(actual.read_masks(1)[40:70,60:80])


def test_typed_msi_native_extent_uses_pixel_to_world_direction_without_sidecar(tmp_path):
    """TIFF readback alone can hide a wrong MSI extent behind its witness."""
    import json
    from mapgis_agent.native_project import prepare_native_project,native_project_info
    bridge=Bridge(tmp_path);native_settings(tmp_path);source=tmp_path/'small-height.tif'
    pixels=np.arange(49,dtype='float32').reshape(1,7,7)
    with rasterio.open(source,'w',driver='GTiff',width=7,height=7,count=1,dtype='float32',
        transform=from_origin(-50,650,100,100),nodata=-9999) as ds:ds.write(pixels)
    prepared=prepare_native_raster(bridge,'to_msi',str(source),sha(source.read_bytes()),{},'extent')
    saved=bridge.apply('extent')
    events=[json.loads(line) for line in json.loads(Path(prepared['native_reopen']['log']).read_text(encoding='utf8'))['stdout'].splitlines()]
    extent=next(e for e in events if e['event']=='image_extent')
    assert extent['native_bounds']==pytest.approx([-50,-50,650,650],abs=1e-7)
    prepare_native_project(bridge,[{'file':saved['output'],'expected_sha256':saved['candidate_sha256']}],
        {'name':'height.mpj','coordinate_frame':'native'},'extent-project')
    project=bridge.apply('extent-project')
    info=native_project_info(tmp_path,project['output'],project['candidate_sha256'])
    assert info['layers'][0]['native_bounds']==pytest.approx([-50,-50,650,650],abs=1e-7)
