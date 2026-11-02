"""Native graphic edits update point information and its renderer bounds."""
from pathlib import Path
import json,struct
import pytest

from mapgis_agent.core import Bridge,BridgeError,read_native,native_ids
from mapgis_agent.creation import prepare_creation
from mapgis_agent.features import read_features
from mapgis_agent.native_backend import prepare_native_operation,native_file_info
from mapgis_agent.raster import file_sha
from test_native_raster import native_settings


def case(root,projected=False):
    bridge=Bridge(root);native_settings(root)
    frame=({'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'} if projected else
        {'kind':'local','name':'native text parameter fixture','unit':'unknown','axis_order':'xy','reference_sha256':'0'*64})
    features=[]
    for i,(x,y) in enumerate([(0,0),(25,0),(0,20),(25,20)],1):
        item={'type':'Feature','id':str(i),'geometry':{'type':'Point','coordinates':[500000+x,3320000+y] if projected else [x,y]},
            'properties':{'ID':7,'name':'点'+str(i)},'style':{'height':3.,'width':2.,'color':1}}
        if i<=2:item['text']='地质T#-2#=d'
        else:item['style'].update(point_type=1,symbol_no=1,pen_width=.1)
        features.append(item)
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},
        {'name':'points.wt','map_scale':50000 if projected else 1000,'crs':frame},'source')
    return bridge,Path(bridge.apply('source')['output'])


@pytest.mark.parametrize('projected',[False,True])
def test_native_text_and_symbol_graphic_changes_preserve_records_and_recompute_boxes(tmp_path,projected):
    bridge,source=case(tmp_path,projected);before=source.read_bytes();old,reader,sections=read_native(source)
    updates=[{'record_id':1,'style':{'height':6.,'width':4.,'spacing':.2,'font_zh':2,'font_en':1,'font_style':1,'angle':15.}},
        {'record_id':3,'style':{'height':8.,'width':5.,'pen_width':.4,'angle':30.,'color':3,'layer':2}}]
    prepared=prepare_native_operation(bridge,'point.style',str(source),file_sha(source),{'updates':updates},'style')
    output=bridge.apply('style');blob,actual,new_sections=read_native(Path(output['output']))
    assert prepared['native_execution']['pid']!=prepared['native_reopen']['pid']
    original=read_features(str(source),file_sha(source))['collection']['features']
    after=read_features(output['output'],file_sha(Path(output['output'])))['collection']['features']
    assert native_ids(actual)==[1,2,3,4]
    for previous,current in zip(original,after):
        assert current['geometry']==previous['geometry'] and current['properties']==previous['properties'] and current.get('text')==previous.get('text')
    for update in updates:
        style=after[update['record_id']-1]['style']
        for key,value in update['style'].items():assert style[key]==pytest.approx(value,rel=1e-6,abs=1e-7)
        a=struct.unpack_from('<4d',old,sections[3][0]+update['record_id']*32)
        b=struct.unpack_from('<4d',blob,new_sections[3][0]+update['record_id']*32)
        assert b!=a
    assert after[1]==original[1] and after[3]==original[3]
    info=native_file_info(tmp_path,output['output'],file_sha(Path(output['output'])));assert info['active']==4
    assert source.read_bytes()==before


@pytest.mark.parametrize('updates',[
    [{'record_id':1,'style':{'pen_width':.3}}],
    [{'record_id':3,'style':{'font_zh':1}}],
    [{'record_id':1,'style':{'point_type':1}}],
    [{'record_id':1,'style':{'height':0}}],
    [{'record_id':1,'style':{'font_zh':1.5}}],
    [{'record_id':1,'style':{'font_en':256}}],
    [{'record_id':1,'style':{'width':1e100}}],
    [{'record_id':1,'style':{'color':True}}],
    [{'record_id':1,'style':{'angle':float('nan')}}],
    [{'record_id':1,'style':{}},{'record_id':1,'style':{'height':2}}],
])
def test_invalid_or_inapplicable_point_fields_are_rejected_before_native_launch(tmp_path,updates):
    bridge,source=case(tmp_path)
    with pytest.raises(BridgeError,match='POINT_STYLE'):
        prepare_native_operation(bridge,'point.style',str(source),file_sha(source),{'updates':updates},'bad')
    assert source.is_file()


def test_point_style_dirty_checkpoint_save_reopen_and_child_loss(tmp_path):
    from mapgis_agent.workspace import NativeWorkspaces
    bridge,source=case(tmp_path);before=source.read_bytes();workspace=NativeWorkspaces(bridge)
    try:
        opened=workspace.execute('open','text-buffer',{'source':str(source),'expected_sha256':file_sha(source)})
        dirty=workspace.execute('edit','text-buffer',{'operation':'point.style','parameters':{'updates':[{'record_id':1,'style':{'height':7.,'width':4.}}]}},0,'edit')
        assert dirty['dirty'] and dirty['native_pid']==opened['native_pid']
        workspace.buffers['text-buffer'].process.kill();workspace.buffers['text-buffer'].process.wait()
        recovered=workspace.execute('recover','text-buffer',{},1)
        assert recovered['dirty'] and recovered['native_pid']!=opened['native_pid']
        saved=workspace.execute('save','text-buffer',{},1,'save')
        assert not saved['dirty']
        actual=read_features(saved['output'],file_sha(Path(saved['output'])))['collection']['features'][0]
        assert actual['style']['height']==7 and actual['style']['width']==4
    finally:workspace.close_all()
    assert source.read_bytes()==before
