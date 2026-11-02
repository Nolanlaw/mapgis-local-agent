"""Native text edits retain formatted GBK text and independent point identity."""
from pathlib import Path
import struct
import pytest

from mapgis_agent.core import BridgeError,read_native
from mapgis_agent.features import read_features
from mapgis_agent.native_backend import prepare_native_operation,native_file_info
from mapgis_agent.raster import file_sha
from test_native_point_style import case


@pytest.mark.parametrize('projected',[False,True])
def test_annotation_growth_and_shrink_preserve_graphics_coordinates_and_other_records(tmp_path,projected):
    bridge,source=case(tmp_path,projected);before=source.read_bytes();original,_,sections=read_native(source)
    texts={1:'中文注记T#-1#=n 123',2:'A'}
    receipt=prepare_native_operation(bridge,'point.text',str(source),file_sha(source),
        {'updates':[{'record_id':identity,'text':text} for identity,text in texts.items()]},'text')
    output=bridge.apply('text');blob,_,new_sections=read_native(Path(output['output']))
    previous=read_features(str(source),file_sha(source))['collection']['features']
    actual=read_features(output['output'],file_sha(Path(output['output'])))['collection']['features']
    for a,b in zip(previous,actual):
        assert a['geometry']==b['geometry'] and a['properties']==b['properties'] and a['style']==b['style']
        if int(b['id']) in texts:assert b['text']==texts[int(b['id'])]
        else:assert b==a
    for identity,text in texts.items():
        old=sections[0][0]+identity*93;new=new_sections[0][0]+identity*93
        assert original[old+7:old+93]==blob[new+7:new+93]
        assert struct.unpack_from('<h',blob,new+1)[0]==len(text.encode('gbk'))
    assert receipt['native_execution']['pid']!=receipt['native_reopen']['pid']
    assert native_file_info(tmp_path,output['output'],file_sha(Path(output['output'])))['active']==4
    assert source.read_bytes()==before


@pytest.mark.parametrize('updates',[
    [{'record_id':3,'text':'symbol is not annotation'}],
    [{'record_id':1,'text':''}],
    [{'record_id':1,'text':'emoji😀'}],
    [{'record_id':1,'text':'bad\x00body'}],
    [{'record_id':1,'text':'x'*32768}],
    [{'record_id':1,'text':False}],
    [{'record_id':True,'text':'body'}],
    [{'record_id':1,'text':'one'},{'record_id':1,'text':'two'}],
    [{'record_id':999,'text':'body'}],
])
def test_invalid_text_target_or_unrepresentable_body_fails_before_native_launch(tmp_path,updates):
    bridge,source=case(tmp_path)
    with pytest.raises(BridgeError,match='POINT_TEXT'):
        prepare_native_operation(bridge,'point.text',str(source),file_sha(source),{'updates':updates},'bad')


def test_native_formatted_text_dirty_save_and_completed_checkpoint_recovery(tmp_path):
    from mapgis_agent.workspace import NativeWorkspaces
    bridge,source=case(tmp_path);before=source.read_bytes();workspace=NativeWorkspaces(bridge)
    try:
        opened=workspace.execute('open','text-buffer',{'source':str(source),'expected_sha256':file_sha(source)})
        dirty=workspace.execute('edit','text-buffer',{'operation':'point.text','parameters':{'updates':[{'record_id':1,'text':'灰岩T#-2#=d'}]}},0,'edit')
        assert dirty['dirty'] and dirty['native_pid']==opened['native_pid']
        checkpoint=Path(dirty['checkpoint']).read_bytes()
        child=workspace.buffers['text-buffer'].process;child.kill();child.wait(timeout=5)
        recovered=workspace.execute('recover','text-buffer',{},1)
        assert recovered['dirty'] and recovered['native_pid']!=opened['native_pid'] and Path(recovered['checkpoint']).read_bytes()==checkpoint
        saved=workspace.execute('save','text-buffer',{},1,'save')
        assert read_features(saved['output'],file_sha(Path(saved['output'])))['collection']['features'][0]['text']=='灰岩T#-2#=d'
        assert not saved['dirty']
    finally:workspace.close_all()
    assert source.read_bytes()==before
