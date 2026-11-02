import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mapgis_agent.core import Bridge,BridgeError,read_native,sha

CRS={'kind':'local','name':'analytic grid','unit':'metre','axis_order':'xy','reference_sha256':'0'*64}


def _plane(tmp_path):
    p=tmp_path/'plane.grd'
    # Cell centres x=0..10, y=0..10, z=x. North-up GDAL raster.
    z=np.tile(np.arange(11,dtype=np.float32),(11,1))
    with rasterio.open(p,'w',driver='GSBG',width=11,height=11,count=1,dtype='float32',
                       transform=from_origin(-.5,10.5,1,1)) as ds:ds.write(z,1)
    return p,sha(p.read_bytes())


def test_grid_isobands_keep_exact_interval_area_and_isolines_without_reinterpolation(tmp_path):
    from mapgis_agent.grid_vectors import prepare_grid_vectors
    bridge=Bridge(tmp_path);source,digest=_plane(tmp_path)
    p=prepare_grid_vectors(bridge,str(source),digest,{'operation':'bands','levels':[0,2,4,10.01],
        'colors':[7,6,2]}, {'name':'bands.wp','crs':CRS,'map_scale':1000},'bands')
    assert p['state']=='PREPARED' and p['metrics']['grid_shape']==[11,11]
    output=bridge.apply('bands')['output'];_,reader,_=read_native(Path(output))
    areas={int(row['class_index']):g.area for (_,row),g in zip(reader.data.iterrows(),reader.geom)}
    assert areas==pytest.approx({1:20,2:20,3:60},abs=1e-8)
    p=prepare_grid_vectors(bridge,str(source),digest,{'operation':'contours','levels':[2,4,20]},
        {'name':'contours.wl','crs':CRS,'map_scale':1000},'contours')
    assert p['metrics']['empty_levels']==[20]
    _,reader,_=read_native(Path(bridge.apply('contours')['output']))
    assert sorted(g.length for g in reader.geom)==[10,10]
    assert sorted(float(g.coords[0][0]) for g in reader.geom)==[2,4]
    assert sha(source.read_bytes())==digest


def test_grid_sidecar_is_bound_and_source_change_blocks_apply(tmp_path):
    from mapgis_agent.grid_vectors import prepare_grid_vectors
    bridge=Bridge(tmp_path);source,digest=_plane(tmp_path)
    sidecar=Path(str(source)+'.json')
    sidecar.write_text(json.dumps({'schema':'mapgis-agent/grid/1','grid_sha256':'0'*64,'crs':CRS}),encoding='utf8')
    with pytest.raises(BridgeError,match='GRID_SIDECAR'):
        prepare_grid_vectors(bridge,str(source),digest,{'operation':'contours','levels':[2]},
                             {'name':'bad.wl','crs':CRS,'map_scale':1000},'bad')
    sidecar.write_text(json.dumps({'schema':'mapgis-agent/grid/1','grid_sha256':digest,'crs':CRS}),encoding='utf8')
    prepare_grid_vectors(bridge,str(source),digest,{'operation':'contours','levels':[2]},
                         {'name':'ok.wl','crs':CRS,'map_scale':1000},'ok')
    sidecar.write_text(sidecar.read_text(encoding='utf8')+' ',encoding='utf8')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):bridge.apply('ok')
