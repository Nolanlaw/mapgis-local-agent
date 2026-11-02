"""Native drawing ranges must share the same units as stored geometry."""
import struct
from pathlib import Path
import numpy as np
import pytest
from shapely.geometry import LineString,Point,Polygon,mapping
from mapgis_agent.core import Bridge,read_native
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import native_file_info,prepare_native_operation
from mapgis_agent.raster import file_sha
from test_native_raster import native_settings

TM={'definition':'+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}


@pytest.mark.parametrize('scale',[1000,50000])
@pytest.mark.parametrize('suffix',['.wt','.wl','.wp'])
def test_created_projected_point_line_region_ranges_are_in_the_same_native_drawing_frame(tmp_path,suffix,scale):
    b=Bridge(tmp_path);native_settings(tmp_path)
    vertices=[(700000.,2880000.),(725000.,2880000.),(725000.,2900000.),(700000.,2900000.),(700000.,2880000.)]
    geometry=Point(*vertices[0]) if suffix=='.wt' else LineString(vertices) if suffix=='.wl' else Polygon(vertices)
    feature={'type':'Feature','id':'one','geometry':mapping(geometry),'properties':{'NAME':'scale-test'}}
    if suffix=='.wt':feature.update(text='A',style={'height':3.,'width':2.4,'color':1,'font_zh':1,'font_en':0})
    prepare_creation(b,{'type':'FeatureCollection','features':[feature]},
        {'name':'scaled'+suffix,'map_scale':scale,'crs':TM},'create')
    created=Path(b.apply('create')['output']);before=file_sha(created);blob,reader,sections=read_native(created)
    factor=1000/scale;bounds=np.array(geometry.bounds)*factor
    if suffix=='.wl':
        np.testing.assert_array_equal(struct.unpack_from('<4d',blob,sections[6][0]),bounds)
        np.testing.assert_array_equal(struct.unpack_from('<4d',blob,sections[6][0]+32),bounds)
    for identity,file in [('created',created),('saved',None)]:
        if file is None:
            prepare_native_operation(b,'resave',str(created),before,{},'resave');file=Path(b.apply('resave')['output'])
        native=native_file_info(tmp_path,str(file),file_sha(file));actual=np.array(native['native_bounds'])
        if suffix=='.wt':
            assert actual[0]<=bounds[0]<=actual[2] and actual[1]<=bounds[1]<=actual[3]
            assert actual[2]-actual[0]<30 and actual[3]-actual[1]<30
        else:np.testing.assert_allclose(actual,bounds,rtol=0,atol=1e-7)
        _,check,_=read_native(file);assert geometry.hausdorff_distance(check.geom[0])<1e-7
    assert file_sha(created)==before
