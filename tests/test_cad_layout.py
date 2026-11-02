"""Independent WCS transforms and source identity for CAD block/layout imports."""
import json
import math
from pathlib import Path

import ezdxf
import numpy as np
import pytest
from pymapgis import Reader

from mapgis_agent.cad_exchange import dxf_source_info, prepare_dxf_import
from mapgis_agent.core import Bridge, BridgeError
from mapgis_agent.raster import file_sha


def frame():
    return {'kind':'local','name':'explicit CAD drawing plane','axis_order':'xy','unit':'metre','reference_sha256':'a'*64}


def run(folder,doc,options,kind='line'):
    source=folder/'drawing.dxf';doc.saveas(source);digest=file_sha(source);bridge=Bridge(folder)
    info=dxf_source_info(str(source))
    receipt=prepare_dxf_import(bridge,str(source),digest,info['members'],
        {'kind':kind,'layers':['target'],'source_units':'metre','restore_bound_metadata':False,**options},
        {'name':'new.'+{'line':'wl','point':'wt','polygon':'wp'}[kind],'crs':frame(),'map_scale':1000},'import')
    output=bridge.apply('import')['output'];assert file_sha(source)==digest
    return receipt,Reader(output,make_valid=False)


def transform(point,insert,scale,angle,base=(0.,0.)):
    x=(point[0]-base[0])*scale[0];y=(point[1]-base[1])*scale[1];angle=math.radians(angle)
    return [insert[0]+x*math.cos(angle)-y*math.sin(angle),insert[1]+x*math.sin(angle)+y*math.cos(angle)]


def test_nested_nonuniform_mirror_blocks_follow_composed_wcs_matrices(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6
    inner=doc.blocks.new('inner',base_point=(1,1));leaf=inner.add_line((1,2),(3,4))
    outer=doc.blocks.new('outer');outer.add_blockref('inner',(4,5),dxfattribs={'xscale':2.,'yscale':1.,'rotation':30.})
    root=doc.modelspace().add_blockref('outer',(100,200),dxfattribs={'layer':'target','xscale':-1.,'yscale':2.,'rotation':90.})
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True})
    expected=[transform(transform(p,(4,5),(2,1),30,(1,1)),(100,200),(-1,2),90) for p in [(1,2),(3,4)]]
    np.testing.assert_allclose(list(reader.geom[0].coords),expected,rtol=0,atol=1e-12)
    row=receipt['source_record_mapping'][0]
    assert row['root_handle']==root.dxf.handle and row['origin_handle']==leaf.dxf.handle
    assert row['handle']==leaf.dxf.handle
    assert reader.data['CAD_ROOT'].iloc[0]==root.dxf.handle
    assert reader.data['CAD_LAYER'].iloc[0]=='target'


def test_multi_insert_covers_all_rows_and_columns(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('station');block.add_point((1,0))
    root=doc.modelspace().add_blockref('station',(10,20),dxfattribs={'layer':'target','xscale':2.,'yscale':3.,'rotation':90.})
    root.grid(size=(2,2),spacing=(3,4))
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True},'point')
    assert len(reader.geom)==4
    expected={(10.,22.),(10.,26.),(7.,22.),(7.,26.)}
    assert {(round(g.x,10),round(g.y,10)) for g in reader.geom}==expected
    assert len({row['virtual_key'] for row in receipt['source_record_mapping']})==4


def test_nonuniform_circle_is_a_real_transformed_ellipse_before_flattening(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('curve');block.add_circle((2,3),1)
    doc.modelspace().add_blockref('curve',(10,20),dxfattribs={'layer':'target','xscale':2.,'yscale':.5,'rotation':90.})
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True,'curve_tolerance':.001})
    points=np.asarray(reader.geom[0].coords)
    np.testing.assert_allclose(((points[:,0]-8.5)/.5)**2+((points[:,1]-24)/2)**2,1.,atol=2e-12)
    assert receipt['curve_approximation']['curved_entities']==1


def test_paper_layout_selection_does_not_import_modelspace(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6
    doc.modelspace().add_line((100,100),(200,200),dxfattribs={'layer':'target'})
    paper=doc.layouts.new('Sheet A');paper.add_line((1,2),(4,6),dxfattribs={'layer':'target'})
    receipt,reader=run(tmp_path,doc,{'layout':'Sheet A','paper_coordinate_units_confirmed':True})
    assert len(reader.geom)==1 and list(reader.geom[0].coords)==[(1.,2.),(4.,6.)]
    assert receipt['layout']['name']=='Sheet A' and receipt['layout']['domain']=='paper_drawing_coordinates'


def test_paper_units_must_be_explicitly_confirmed(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;doc.layouts.new('Sheet A').add_line((1,2),(4,6),dxfattribs={'layer':'target'})
    with pytest.raises(BridgeError,match='DXF_PAPER_UNITS'):
        run(tmp_path,doc,{'layout':'Sheet A'})


def test_mirrored_hatch_holes_are_retained_in_native_region(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('area')
    hatch=block.add_hatch();hatch.paths.add_polyline_path([(0,0),(20,0),(20,12),(0,12)],is_closed=True)
    hatch.paths.add_polyline_path([(5,4),(10,4),(10,8),(5,8)],is_closed=True)
    doc.modelspace().add_blockref('area',(100,200),dxfattribs={'layer':'target','xscale':-2.,'yscale':.5,'rotation':30.})
    receipt,reader=run(tmp_path,doc,{'expand_blocks':True},'polygon')
    assert len(reader.geom)==1 and reader.geom[0].is_valid and reader.geom[0].area==pytest.approx(220.)
    parts=list(reader.geom[0].geoms) if reader.geom[0].geom_type=='MultiPolygon' else [reader.geom[0]]
    assert sum(len(g.interiors) for g in parts)==1
    assert receipt['selected_entities']=={'HATCH':1}


def test_selected_paper_viewport_is_explicitly_rejected_until_projection_is_requested(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;paper=doc.layouts.new('Sheet A')
    paper.add_viewport(center=(50,50),size=(20,20),view_center_point=(0,0),view_height=100,
        dxfattribs={'layer':'target'})
    with pytest.raises(BridgeError,match='DXF_UNSUPPORTED_ENTITY.*VIEWPORT'):
        run(tmp_path,doc,{'layout':'Sheet A','paper_coordinate_units_confirmed':True})


def test_actual_block_clip_cannot_be_silently_lost(tmp_path):
    from ezdxf.xclip import XClip
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('clip');block.add_line((0,0),(10,0))
    root=doc.modelspace().add_blockref('clip',(0,0),dxfattribs={'layer':'target'})
    XClip(root).set_block_clipping_path([(0,-1),(5,1)])
    with pytest.raises(BridgeError,match='DXF_XCLIP'):
        run(tmp_path,doc,{'expand_blocks':True})


def test_recursive_or_clipped_block_content_is_not_silently_expanded(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('loop');block.add_blockref('loop',(0,0))
    doc.modelspace().add_blockref('loop',(0,0),dxfattribs={'layer':'target'})
    with pytest.raises(BridgeError,match='DXF_BLOCK_CYCLE'):
        run(tmp_path,doc,{'expand_blocks':True})


def test_block_expansion_budget_is_checked_before_unbounded_array_expansion(tmp_path):
    doc=ezdxf.new('R2013');doc.units=6;block=doc.blocks.new('large');block.add_point((0,0))
    doc.modelspace().add_blockref('large',(0,0),dxfattribs={'layer':'target'}).grid(size=(100,100),spacing=(1,1))
    with pytest.raises(BridgeError,match='DXF_EXPANDED_ENTITY_LIMIT'):
        run(tmp_path,doc,{'expand_blocks':True,'max_expanded_entities':100},'point')
