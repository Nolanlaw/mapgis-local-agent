import json
from pathlib import Path
import cv2
import numpy as np
import pytest
import rasterio

from mapgis_agent.raster import RasterWorkspace
from test_local_frame import LOCAL


def test_geotiff_contains_validated_source_mapping_and_native_gcps(tmp_path):
    image=np.full((60,80,3),200,np.uint8)
    original=tmp_path / 'scan.png'
    cv2.imencode('.png',image)[1].tofile(str(original))
    service=RasterWorkspace(tmp_path / 'project')
    service.register_scan(str(original),'scan')
    service.render_window('scan',[10,10,40,30],'tile',40)
    gcps=[{'pixel':[0,0],'world':[1000,2000]}, {'pixel':[80,0],'world':[1160,2000]},
          {'pixel':[0,60],'world':[1000,1880]}]
    service.prepare_georeference('scan',LOCAL,gcps,'fit',
        checkpoints=[{'pixel':[40,30],'world':[1080,1940]}])
    from mapgis_agent.raster import write_georeferenced_window
    result=write_georeferenced_window(service,'scan','tile','fit','geotiff')
    with rasterio.open(result['output']) as src:
        points,crs=src.gcps
        assert len(points)==4
        assert src.shape==(30,40)
        assert points[0].x==pytest.approx(1020)
        assert points[0].y==pytest.approx(1980)
        assert points[2].x==pytest.approx(1100)
        assert points[2].y==pytest.approx(1920)
        assert crs.is_geographic is False
        assert set(src.tags()) <= {'AREA_OR_POINT'}  # No private GDAL metadata tag 42112.
    native_points=Path(result['native_gcp_file']).read_text(encoding='gbk').splitlines()
    assert len(native_points)==5
    assert native_points[0].startswith('控制点号')
    assert [float(x) for x in native_points[1].split(',')]==pytest.approx([1,0,0,1020,1980,0])
    assert [float(x) for x in native_points[3].split(',')]==pytest.approx([3,40,30,1100,1920,0])
    assert result['native_georeference_requires_gcp_import'] is True
    assert result['crs']==LOCAL
    assert write_georeferenced_window(service,'scan','tile','fit','geotiff')==result
