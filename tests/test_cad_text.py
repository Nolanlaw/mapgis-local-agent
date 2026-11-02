"""Exact synthetic font outlines: no system-font matching or CAD self-oracle."""
import ezdxf
import pytest
from ezdxf.enums import TextEntityAlignment
from ezdxf.xclip import XClip
from shapely.geometry import Polygon

from cad_font_fixture import make_font
from test_cad_clipping import run
from mapgis_agent.core import BridgeError
from mapgis_agent.raster import file_sha


def options(folder,**extra):
    font=make_font(folder/'exact.ttf')
    return {'text_mode':'outline','curve_tolerance':.001,
        'font_bindings':[{'style':'Standard','file':str(font),'expected_sha256':file_sha(font)}],**extra}


def doc_text(text='A',**props):
    doc=ezdxf.new('R2013');doc.units=6
    entity=doc.modelspace().add_text(text,dxfattribs={'layer':'target','height':7.,**props})
    return doc,entity


def test_exact_glyph_hole_and_chinese_cmap_are_preserved(tmp_path):
    doc,_=doc_text('A地');receipt,reader=run(tmp_path,doc,options(tmp_path),'polygon')
    assert len(reader.geom)==2 and sum(g.area for g in reader.geom)==pytest.approx(44.)
    assert sorted(tuple(g.bounds) for g in reader.geom)==[(0.,0.,6.,7.),(7.,0.,13.,7.)]
    parts=[list(g.geoms) if g.geom_type=='MultiPolygon' else [g] for g in reader.geom]
    assert all(len(value)==1 and len(value[0].interiors)==1 for value in parts)
    assert all(row['source_text']=='A地' for row in receipt['source_record_mapping'])


def test_top_center_alignment_uses_declared_cap_height(tmp_path):
    doc,text=doc_text();text.set_placement((20,30),align=TextEntityAlignment.TOP_CENTER)
    _,reader=run(tmp_path,doc,options(tmp_path),'polygon')
    assert tuple(reader.geom[0].bounds)==(17.,23.,23.,30.)


@pytest.mark.parametrize('align,area,height',[(TextEntityAlignment.FIT,22*20/6,7),
                                            (TextEntityAlignment.ALIGNED,22*(20/6)**2,7*20/6)])
def test_fit_and_aligned_text_use_the_two_placement_points(tmp_path,align,area,height):
    doc,text=doc_text();text.set_placement((10,10),(30,10),align=align)
    _,reader=run(tmp_path,doc,options(tmp_path),'polygon')
    assert reader.geom[0].area==pytest.approx(area)
    assert tuple(reader.geom[0].bounds)==pytest.approx((10,10,30,10+height))


def test_oblique_width_and_generation_mirror_are_real_glyph_transforms(tmp_path):
    doc,_=doc_text(width=2.,oblique=45.,text_generation_flag=2)
    _,reader=run(tmp_path,doc,options(tmp_path),'polygon')
    expected=Polygon([(0,0),(-12,0),(-26,7),(-14,7)],
        [[(-4,1),(-14,6),(-22,6),(-12,1)]])
    assert reader.geom[0].symmetric_difference(expected).area<1e-11


def test_nonuniform_mirrored_block_text_is_outlined_before_transform(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('label')
    block.add_text('A',dxfattribs={'height':7.})
    doc.modelspace().add_blockref('label',(100,200),dxfattribs={'layer':'target','xscale':-2.,'yscale':.5,'rotation':90.})
    _,reader=run(tmp_path,doc,options(tmp_path,expand_blocks=True),'polygon')
    assert tuple(reader.geom[0].bounds)==pytest.approx((96.5,188,100,200))
    assert reader.geom[0].area==pytest.approx(22.)


def test_text_clipping_uses_glyph_surface_instead_of_anchor(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('label');block.add_text('A',dxfattribs={'height':7.})
    root=doc.modelspace().add_blockref('label',(0,0),dxfattribs={'layer':'target'})
    XClip(root).set_block_clipping_path([(0,0),(3,7)])
    _,reader=run(tmp_path,doc,options(tmp_path,expand_blocks=True,apply_block_clipping=True),'polygon')
    assert len(reader.geom)==1 and reader.geom[0].area==pytest.approx(11.)
    assert not reader.geom[0].interiors


def test_attached_values_replace_variable_definitions_and_constants_render_once(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('labels')
    block.add_attdef('VALUE',(0,0),height=7)
    block.add_attdef('CONST',(10,0),text='A',height=7,dxfattribs={'flags':2})
    root=doc.modelspace().add_blockref('labels',(100,200),dxfattribs={'layer':'target'})
    root.add_auto_attribs({'VALUE':'A'})
    attached=[a for a in root.attribs if a.dxf.tag=='VALUE'][0]
    receipt,reader=run(tmp_path,doc,options(tmp_path,expand_blocks=True),'polygon')
    assert len(reader.geom)==2
    assert sorted(g.bounds for g in reader.geom)==[(100.,200.,106.,207.),(110.,200.,116.,207.)]
    rows=receipt['source_record_mapping'];assert any(row['origin_handle']==attached.dxf.handle for row in rows)


def test_line_outline_keeps_both_external_and_internal_contours(tmp_path):
    doc,_=doc_text();_,reader=run(tmp_path,doc,options(tmp_path),'line')
    assert len(reader.geom)==2
    assert sorted(g.length for g in reader.geom)==pytest.approx([18.,26.])


def test_quadratic_curve_is_flattened_after_nonuniform_transform(tmp_path):
    import math
    from shapely.geometry import Point
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('curve')
    block.add_text('Q',dxfattribs={'height':7.})
    doc.modelspace().add_blockref('curve',(0,0),dxfattribs={'layer':'target','xscale':2.,'yscale':.5})
    _,reader=run(tmp_path,doc,options(tmp_path,expand_blocks=True),'polygon')
    geometry=reader.geom[0];assert geometry.area==pytest.approx(14.,abs=.01)
    assert max(Point(24*t-12*t*t,7*t*(1-t)).distance(geometry.boundary)
               for t in (i/10000 for i in range(10001)))<=.001


@pytest.mark.parametrize('text',['Z','A\u0301','\u0627'])
def test_missing_glyph_or_contextual_shaping_is_explicitly_rejected(tmp_path,text):
    doc,_=doc_text(text)
    with pytest.raises(BridgeError,match='DXF_TEXT_(GLYPH|SHAPING)'):
        run(tmp_path,doc,options(tmp_path),'polygon')


def test_font_hash_is_checked_before_rendering_without_fallback(tmp_path):
    doc,_=doc_text();params=options(tmp_path);params['font_bindings'][0]['expected_sha256']='0'*64
    with pytest.raises(BridgeError,match='STALE_FONT'):
        run(tmp_path,doc,params,'polygon')


def test_rich_mtext_is_not_presented_as_single_line_glyph_layout(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;doc.modelspace().add_mtext('A\\P地',dxfattribs={'layer':'target'})
    with pytest.raises(BridgeError,match='DXF_MTEXT_OUTLINE'):
        run(tmp_path,doc,options(tmp_path),'polygon')
