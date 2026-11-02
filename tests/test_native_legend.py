"""A saved native legend must contain actual styled items and editable output."""
from pathlib import Path
import pytest
from mapgis_agent.core import Bridge,BridgeError,read_native,native_ids,annotation_text
from mapgis_agent.native_legend import prepare_native_legend,native_legend_info
from mapgis_agent.raster import file_sha


def example_items():
    return [
        {'kind':'region','name':'T1n2 灰岩','description':'灰岩与白云岩','code':100,'style':{'fill_color':3,'pattern':0}},
        {'kind':'line','name':'断层','description':'实测断层','code':200,'style':{'color':2,'width':.35,'linetype':1}},
        {'kind':'point','name':'矿点','description':'钨锡矿化点','code':300,'style':{'color':4,'symbol_no':1,'height':5.,'width':5.}},
    ]


def legend_case(tmp_path,items=None):
    from test_native_project_edit import project_case
    b,_,_=project_case(tmp_path)
    items=example_items() if items is None else items
    prepared=prepare_native_legend(b,items,{'name':'geology.cln','title':'地质图图例','bounds':[10.,10.,150.,100.],'columns':1},'legend')
    result=b.apply('legend')
    return b,items,prepared,result


def test_native_legend_mixed_items_save_relocate_and_fresh_native_readback(tmp_path):
    b,items,prepared,result=legend_case(tmp_path)
    output=Path(result['output']);before={p:file_sha(p) for p in output.parent.iterdir()}
    info=native_legend_info(tmp_path,str(output),file_sha(output))
    assert [v['kind'] for v in info['items']]==[v['kind'] for v in items]
    assert [(v['name'],v['description'],v['code']) for v in info['items']]==[(v['name'],v['description'],v['code']) for v in items]
    assert info['items'][0]['style']['fill_color']==3
    assert info['items'][1]['style']['width']==pytest.approx(.35,rel=1e-6)
    assert info['items'][2]['style']['symbol_no']==1
    assert info['items'][2]['style']['height']==pytest.approx(5.)
    wt=output.with_suffix('.wt');blob,reader,sections=read_native(wt)
    texts=[annotation_text(blob,sections,i) for i in native_ids(reader)]
    assert [t for t in texts if t] == ['地质图图例','矿点','断层','T1n2 灰岩']
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    assert prepared['vectors']['POLYGON']['active']==1
    assert prepared['vectors']['LINE']['active']>=2
    assert all(file_sha(path)==value for path,value in before.items())
    assert b.status('legend')['output_matches']
    assert prepare_native_legend(b,items,prepared['payload']['parameters'],'legend')['state']=='APPLIED'


def test_native_region_only_legend_exports_names_and_nonempty_regions(tmp_path):
    b,items,prepared,result=legend_case(tmp_path,example_items()[:1])
    assert prepared['vectors']['POLYGON']['active']==1
    data,reader,sections=read_native(Path(result['output']).with_suffix('.wt'))
    assert [annotation_text(data,sections,i) for i in native_ids(reader)]==['地质图图例','T1n2 灰岩']


def test_native_legend_zero_pattern_dimensions_duplicate_codes_and_signed_boundaries(tmp_path):
    items=[{'kind':'region','name':'纯色区','code':-2147483648,'style':{'pattern':0,'pattern_height':0,'pattern_width':0}},
           {'kind':'line','name':'同码线','code':-2147483648,'style':{'width':0}},
           {'kind':'point','name':'边界点','code':2147483647}]
    b,_,prepared,result=legend_case(tmp_path,items)
    info=native_legend_info(tmp_path,result['output'],file_sha(Path(result['output'])))
    assert [v['code'] for v in info['items']]==[v['code'] for v in items]
    assert info['items'][0]['style']['pattern_width']==0


@pytest.mark.parametrize('items,parameters',[
    ([],{}),
    ([{'kind':'region','name':'a'*100,'code':1}],{}),
    ([{'kind':'region','name':'a\0b','code':1}],{}),
    ([{'kind':'region','name':'rock','code':True}],{}),
    ([{'kind':'region','name':'rock','code':1,'style':{'width':2}}],{}),
    ([{'kind':'point','name':'point','code':1,'style':{'height':float('nan')}}],{}),
    (example_items(),{'bounds':[1,1,1,2]}),
    (example_items(),{'columns':0}),
    (example_items(),{'name':'../escape.cln'}),
])
def test_native_legend_invalid_semantics_fail_before_dll_call(tmp_path,items,parameters):
    with pytest.raises(BridgeError,match='LEGEND_'):
        prepare_native_legend(Bridge(tmp_path),items,{'name':'test.cln','title':'图例','bounds':[0,0,100,100],'columns':1}|parameters,'invalid')
