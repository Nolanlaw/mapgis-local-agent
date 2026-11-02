"""Native selected-copy transforms must preserve attributes, styles and holes."""
import io,itertools,math,struct
from pathlib import Path
import pytest
from shapely.geometry import Point,LineString,Polygon,mapping
from shapely.affinity import scale,rotate,translate
from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_com import prepare_native_subset,_check_subset
from mapgis_agent.raster import file_sha
from test_native_raster import native_settings
from pymapgis.reader import _read_crs


@pytest.mark.parametrize('kind,map_scale',[('wt',1),('wl',1000),('wp',50000)])
@pytest.mark.parametrize('reflection',[False,True])
def test_native_selected_copy_transforms_source_units_with_nonuniform_scale_rotation_and_holes(tmp_path,kind,map_scale,reflection):
    b=Bridge(tmp_path);native_settings(tmp_path)
    geometry={'wt':Point(3,5),'wl':LineString([(0,0),(4,2),(8,0)]),
        'wp':Polygon([(0,0),(20,0),(20,20),(0,20)],holes=[[(5,5),(5,10),(10,10),(10,5)]])}[kind]
    features=[]
    for i in range(3):
        item={'type':'Feature','id':str(i),'geometry':mapping(translate(geometry,xoff=i*20)), 'properties':{'ID':9,'岩性':'第'+str(i)+'层'}}
        if kind=='wt':item|={'text':'中文'+str(i),'style':{'height':3,'width':2.5,'angle':15,'color':3}}
        features.append(item)
    prepare_creation(b,{'type':'FeatureCollection','features':features},{'name':'source.'+kind,'crs':{'kind':'local','name':'transform fixture','axis_order':'xy','unit':'metre','reference_sha256':'a'*64},'map_scale':map_scale},'source')
    source=Path(b.apply('source')['output']);before=source.read_bytes();_,original,_=read_native(source)
    sx=-2. if reflection else 2.;transform={'translation':[100.,-25.],'scale':[sx,3.],'rotation_degrees':37.,'order':['scale','rotate','translate'],'coordinate_frame':'source'}
    prepared=prepare_native_subset(b,str(source),file_sha(source),[2,1],{'name':'transformed.'+kind},'transform',transform=transform)
    output=Path(b.apply('transform')['output']);_,actual,_=read_native(output)
    for i,old in enumerate([1,0]):
        expected=translate(rotate(scale(original.geom[old],xfact=sx,yfact=3.,origin=(0,0)),37.,origin=(0,0)),xoff=100.,yoff=-25.)
        assert expected.hausdorff_distance(actual.geom[i])<1e-7
        if kind=='wp':
            parts=list(actual.geom[i].geoms) if actual.geom[i].geom_type=='MultiPolygon' else [actual.geom[i]]
            assert sum(len(g.interiors) for g in parts)==1 and actual.geom[i].area==pytest.approx(2250.)
    assert list(actual.data['ID'])==[9,9] and list(actual.data['岩性'])==['第1层','第0层']
    assert prepared['verification']['primary_attribute_records_byte_identical']
    assert prepared['verification']['geometry_matches_requested_transform']
    assert prepared['verification']['graphic_parameters_equal']
    assert source.read_bytes()==before and output.read_bytes()[91:260]==before[91:260]
    assert prepare_native_subset(b,str(source),file_sha(source),[2,1],{'name':'transformed.'+kind},'transform',transform=transform)['state']=='APPLIED'
    if kind=='wt':
        blob,_,sections=read_native(output);bad=bytearray(blob);offset=sections[0][0]+93+7
        struct.pack_into('<d',bad,offset,struct.unpack_from('<d',bad,offset)[0]+.0001)
        tampered=tmp_path/'wrong-coordinate.wt';tampered.write_bytes(bad)
        with pytest.raises(BridgeError,match='SUBSET_TRANSFORM_GEOMETRY_DIFF'):
            _check_subset(source,tampered,prepared['verification']['mapping'],transform)


@pytest.mark.parametrize('transform',[
    {'translation':[1,float('nan')]}, {'scale':[0,1]}, {'rotation_degrees':True},
    {'order':['scale','scale','translate']}, {'coordinate_frame':'guessed'}, {'origin':[1,2]},
])
def test_subset_transform_rejects_invalid_parameters_before_native_launch(tmp_path,transform):
    from mapgis_agent.native_com import subset_transform
    base={'translation':[0,0],'scale':[1,1],'rotation_degrees':0,'order':['scale','rotate','translate'],'coordinate_frame':'source'}
    with pytest.raises(BridgeError,match='SUBSET_TRANSFORM'):subset_transform(base|transform)


@pytest.mark.parametrize('order',list(itertools.permutations(['translate','rotate','scale'])))
def test_native_transform_all_six_orders_on_actual_nonunit_drawing_scale(tmp_path,order):
    b=Bridge(tmp_path);native_settings(tmp_path)
    prepare_creation(b,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':mapping(Point(500000,3320000)),'properties':{'ID':7,'地层':'T2d'}}]},
        {'name':'source.wt','crs':{'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'},'map_scale':50000},'source')
    source=Path(b.apply('source')['output']);blob,original,_=read_native(source);_,factor=_read_crs(io.BytesIO(blob));assert factor==50.
    transform={'translation':[12.5,-30.25],'scale':[1.2,.8],'rotation_degrees':30.,'order':list(order),'coordinate_frame':'source'}
    prepare_native_subset(b,str(source),file_sha(source),[1],{'name':'changed.wt'},'ordered',transform=transform)
    target=Path(b.apply('ordered')['output']);_,actual,_=read_native(target);expected=original.geom[0]
    for step in order:
        expected={'translate':lambda g:translate(g,xoff=12.5,yoff=-30.25),
                  'rotate':lambda g:rotate(g,30.,origin=(0,0)),
                  'scale':lambda g:scale(g,xfact=1.2,yfact=.8,origin=(0,0))}[step](expected)
    assert expected.distance(actual.geom[0])<1e-7
    assert source.read_bytes()==blob and target.read_bytes()[91:260]==blob[91:260]
