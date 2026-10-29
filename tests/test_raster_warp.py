from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.transform import from_origin

from mapgis_agent.core import Bridge,BridgeError,sha


def scan(root):
    source=root/'scan.tif';array=np.arange(32*24,dtype=np.uint8).reshape(1,24,32)
    points=[(0,0,400,900),(32,0,464,900),(32,24,464,852),(0,24,400,852)]
    with rasterio.open(source,'w',driver='GTiff',width=32,height=24,count=1,dtype='uint8',
        crs='EPSG:32650',gcps=[GroundControlPoint(col=x,row=y,x=X,y=Y) for x,y,X,Y in points]) as raster:raster.write(array)
    return source,array


def test_gcp_rectification_has_exact_extent_crs_and_nearest_pixels(tmp_path):
    from mapgis_agent.native_raster import prepare_raster_warp
    bridge=Bridge(tmp_path);source,array=scan(tmp_path);before=source.read_bytes()
    parameters={'name':'rectified.tif','target_crs':{'definition':'EPSG:32650','axis_order':'xy'},
        'bounds':[400,852,464,900],'resolution':[2,2],'resampling':'nearest'}
    preview=prepare_raster_warp(bridge,str(source),sha(before),parameters,'warp')
    assert preview['state']=='PREPARED' and preview['georeference']=='GCP_AFFINE_RECTIFIED'
    output=Path(bridge.apply('warp')['output'])
    with rasterio.open(output) as raster:
        assert raster.crs.to_epsg()==32650 and tuple(raster.bounds)==(400,852,464,900)
        assert raster.transform==from_origin(400,900,2,2)
        assert not raster.gcps[0] and np.array_equal(raster.read(),array)
    assert source.read_bytes()==before


def test_warp_rejects_ambiguous_or_invalid_target_geometry(tmp_path):
    from mapgis_agent.native_raster import prepare_raster_warp
    bridge=Bridge(tmp_path);source,_=scan(tmp_path);digest=sha(source.read_bytes())
    parameters={'name':'a.tif','target_crs':{'definition':'EPSG:32650','axis_order':'xy'},
        'bounds':[400,852,464,900],'resolution':[2,2],'resampling':'nearest'}
    with pytest.raises(BridgeError,match='WARP_GRID'):prepare_raster_warp(bridge,str(source),digest,parameters|{'bounds':[464,852,400,900]},'invalid')
    with pytest.raises(BridgeError,match='WARP_RESAMPLING'):prepare_raster_warp(bridge,str(source),digest,parameters|{'resampling':'invented'},'invalid2')
