"""DXF text, units, holes and native styles/attributes have independent readback."""
from pathlib import Path
import json
import ezdxf,pyogrio,pytest
from mapgis_agent.core import Bridge,BridgeError,read_native,annotation_text
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha
from mapgis_agent.cad_exchange import prepare_dxf_export,dxf_source_info,prepare_dxf_import


def frame():return {'kind':'local','name':'CAD fixture','axis_order':'xy','unit':'metre','reference_sha256':'a'*64}


def create(b,name,geometry,text=None):
    feature={'type':'Feature','id':'1','geometry':geometry,'properties':{'ID':7,'岩性':'花岗岩'}}
    if text:feature.update(text=text,style={'color':1,'height':2.,'width':1.6,'angle':30.,'font_zh':1,'font_en':0})
    identity=Path(name).stem
    prepare_creation(b,{'type':'FeatureCollection','features':[feature]},{'name':name,'crs':frame(),'map_scale':1000},identity)
    return Path(b.apply(identity)['output'])


def test_dxf_chinese_text_holes_units_and_hash_bound_native_roundtrip(tmp_path):
    b=Bridge(tmp_path);point=create(b,'labels.wt',{'type':'Point','coordinates':[10,20]},'中文地层')
    polygon=create(b,'areas.wp',{'type':'Polygon','coordinates':[[[0,0],[30,0],[30,40],[0,40],[0,0]],[[5,5],[5,10],[10,10],[10,5],[5,5]]]})
    files=[{'file':str(v),'expected_sha256':file_sha(v)} for v in [point,polygon]]
    receipt=prepare_dxf_export(b,files,{'name':'map.dxf','crs':frame(),'units':'millimetre'},'export')
    output=Path(b.apply('export')['output']);doc=ezdxf.readfile(output)
    assert doc.units==4
    text=list(doc.modelspace().query('TEXT'));assert len(text)==1 and text[0].dxf.text=='中文地层'
    assert tuple(text[0].dxf.insert)[:2]==pytest.approx([10000,20000]) and text[0].dxf.height==2000
    hatch=list(doc.modelspace().query('HATCH'));assert len(hatch)==1 and len(hatch[0].paths)==2
    independent=pyogrio.read_dataframe(output)
    assert any(g.geom_type in {'Polygon','MultiPolygon'} and g.area==pytest.approx(1175e6) for g in independent.geometry)
    info=dxf_source_info(str(output));assert info['entities_by_type']=={'HATCH':1,'TEXT':1}
    for kind,suffix in [('point','wt'),('polygon','wp')]:
        prepared=prepare_dxf_import(b,str(output),file_sha(output),info['members'],
            {'kind':kind,'layers':[point.stem if kind=='point' else polygon.stem],'source_units':'millimetre'},
            {'name':'restored.'+suffix,'crs':frame(),'map_scale':1000},'import-'+kind)
        target=Path(b.apply('import-'+kind)['output']);raw,reader,sections=read_native(target)
        assert reader.data.iloc[0]['岩性']=='花岗岩' and reader.data.iloc[0]['ID']==7
        if kind=='point':
            assert annotation_text(raw,sections,1)=='中文地层'
            assert reader.geom[0].x==pytest.approx(10) and reader.point_params.iloc[0]['angle']==pytest.approx(30)
        else:assert reader.geom[0].area==pytest.approx(1175)
        assert prepared['metadata_restored'] and prepared['source_preserved']
        assert prepared['restoration_mode']=='EXACT_NATIVE_SNAPSHOT_AFTER_DXF_GEOMETRY_CHECK'
        assert file_sha(target)==file_sha(point if kind=='point' else polygon)
    assert all(file_sha(Path(v['file']))==v['expected_sha256'] for v in files)


def test_dxf_curve_tolerance_is_explicit_and_source_changes_block_apply(tmp_path):
    b=Bridge(tmp_path);source=tmp_path/'arc.dxf';doc=ezdxf.new('R2013');doc.units=6
    doc.modelspace().add_arc((0,0),10,0,90,dxfattribs={'layer':'curve'});doc.saveas(source)
    info=dxf_source_info(str(source));parameters={'kind':'line','layers':['curve'],'source_units':'metre'}
    template={'name':'arc.wl','crs':frame(),'map_scale':1000}
    with pytest.raises(BridgeError,match='DXF_CURVE_TOLERANCE'):
        prepare_dxf_import(b,str(source),file_sha(source),info['members'],parameters,template,'bad')
    prepared=prepare_dxf_import(b,str(source),file_sha(source),info['members'],{**parameters,'curve_tolerance':.01},template,'arc')
    assert prepared['curve_approximation']['tolerance_source_units']==.01
    _,reader,_=read_native(Path(prepared['stage'])/'files/arc.wl')
    import math
    coordinates=list(reader.geom[0].coords)
    assert all(abs(math.hypot(x,y)-10)<1e-9 for x,y in coordinates)
    assert all(10-math.hypot((a[0]+c[0])/2,(a[1]+c[1])/2)<=.01+1e-9 for a,c in zip(coordinates,coordinates[1:]))
    source.write_bytes(source.read_bytes()+b'\n')
    with pytest.raises(BridgeError,match='STALE_SOURCE'):b.apply('arc')


def test_dxf_unknown_units_and_unsupported_selected_entities_are_not_silent(tmp_path):
    b=Bridge(tmp_path);source=tmp_path/'unknown.dxf';doc=ezdxf.new('R2013');doc.units=0
    doc.modelspace().add_point((1,2));doc.saveas(source);info=dxf_source_info(str(source))
    with pytest.raises(BridgeError,match='DXF_UNIT_CONTEXT'):
        prepare_dxf_import(b,str(source),file_sha(source),info['members'],{'kind':'point','layers':['0'],'source_units':'unitless'},
            {'name':'point.wt','crs':frame(),'map_scale':1000},'unit')
    doc.units=6;doc.blocks.new('BLOCK').add_line((0,0),(1,1));doc.modelspace().add_blockref('BLOCK',(0,0));doc.saveas(source)
    info=dxf_source_info(str(source));assert info['unsupported_modelspace_entities']=={'INSERT':1}
    with pytest.raises(BridgeError,match='DXF_UNSUPPORTED_ENTITY'):
        prepare_dxf_import(b,str(source),file_sha(source),info['members'],{'kind':'point','layers':['0'],'source_units':'metre'},
            {'name':'point.wt','crs':frame(),'map_scale':1000},'unsupported')
