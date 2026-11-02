"""Known basin conservation, depression volume, native geometry and recovery."""
import json
from pathlib import Path
import numpy as np
import pytest,rasterio
from rasterio.transform import from_origin
from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.raster import file_sha

FRAME={'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}
PARAMETERS={'vertical_unit':'metre','depression_handling':'fill','outlets':'min','stream_min_cells':5}
TEMPLATE={'name':'flow.tif','crs':FRAME,'map_scale':1000}


def dem(tmp_path,bowl=False,nodata=False):
    file=tmp_path/'dem.tif';row,col=np.mgrid[:7,:7];z=(100+2*(6-row)+np.abs(col-3)).astype('float32')
    if bowl:z[:]=10;z[3,3]=0;z[6,3]=5
    if nodata:z[0,0]=-9999.
    with rasterio.open(file,'w',driver='GTiff',height=7,width=7,count=1,dtype='float32',nodata=-9999 if nodata else None,
                       crs='+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs',transform=from_origin(500000,3300070,10,10)) as ds:ds.write(z,1)
    return file,z


@pytest.mark.parametrize('bowl',[False,True])
def test_fill_d8_accumulation_basin_area_and_editable_geometry(tmp_path,bowl):
    from mapgis_agent.hydrology import prepare_hydrology
    source,z=dem(tmp_path,bowl=bowl);before=(file_sha(source),source.stat().st_mtime_ns);bridge=Bridge(tmp_path)
    prepared=prepare_hydrology(bridge,str(source),before[0],PARAMETERS,TEMPLATE,'hydro')
    result=bridge.apply('hydro');folder=Path(result['output']).parent
    assert prepared['metrics']['valid_cells']==49 and prepared['metrics']['basins']==1
    assert prepared['metrics']['fill_volume_m3']==(1000. if bowl else 0.)
    assert prepared['metrics']['outlet_upstream_cell_sum']==49
    for role in ['direction','filled_elevation','fill_depth','upstream_cells','upstream_area','basins']:
        with rasterio.open(folder/prepared['rasters'][role]['file']) as ds:
            a=ds.read(1,masked=True);assert a.count()==49 and ds.transform==from_origin(500000,3300070,10,10)
            if role=='upstream_cells':assert a[6,3]==49
            if role=='upstream_area':assert a[6,3]==4900
            if role=='basins':assert np.all(a==1)
            if role=='filled_elevation':assert a[3,3]==(10 if bowl else z[3,3])
    for role in ['streams','basins','outlets']:
        _,parsed,_=read_native(folder/prepared['vectors'][role]['file'])
        assert len(parsed.geom)>0
        if role=='basins':assert abs(sum(g.area for g in parsed.geom)-4900.)<1e-7
        if role=='outlets':assert list(parsed.geom[0].coords)[0]==(500035.,3300005.)
    assert (file_sha(source),source.stat().st_mtime_ns)==before
    assert bridge.status('hydro')['output_matches']


def test_missing_elevation_boundary_must_be_explicit_and_valid_zero_is_not_missing(tmp_path):
    from mapgis_agent.hydrology import prepare_hydrology
    source,z=dem(tmp_path,bowl=True,nodata=True);bridge=Bridge(tmp_path)
    with pytest.raises(BridgeError,match='HYDRO_NODATA_BOUNDARY'):
        prepare_hydrology(bridge,str(source),file_sha(source),PARAMETERS,TEMPLATE,'reject')
    prepared=prepare_hydrology(bridge,str(source),file_sha(source),PARAMETERS|{'nodata_boundary':'open'},TEMPLATE,'open')
    result=bridge.apply('open');folder=Path(result['output']).parent
    assert prepared['metrics']['valid_cells']==48
    with rasterio.open(result['output']) as ds:
        assert ds.read(1)[0,0]==247 and ds.read_masks(1)[0,0]==0
        assert ds.read_masks(1)[6,3]==255 and ds.read(1)[6,3]==0
    for role in prepared['rasters']:
        with rasterio.open(folder/prepared['rasters'][role]['file']) as ds:assert np.count_nonzero(ds.read_masks(1))==48


def test_precision_loss_cannot_silently_change_drainage(tmp_path):
    from mapgis_agent.hydrology import prepare_hydrology
    source,z=dem(tmp_path)
    with rasterio.open(source,'r+') as ds:
        ds.scales=(1.,);ds.offsets=(.0000001,)
    with pytest.raises(BridgeError,match='HYDRO_ELEVATION_PRECISION'):
        prepare_hydrology(Bridge(tmp_path),str(source),file_sha(source),PARAMETERS,TEMPLATE,'precision')


def test_numeric_only_and_explicit_precision_allowance_keep_same_basin(tmp_path):
    from mapgis_agent.hydrology import prepare_hydrology
    source,_=dem(tmp_path)
    with rasterio.open(source,'r+') as ds:ds.offsets=(.0000001,)
    bridge=Bridge(tmp_path)
    result=prepare_hydrology(bridge,str(source),file_sha(source),PARAMETERS|{
        'vector_outputs':False,'max_elevation_rounding_error_m':.000001},TEMPLATE,'numeric')
    assert result['vectors']=={} and result['metrics']['outlet_upstream_cell_sum']==49
    assert 0<result['maximum_elevation_rounding_error_m']<.000001
    bridge.apply('numeric');assert bridge.status('numeric')['output_matches']


def test_single_minimum_outlet_rejects_disconnected_open_domains(tmp_path):
    from mapgis_agent.hydrology import prepare_hydrology
    source,z=dem(tmp_path,nodata=True)
    with rasterio.open(source,'r+') as ds:z[:,3]=-9999.;ds.write(z,1)
    with pytest.raises(BridgeError,match='HYDRO_MIN_OUTLET_COMPONENTS'):
        prepare_hydrology(Bridge(tmp_path),str(source),file_sha(source),PARAMETERS|{'nodata_boundary':'open'},TEMPLATE,'islands')


def test_completed_public_algorithm_recovery_keeps_original_result(tmp_path,monkeypatch):
    from mapgis_agent import hydrology
    source,_=dem(tmp_path,bowl=True);bridge=Bridge(tmp_path);finish=hydrology._finish_hydrology
    def stop(*args):raise SystemExit(118)
    monkeypatch.setattr(hydrology,'_finish_hydrology',stop)
    with pytest.raises(SystemExit):hydrology.prepare_hydrology(bridge,str(source),file_sha(source),PARAMETERS,TEMPLATE,'recover')
    stage=Path(bridge.status('recover')['stage']);raw=stage/'hydrology-raw.npz';before=(file_sha(raw),raw.stat().st_mtime_ns)
    monkeypatch.setattr(hydrology,'_finish_hydrology',finish)
    with pytest.raises(BridgeError,match='HYDRO_PRODUCER_RUNNING'):hydrology.reconcile_hydrology(bridge,'recover')
    monkeypatch.setattr(hydrology,'_producer_alive',lambda _:False)
    monkeypatch.setattr(hydrology,'_calculate',lambda *args:pytest.fail('Completed hydrology was replayed'))
    restored=hydrology.reconcile_hydrology(bridge,'recover');assert restored['state']=='PREPARED' and restored['replayed'] is False
    bridge.apply('recover');assert bridge.status('recover')['output_matches']
    assert (file_sha(raw),raw.stat().st_mtime_ns)==before


@pytest.mark.parametrize('changed',['source','raw','context'])
def test_recovery_rejects_changed_source_result_and_request_binding(tmp_path,monkeypatch,changed):
    from mapgis_agent import hydrology
    source,_=dem(tmp_path);bridge=Bridge(tmp_path);finish=hydrology._finish_hydrology
    def stop(*args):raise SystemExit(118)
    monkeypatch.setattr(hydrology,'_finish_hydrology',stop)
    with pytest.raises(SystemExit):hydrology.prepare_hydrology(bridge,str(source),file_sha(source),PARAMETERS,TEMPLATE,'tamper')
    stage=Path(bridge.status('tamper')['stage'])
    if changed=='context':
        file=stage/'hydrology-completed.json';record=json.loads(file.read_text(encoding='utf8'));record['payload_sha256']='0'*64
        file.write_text(json.dumps(record),encoding='utf8')
    else:
        with (source if changed=='source' else stage/'hydrology-raw.npz').open('ab') as stream:stream.write(b'changed')
    monkeypatch.setattr(hydrology,'_finish_hydrology',finish);monkeypatch.setattr(hydrology,'_producer_alive',lambda _:False)
    with pytest.raises(BridgeError,match='STALE_SOURCE|HYDRO_CHECKPOINT_CHANGED'):hydrology.reconcile_hydrology(bridge,'tamper')
