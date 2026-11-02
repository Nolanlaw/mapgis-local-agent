"""Ground endpoints, ray sampling and differential native GDAL compatibility."""
import ctypes as C
import math
import numpy as np
import pytest
import rasterio
from mapgis_agent.core import BridgeError
from mapgis_agent.raster import file_sha
from test_visibility import dem_case,FRAME


def test_ground_surface_equality_and_first_ordered_obstruction(tmp_path):
    from mapgis_agent.visibility import line_of_sight
    source,observer=dem_case(tmp_path)
    parameters={'observer':observer,'target':[500355.,3300205.],'observer_height':0.,'target_height':0.,'vertical_unit':'metre'}
    clear=line_of_sight(str(source),file_sha(source),parameters,FRAME)
    assert clear['visible'] and clear['first_obstruction'] is None
    assert len(clear['samples'])==31 and all(r['clearance_m']==0 for r in clear['samples'])
    with rasterio.open(source,'r+') as ds:
        z=ds.read(1);z[:,20:23]=180.;ds.write(z,1)
    blocked=line_of_sight(str(source),file_sha(source),parameters,FRAME)
    assert not blocked['visible'] and blocked['first_obstruction']['column']==20
    parameters.update(observer=parameters['target'],target=observer)
    reverse=line_of_sight(str(source),file_sha(source),parameters,FRAME)
    assert not reverse['visible'] and reverse['first_obstruction']['column']==22


@pytest.mark.parametrize('target',[(20,0),(20,40),(0,5),(40,5),(0,0),(0,40),(40,0),(40,40),(20,5)])
def test_flat_ground_visible_in_all_directions_including_same_cell(tmp_path,target):
    from mapgis_agent.visibility import line_of_sight
    source,observer=dem_case(tmp_path,zero=True)
    with rasterio.open(source) as ds:xy=list(ds.xy(*target))
    result=line_of_sight(str(source),file_sha(source),{'observer':observer,'target':xy,'observer_height':0.,'target_height':0.,'vertical_unit':'metre'},FRAME)
    assert result['visible'] and result['first_obstruction'] is None
    assert result['samples'][0]['row']==20 and result['samples'][0]['column']==5
    assert result['samples'][-1]['row']==target[0] and result['samples'][-1]['column']==target[1]
    assert len(result['samples'])==max(abs(target[0]-20),abs(target[1]-5))+1


def test_port_matches_real_legacy_gdal_booleans_when_no_equality_case(tmp_path):
    from mapgis_agent._los import trace
    from mapgis_agent.grid_analysis import _gdal,_LOCK
    source,_=dem_case(tmp_path)
    rng=np.random.default_rng(20261031);z=rng.integers(0,100,(41,41)).astype('float64')
    with rasterio.open(source,'r+') as ds:ds.write(z,1)
    with _LOCK:
        lib,_=_gdal()
        lib.GDALGetRasterBand.restype=C.c_void_p;lib.GDALGetRasterBand.argtypes=[C.c_void_p,C.c_int]
        lib.GDALIsLineOfSightVisible.restype=C.c_bool
        lib.GDALIsLineOfSightVisible.argtypes=[C.c_void_p,C.c_int,C.c_int,C.c_double,C.c_int,C.c_int,C.c_double,C.POINTER(C.c_int),C.POINTER(C.c_int),C.c_void_p]
        dataset=lib.GDALOpenEx(str(source).encode('utf8'),2,None,None,None)
        assert dataset
        try:
            band=lib.GDALGetRasterBand(dataset,1)
            for _ in range(200):
                xa,ya,xb,yb=map(int,rng.integers(0,41,4));za=float(z[ya,xa]+2.12345);zb=float(z[yb,xb]+3.76543)
                x=C.c_int();y=C.c_int()
                original=lib.GDALIsLineOfSightVisible(band,xa,ya,za,xb,yb,zb,C.byref(x),C.byref(y),None)
                port=trace(z,(xa,ya),(xb,yb),za,zb)
                assert port['visible']==original,(xa,ya,za,xb,yb,zb,port,original)
        finally:lib.GDALClose(dataset)


def test_los_checks_missing_data_on_ray_and_returns_usable_map_and_section_features(tmp_path):
    from mapgis_agent.visibility import line_of_sight
    source,observer=dem_case(tmp_path,nodata=True)
    parameters={'observer':observer,'target':[500355.,3300205.],'vertical_unit':'metre'}
    with pytest.raises(BridgeError,match='VISIBILITY_NODATA'):
        line_of_sight(str(source),file_sha(source),parameters,FRAME)
    parameters['target']=[500355.,3300305.]
    result=line_of_sight(str(source),file_sha(source),parameters,FRAME)
    assert result['visible']
    assert result['map_features']['features'][0]['geometry']['type']=='LineString'
    assert len(result['profile_features']['features'])==2
    assert result['profile_crs']['unit']=='metre'
    assert math.isclose(result['horizontal_distance_m'],math.hypot(300,100))


def test_returned_geometries_can_be_encoded_as_native_point_and_line_files(tmp_path):
    from mapgis_agent.visibility import line_of_sight
    from mapgis_agent.creation import prepare_creation
    from mapgis_agent.core import Bridge
    source,observer=dem_case(tmp_path,ridge=True)
    result=line_of_sight(str(source),file_sha(source),{'observer':observer,'target':[500355.,3300205.],
        'vertical_unit':'metre','observer_height':0.,'target_height':0.},FRAME)
    bridge=Bridge(tmp_path)
    for key,name,frame in [('map_features','map.wl',FRAME),('endpoint_features','endpoints.wt',FRAME),
                           ('profile_features','section.wl',result['profile_crs'])]:
        identity=name.split('.')[0]
        prepared=prepare_creation(bridge,result[key],{'name':name,'crs':frame,'map_scale':1000},identity)
        assert prepared['geometry_readback']=='VERIFIED'
        bridge.apply(identity)


def test_los_band_scale_offset_feet_and_subpixel_endpoint_sampling(tmp_path):
    from mapgis_agent.visibility import line_of_sight
    source,observer=dem_case(tmp_path,ridge=True)
    feet=tmp_path/'feet.tif'
    with rasterio.open(source) as ds:data,profile=ds.read(1),ds.profile
    with rasterio.open(feet,'w',**profile) as ds:
        ds.write((data/.3048-20)/3,1);ds.scales=(3.,);ds.offsets=(20.,)
    parameters={'observer':[observer[0]+1,observer[1]-1],'target':[500355.,3300205.],
                'vertical_unit':'metre','observer_height':2.,'target_height':0.}
    metric=line_of_sight(str(source),file_sha(source),parameters,FRAME)
    imperial=line_of_sight(str(feet),file_sha(feet),parameters|{'vertical_unit':'foot','observer_height':2/.3048},FRAME)
    assert metric['visible']==imperial['visible'] and metric['first_obstruction']['column']==imperial['first_obstruction']['column']
    assert metric['sampled_endpoints'][0]==observer
    np.testing.assert_allclose([v['clearance_m'] for v in metric['samples']],
                               [v['clearance_m'] for v in imperial['samples']],rtol=0,atol=1e-4)
