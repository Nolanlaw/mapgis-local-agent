"""Physical page elements use the same centred aspect fit as native plotting."""
from pathlib import Path
import pytest
from mapgis_agent.core import Bridge,BridgeError,read_native,native_ids,annotation_text
from mapgis_agent.cartography import prepare_map_frame


def template(name):
    return {'name':name,'crs':{'kind':'local','name':'fixed plot native frame','axis_order':'xy','unit':'unknown','reference_sha256':'a'*64},'map_scale':1,'style':{'color':1}}


def test_page_north_arrow_has_requested_paper_length_with_centred_letterboxing(tmp_path):
    b=Bridge(tmp_path);view={'native_bounds':[100,200,500,400],'paper_mm':[200,200],'coordinate_frame':'native'}
    params={'view':view,'anchor_mm':[150,125],'element_id':'north','length_mm':20,'angle_degrees':0,'direction_kind':'declared'}
    prepare_map_frame(b,'page.north_arrow',params,template('north.wl'),'north');out=b.apply('north')
    _,r,_=read_native(Path(out['output']));assert len(r.geom)==3
    assert list(r.geom[0].coords)==pytest.approx([(400,350),(400,390)])
    assert r.data['element_id'].tolist()==['north']*3
    prepare_map_frame(b,'page.north_label',params|{'font_mm':3},template('north.wt'),'north-label');text=b.apply('north-label')
    blob,p,s=read_native(Path(text['output']));assert annotation_text(blob,s,1)=='N'
    assert p.geom[0].x==pytest.approx(400) and p.geom[0].y>390


def test_page_scale_bar_and_labels_have_declared_ground_distance_and_physical_ticks(tmp_path):
    b=Bridge(tmp_path);view={'native_bounds':[0,0,4000,2000],'paper_mm':[200,100],'coordinate_frame':'native'}
    params={'view':view,'anchor_mm':[20,20],'element_id':'scale','distance_metres':1000,'metres_per_native_unit':.5,'segments':4,'font_mm':3}
    prepare_map_frame(b,'page.scale_bar',params,template('scale.wl'),'scale');out=b.apply('scale')
    _,r,_=read_native(Path(out['output']));assert len(r.geom)==6
    assert r.geom[0].length==pytest.approx(2000)
    assert list(r.geom[0].coords)==pytest.approx([(400,400),(2400,400)])
    assert r.geom[1].length==pytest.approx(40)
    prepare_map_frame(b,'page.scale_labels',params,template('scale.wt'),'labels');labels=b.apply('labels')
    blob,p,s=read_native(Path(labels['output']));assert [annotation_text(blob,s,i) for i in native_ids(p)]==['0','0.25','0.5','0.75','1 km']
    assert list(p.point_params['height'])==pytest.approx([60]*5)


def test_page_title_uses_native_positions_and_requested_physical_font_size(tmp_path):
    b=Bridge(tmp_path);view={'native_bounds':[0,0,4000,2000],'paper_mm':[200,100],'coordinate_frame':'native'}
    params={'view':view,'anchor_mm':[40,80],'element_id':'title','text':'地质图','font_mm':4}
    prepare_map_frame(b,'page.title',params,template('title.wt'),'title');out=b.apply('title')
    blob,r,s=read_native(Path(out['output']));assert annotation_text(blob,s,1)=='地质图'
    assert (r.geom[0].x,r.geom[0].y)==pytest.approx((800,1600))
    assert r.point_params['height'][0]==pytest.approx(80)


@pytest.mark.parametrize('part,delta',[
    ('page.title',{'anchor_mm':[201,80]}),('page.title',{'font_mm':0}),('page.title',{'text':''}),
    ('page.north_arrow',{'angle_degrees':True}),('page.north_arrow',{'direction_kind':'true_north_guessed'}),
    ('page.north_arrow',{'length_mm':500}),('page.scale_bar',{'metres_per_native_unit':0}),
    ('page.scale_bar',{'distance_metres':float('nan')}),('page.scale_bar',{'segments':True}),
    ('page.scale_bar',{'distance_metres':1000000}),
])
def test_page_elements_reject_invalid_units_or_off_page_elements(tmp_path,part,delta):
    parameters={'view':{'native_bounds':[0,0,4000,2000],'paper_mm':[200,100],'coordinate_frame':'native'},'anchor_mm':[40,60],
        'element_id':'item','text':'图例','font_mm':3,'length_mm':20,'angle_degrees':0,'direction_kind':'declared',
        'distance_metres':1000,'metres_per_native_unit':1,'segments':4}|delta
    with pytest.raises(BridgeError,match='PAGE_'):
        prepare_map_frame(Bridge(tmp_path),part,parameters,template('item.wt' if part=='page.title' else 'item.wl'),'invalid')
