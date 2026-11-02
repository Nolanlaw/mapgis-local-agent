"""Exact native parameter circles/arcs, not polygon approximations."""
from pathlib import Path
import pytest
from mapgis_agent.core import Bridge,BridgeError,read_native
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import prepare_native_operation,native_file_info
from mapgis_agent.native_com import prepare_native_subset
from mapgis_agent.features import read_features
from mapgis_agent.raster import file_sha
from test_native_raster import native_settings

FRAME={'kind':'local','name':'native curve fixture','axis_order':'xy','unit':'unknown','reference_sha256':'d'*64}
# Captured first62 PNT_INFO bytes from normal original SDK property writes,
# native UpdateInfo/Save and a separate BAS60 save. All reserved bytes were zero.
SDK_CIRCLE='02000000000000002440010000009a99993e0000000000000000000000000000000000000000000000000000010000000000000000000000000000000000'
SDK_ARC='03000000000000002440000000000000b4429a99993e00000000000000000000000000000000000000000000010000000000000000000000000000000000'


def test_create_exact_native_circle_and_arc_bytes_then_bas60_save_reopen_and_semantic_read(tmp_path):
    b=Bridge(tmp_path);native_settings(tmp_path)
    styles=[{'point_type':2,'filled':False},{'point_type':2,'filled':True},{'point_type':3,'start_angle':0.,'end_angle':90.},
            {'point_type':3,'start_angle':0.,'end_angle':1.5707963267948966},{'point_type':3,'start_angle':30.,'end_angle':150.}]
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':[20+30*(i-1),20]},
        'properties':{'ID':7,'名称':'图元'+str(i)},'style':dict(radius_mm=10.,pen_width=.3,**style)} for i,style in enumerate(styles,1)]}
    prepare_creation(b,features,{'name':'curves.wt','map_scale':1,'crs':FRAME},'create');source=Path(b.apply('create')['output'])
    blob,reader,sections=read_native(source)
    assert blob[sections[0][0]+93+31:sections[0][0]+186]==bytes.fromhex(SDK_CIRCLE)
    assert blob[sections[0][0]+279+31:sections[0][0]+372]==bytes.fromhex(SDK_ARC)
    assert reader.point_params['radius_mm'].tolist()==[10.]*5
    report=read_features(str(source),file_sha(source));assert 'centre' in report['source']['geometry_semantics']
    assert report['collection']['features'][0]['style']['filled']==0
    assert 'height' not in report['collection']['features'][0]['style']
    from mapgis_agent.features import prepare_file_operation
    with pytest.raises(BridgeError,match='PARAMETRIC_GEOMETRY'):
        prepare_file_operation(b,'vector.buffer',str(source),file_sha(source),{'name':'buffer.wp'},{'distance':2},'bad-buffer')
    prepare_native_operation(b,'resave',str(source),file_sha(source),{},'native');target=Path(b.apply('native')['output'])
    after,other,new_sections=read_native(target)
    assert [g.wkt for g in other.geom]==[g.wkt for g in reader.geom] and list(other.data['ID'])==[7]*5
    assert all(blob[sections[0][0]+i*93+31:sections[0][0]+(i+1)*93]==after[new_sections[0][0]+i*93+31:new_sections[0][0]+(i+1)*93] for i in range(1,6))
    info=native_file_info(tmp_path,str(target),file_sha(target));assert info['active']==5 and info['native_bounds']==[10,10,150,30]


@pytest.mark.parametrize('style',[
    {'point_type':2},{'point_type':2,'radius_mm':-1},{'point_type':3,'radius_mm':10},
    {'point_type':3,'radius_mm':10,'start_angle':90,'end_angle':0},
    {'point_type':2,'radius_mm':10,'filled':2},{'point_type':1,'radius_mm':10},
])
def test_primitive_validation_rejects_missing_radius_invalid_span_and_inapplicable_parameters(tmp_path,style):
    b=Bridge(tmp_path)
    with pytest.raises(BridgeError,match='POINT_PRIMITIVE'):
        prepare_creation(b,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':{'type':'Point','coordinates':[0,0]},'properties':{'ID':1},'style':style}]},
            {'name':'invalid.wt','map_scale':1,'crs':FRAME},'invalid')


def test_native_subset_of_symbol_from_mixed_curve_file_keeps_normal_graphic_schema(tmp_path):
    b=Bridge(tmp_path);native_settings(tmp_path)
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':[i*10,0]},'properties':{'ID':7},
        'style':style} for i,style in enumerate([{'point_type':2,'radius_mm':3},{'point_type':1,'symbol_no':45,'height':2,'width':2}],1)]}
    prepare_creation(b,features,{'name':'mixed.wt','map_scale':1,'crs':FRAME},'mixed');source=Path(b.apply('mixed')['output'])
    result=prepare_native_subset(b,str(source),file_sha(source),[2],{'name':'symbol.wt'},'symbol')
    assert result['verification']['graphic_parameters_equal']


@pytest.mark.parametrize('kind,style,updates',[(2,{'radius_mm':10},{'border_color':5,'filled':1,'pen_width':.4}),
    (3,{'radius_mm':10,'start_angle':0,'end_angle':90},{'start_angle':30,'end_angle':150,'pen_width':.4})])
def test_primitive_graphic_patch_native_save_and_exact_recovery(tmp_path,kind,style,updates):
    b=Bridge(tmp_path);native_settings(tmp_path)
    prepare_creation(b,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':{'type':'Point','coordinates':[20,20]},
        'properties':{'ID':7},'style':{'point_type':kind,**style}}]},{'name':'curve.wt','map_scale':1,'crs':FRAME},'create')
    source=Path(b.apply('create')['output']);before=source.read_bytes()
    b.prepare(str(source),file_sha(source),[1],'style',style=updates);target=Path(b.apply('style')['output'])
    actual=read_features(str(target),file_sha(target))['collection']['features'][0]['style']
    assert all(actual[k]==pytest.approx(v) for k,v in updates.items())
    prepare_native_operation(b,'resave',str(target),file_sha(target),{},'native');saved=Path(b.apply('native')['output'])
    checked=read_features(str(saved),file_sha(saved))['collection']['features'][0]['style']
    assert all(checked[k]==pytest.approx(v) for k,v in updates.items())
    b.recover('style',str(target),file_sha(target),'undo');assert Path(b.apply('undo')['output']).read_bytes()==before
    with pytest.raises(BridgeError,match='POINT_PRIMITIVE'):
        b.prepare(str(source),file_sha(source),[1],'bad-radius',style={'radius_mm':20})


def test_circle_radius_is_drawing_mm_at_a_nonunit_projected_map_scale(tmp_path):
    b=Bridge(tmp_path);native_settings(tmp_path)
    prepare_creation(b,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':{'type':'Point','coordinates':[500000,3320000]},
        'properties':{'ID':1},'style':{'point_type':2,'radius_mm':10}}]},{'name':'projected.wt','map_scale':50000,'crs':{'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}},'create')
    source=Path(b.apply('create')['output']);blob,reader,sections=read_native(source)
    import io
    from pymapgis.reader import _read_crs
    assert _read_crs(io.BytesIO(blob))[1]==50.
    assert list(reader.geom[0].coords)==[(500000.,3320000.)]
    assert reader.point_params['radius_mm'].iloc[0]==10.
    info=native_file_info(tmp_path,str(source),file_sha(source));assert info['native_bounds']==[9990.,66390.,10010.,66410.]
