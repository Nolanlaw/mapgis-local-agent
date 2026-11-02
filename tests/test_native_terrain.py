"""Real DTM interpolation, independent GDAL readback and fresh native reopen."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio

from mapgis_agent.core import Bridge, BridgeError, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.terrain import prepare_terrain

ROOT=Path(__file__).resolve().parents[1]
CRS={'kind':'local','name':'IDW analytic plane','unit':'metre','axis_order':'xy','reference_sha256':'0'*64}
PARAMS={'operation':'native.idw','height_field':'z','bounds':[0,0,10,10],
        'shape':[5,5],'power':2,'search_radius':100,'anisotropy_ratio':1,'anisotropy_angle_degrees':0}


def _source(tmp_path,heights):
    bridge=Bridge(tmp_path)
    settings=ROOT/'config.local.json'
    if sys.platform!='win32' or not settings.is_file():pytest.skip('Licensed native installation required')
    config=json.loads(settings.read_text(encoding='utf8'))
    if not config.get('native_backend_legacy_dep_compatibility'):pytest.skip('Owned legacy process opt-in required')
    (tmp_path/'config.local.json').write_text(json.dumps({k:config[k] for k in
        ('mapgis_installation','native_backend_legacy_dep_compatibility')}),encoding='utf8')
    xy=[(0,0),(10,0),(10,10),(0,10),(5,5)]
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),
        'geometry':{'type':'Point','coordinates':list(pt)},'properties':{'z':float(z)}}
        for i,(pt,z) in enumerate(zip(xy,heights),1)]}
    prepare_creation(bridge,features,{'name':'samples.wt','crs':CRS,'map_scale':1000},'samples')
    path=Path(bridge.apply('samples')['output'])
    return bridge,path,sha(path.read_bytes())


def test_native_idw_generates_surfer_grid_and_reopens_every_cell_in_new_pid(tmp_path):
    bridge,source,digest=_source(tmp_path,[0,10,20,10,10])
    original=source.read_bytes()
    prepared=prepare_terrain(bridge,str(source),digest,PARAMS,{'name':'idw.grd','crs':CRS},'idw')
    assert prepared['state']=='PREPARED'
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    assert prepared['native_reopen']['all_cells_equal'] is True
    applied=bridge.apply('idw')
    with rasterio.open(applied['output']) as ds:
        assert ds.driver=='GSBG' and ds.shape==(5,5)
        actual=ds.read(1)
        # Hand-computed inverse-square weights; north row is y=10.
        assert actual[0,0]==10 and actual[0,4]==20 and actual[4,0]==0 and actual[4,4]==10
        assert actual[2,2]==10
        assert actual[4,1]==pytest.approx(3.191173791885376,abs=1e-6)
        assert actual[2,0]==pytest.approx(7.647058823529412,abs=1e-6)
    sidecar=Path(applied['output']+'.json')
    metadata=json.loads(sidecar.read_text(encoding='utf8'))
    assert metadata['grid_sha256']==sha(Path(applied['output']).read_bytes()) and metadata['crs']==CRS
    repeat=prepare_terrain(bridge,str(source),digest,PARAMS,{'name':'idw.grd','crs':CRS},'idw')
    assert repeat['state']=='APPLIED' and repeat['native_execution']['pid']==prepared['native_execution']['pid']
    assert source.read_bytes()==original


def test_native_idw_keeps_constant_zero_valid_and_rejects_fractional_power(tmp_path):
    bridge,source,digest=_source(tmp_path,[0]*5)
    with pytest.raises(BridgeError,match='IDW_POWER'):
        prepare_terrain(bridge,str(source),digest,PARAMS|{'power':1.5},{'name':'bad.grd','crs':CRS},'bad')
    p=prepare_terrain(bridge,str(source),digest,PARAMS,{'name':'zero.grd','crs':CRS},'zero')
    output=bridge.apply('zero')['output']
    with rasterio.open(output) as ds:
        assert np.all(ds.read(1)==0) and np.all(ds.read_masks(1)==255)
    assert p['metrics']['range']==[0,0] and p['metrics']['valid_cells']==25


def test_native_idw_grid_must_fit_signed_surfer_dimensions_and_preserve_source(tmp_path):
    bridge,source,digest=_source(tmp_path,[7]*5)
    with pytest.raises(BridgeError,match='GRID_SHAPE'):
        prepare_terrain(bridge,str(source),digest,PARAMS|{'shape':[32768,2]},
                        {'name':'too-wide.grd','crs':CRS},'too-wide')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):
        prepare_terrain(bridge,str(source),'0'*64,PARAMS,{'name':'stale.grd','crs':CRS},'stale')
    assert sha(source.read_bytes())==digest
