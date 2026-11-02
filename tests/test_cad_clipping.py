"""Independent coordinate and clipping oracles for explicit CAD composition."""
import math

import ezdxf
import numpy as np
import pytest
from ezdxf.xclip import XClip
from pymapgis import Reader

from mapgis_agent.cad_exchange import dxf_source_info, prepare_dxf_import
from mapgis_agent.core import Bridge, BridgeError
from mapgis_agent.raster import file_sha


def run(folder, doc, options, kind='line', layers=None):
    source=folder/'drawing.dxf';doc.saveas(source);digest=file_sha(source)
    info=dxf_source_info(str(source));bridge=Bridge(folder)
    receipt=prepare_dxf_import(bridge,str(source),digest,info['members'],
        {'kind':kind,'layers':layers or ['target'],'source_units':'metre',
         'restore_bound_metadata':False,**options},
        {'name':'new.'+{'line':'wl','point':'wt','polygon':'wp'}[kind],
         'crs':{'kind':'local','name':'explicit drawing plane','axis_order':'xy',
                'unit':'metre','reference_sha256':digest},'map_scale':1000},'import')
    output=bridge.apply('import')['output'];assert file_sha(source)==digest
    return receipt,Reader(output,make_valid=False)


def test_nested_clip_intersection_is_transformed_with_its_block(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6
    inner=doc.blocks.new('inner');leaf=inner.add_line((0,0),(10,0))
    outer=doc.blocks.new('outer');child=outer.add_blockref('inner',(0,0))
    XClip(child).set_block_clipping_path([(2,-1),(8,1)])
    root=doc.modelspace().add_blockref('outer',(100,200),
        dxfattribs={'layer':'target','rotation':90.,'xscale':2.,'yscale':.5})
    XClip(root).set_block_clipping_path([(4,-1),(6,1)])
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True,'apply_block_clipping':True})
    assert len(reader.geom)==1
    np.testing.assert_allclose(list(reader.geom[0].coords),[(100,208),(100,212)],atol=1e-12,rtol=0)
    assert receipt['source_record_mapping'][0]['origin_handle']==leaf.dxf.handle
    assert receipt['clipping']['active_block_clips']==2


@pytest.mark.parametrize('kind',['line','polygon'])
def test_nonconvex_clip_keeps_disconnected_native_parts(tmp_path,kind):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('body')
    if kind=='line':block.add_line((-1,5),(11,5))
    else:block.add_hatch().paths.add_polyline_path([(-1,4),(11,4),(11,6),(-1,6)],is_closed=True)
    root=doc.modelspace().add_blockref('body',(0,0),dxfattribs={'layer':'target'})
    XClip(root).set_block_clipping_path([(0,0),(10,0),(10,10),(7,10),(7,3),(3,3),(3,10),(0,10)])
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True,'apply_block_clipping':True},kind)
    assert len(reader.geom)==2
    assert sorted(round(g.bounds[0],9) for g in reader.geom)==[0,7]
    assert sum(g.length if kind=='line' else g.area for g in reader.geom)==pytest.approx(6 if kind=='line' else 12)
    rows=receipt['source_record_mapping'];assert rows[0]['origin_handle']==rows[1]['origin_handle']
    assert [r['clip_part'] for r in rows]==[1,2]


def test_disabled_clip_does_not_trim_or_require_enabled_option(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('body');block.add_line((0,0),(10,0))
    root=doc.modelspace().add_blockref('body',(0,0),dxfattribs={'layer':'target'})
    clip=XClip(root);clip.set_block_clipping_path([(0,-1),(5,1)]);clip.disable_clipping()
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True})
    assert list(reader.geom[0].coords)==[(0.,0.),(10.,0.)]


def test_each_multi_insert_cell_has_its_own_transformed_clip(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('body');block.add_line((0,0),(8,0))
    root=doc.modelspace().add_blockref('body',(0,0),dxfattribs={'layer':'target'})
    root.grid(size=(2,2),spacing=(10,10));XClip(root).set_block_clipping_path([(2,-1),(5,1)])
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True,'apply_block_clipping':True})
    assert sorted(tuple(round(v,9) for v in g.bounds) for g in reader.geom)==[
        (2.,0.,5.,0.),(2.,10.,5.,10.),(12.,0.,15.,0.),(12.,10.,15.,10.)]
    assert receipt['clipping']['active_block_clips']==4


def test_clipped_points_use_boundary_inclusion_without_inventing_text_extents(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('body')
    block.add_point((0,0));block.add_point((1,1));block.add_point((2,2))
    root=doc.modelspace().add_blockref('body',(0,0),dxfattribs={'layer':'target'})
    XClip(root).set_block_clipping_path([(0,0),(1,1)])
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True,'apply_block_clipping':True},'point')
    assert {(g.x,g.y) for g in reader.geom}=={(0.,0.),(1.,1.)}
    assert receipt['clipping']['fully_clipped_entities']==1


def paper_view(doc,**extra):
    paper=doc.layouts.new('Sheet A')
    vp=paper.add_viewport(center=(100,50),size=(60,40),view_center_point=(4,-3),view_height=20,
        dxfattribs={'layer':'target','view_direction_vector':(0,0,1),
                   'view_target_point':(1000,2000,0),'view_twist_angle':90.,**extra})
    return paper,vp


def options(**extra):
    return {'layout':'Sheet A','paper_coordinate_units_confirmed':True,
            'compose_viewports':True,'viewport_model_layers':['geology'],**extra}


def test_top_view_uses_dcs_center_and_wcs_target_with_twist(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6
    doc.modelspace().add_line((1003,2004),(1005,2006),dxfattribs={'layer':'geology'})
    _,vp=paper_view(doc)
    receipt,reader=run(tmp_path,doc,options())
    expected=[]
    for x,y in [(1003,2004),(1005,2006)]:
        x-=1000;y-=2000
        expected.append((100+2*(-y-4),50+2*(x+3)))
    np.testing.assert_allclose(list(reader.geom[0].coords),expected,rtol=0,atol=1e-12)
    assert receipt['source_record_mapping'][0]['viewport_handle']==vp.dxf.handle


def test_viewport_rectangular_clip_and_frozen_layers(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;doc.layers.new('blocked')
    space=doc.modelspace();space.add_line((-100,0),(100,0),dxfattribs={'layer':'geology'})
    block=doc.blocks.new('unit');block.add_line((-100,2),(100,2),dxfattribs={'layer':'blocked'})
    space.add_blockref('unit',(0,0),dxfattribs={'layer':'geology'})
    paper=doc.layouts.new('Sheet A');vp=paper.add_viewport(center=(50,50),size=(20,20),
        view_center_point=(0,0),view_height=20,dxfattribs={'layer':'target'})
    vp.frozen_layers=['blocked']
    receipt,reader=run(tmp_path,doc,options(expand_blocks=True))
    assert len(reader.geom)==1
    assert list(reader.geom[0].coords)==[(40.,50.),(60.,50.)]
    assert receipt['clipping']['hidden_entities']>=1


def test_nonrectangular_viewport_uses_actual_boundary_entity(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6
    doc.modelspace().add_line((-100,0),(100,0),dxfattribs={'layer':'geology'})
    paper=doc.layouts.new('Sheet A');vp=paper.add_viewport(center=(50,50),size=(20,20),
        view_center_point=(0,0),view_height=20,dxfattribs={'layer':'target'})
    border=paper.add_lwpolyline([(40,40),(60,40),(50,60)],close=True,dxfattribs={'layer':'border'})
    vp.dxf.clipping_boundary_handle=border.dxf.handle;vp.dxf.flags|=65536
    receipt,reader=run(tmp_path,doc,options())
    assert list(reader.geom[0].coords)==[(45.,50.),(55.,50.)]


@pytest.mark.parametrize('setting',[{'view_direction_vector':(1,0,1)},{'view_direction_vector':(0,0,0)},
                                   {'flags':1},{'flags':2},{'render_mode':3}])
def test_unverified_view_modes_are_rejected_before_planar_projection(tmp_path,setting):
    doc=ezdxf.new('R2013');doc.units=6
    doc.modelspace().add_line((1003,2004),(1005,2006),dxfattribs={'layer':'geology'})
    paper_view(doc,**setting)
    with pytest.raises(BridgeError,match='DXF_VIEWPORT_PROFILE'):
        run(tmp_path,doc,options())


def test_viewport_model_layer_selection_is_explicit(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;paper_view(doc)
    with pytest.raises(BridgeError,match='DXF_VIEWPORT_LAYERS'):
        run(tmp_path,doc,{'layout':'Sheet A','paper_coordinate_units_confirmed':True,'compose_viewports':True})


def test_clip_does_not_claim_to_trim_annotation_from_anchor_point(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('label')
    block.add_text('a long label',dxfattribs={'insert':(0,0),'height':3})
    root=doc.modelspace().add_blockref('label',(0,0),dxfattribs={'layer':'target'})
    XClip(root).set_block_clipping_path([(0,-1),(1,1)])
    with pytest.raises(BridgeError,match='DXF_CLIPPED_TEXT'):
        run(tmp_path,doc,{'expand_blocks':True,'apply_block_clipping':True},'point')


@pytest.mark.parametrize('setting',[{'has_front_clipping_plane':1},{'has_back_clipping_plane':1},
                                   {'origin':(1,0,0)},{'extrusion':(1,0,0)}])
def test_spatial_filter_depth_or_unhandled_ocs_is_not_silently_ignored(tmp_path,setting):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('body');block.add_line((0,0),(10,0))
    root=doc.modelspace().add_blockref('body',(0,0),dxfattribs={'layer':'target'})
    clip=XClip(root);clip.set_block_clipping_path([(0,-1),(5,1)])
    for key,value in setting.items():setattr(clip.get_spatial_filter().dxf,key,value)
    with pytest.raises(BridgeError,match='DXF_XCLIP_PROFILE'):
        run(tmp_path,doc,{'expand_blocks':True,'apply_block_clipping':True})
