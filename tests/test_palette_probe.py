"""A rendered sheet must retain color IDs and the native lower-left origin."""
import importlib.util
from pathlib import Path

import numpy as np
import pytest
import rasterio

SPEC=importlib.util.spec_from_file_location('palette_probe',Path(__file__).parents[1]/'scripts/palette_probe.py')
probe=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_native_palette_samples_keep_number_and_lower_left_origin(tmp_path):
    library=tmp_path/'Pcolor.lib'
    library.write_bytes(b'local library fingerprint fixture')
    size=int(150*300/25.4)
    pixels=np.full((3,size,size),255,dtype=np.uint8)
    anchors={0:(11,22,33),31:(44,55,66),32:(77,88,99),563:(255,240,127),1023:(101,111,121)}
    for number,rgb in anchors.items():
        left=10+(number%32)*4
        bottom=10+(number//32)*4
        x0=int(left*size/150);x1=int((left+3.5)*size/150)
        y0=size-int((bottom+3.5)*size/150);y1=size-int(bottom*size/150)
        pixels[:,y0:y1,x0:x1]=np.array(rgb,dtype=np.uint8)[:,None,None]
    sheet=tmp_path/'palette.tif'
    with rasterio.open(sheet,'w',driver='GTiff',width=size,height=size,count=3,dtype='uint8') as target:
        target.write(pixels)
    actual=probe.read(sheet,library)
    assert actual['count']==1024
    for number,rgb in anchors.items():
        assert actual['colors'][number]=={'number':number,'rgb':list(rgb)}
    assert actual['state']=='EXTRACTED_REQUIRES_NATIVE_BINDING'
    assert actual['library_sha256']==probe.file_sha(library)


def test_native_palette_rejects_a_different_page(tmp_path):
    sheet=tmp_path/'wrong-page.tif'
    library=tmp_path/'Pcolor.lib';library.write_bytes(b'fixture')
    with rasterio.open(sheet,'w',driver='GTiff',width=300,height=200,count=3,dtype='uint8') as target:
        target.write(np.zeros((3,200,300),dtype=np.uint8))
    with pytest.raises(ValueError,match='150 x 150'):
        probe.read(sheet,library)
