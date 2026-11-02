from pathlib import Path
from contextlib import nullcontext
import json
import pytest
from pydantic import ValidationError
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha
from mapgis_agent.scene_annotations import AnnotationOverlayInput,AnnotationFont,annotation_profile,prepare_annotations,annotation_info,annotation_settings

FRAME={'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}
GRID={'bounds':[500005.,3000005.,500645.,3000485.]}

def fixture(tmp_path,*,text='山峰A',scale=1000,kind=0,xy=(500225.,3000255.)):
    bridge=Bridge(tmp_path)
    feature={'type':'Feature','id':'peak','geometry':{'type':'Point','coordinates':list(xy)},'properties':{'tag':'peak'},
        'text':text,'style':{'point_type':kind,'height':6.,'width':6.,'color':0}}
    if kind:feature.pop('text')
    prepare_creation(bridge,{'type':'FeatureCollection','features':[feature]},
        {'name':'source.wt','crs':FRAME,'map_scale':scale},'source')
    return Path(bridge.apply('source')['output'])

def test_native_annotation_retains_text_physical_id_bytes_and_explicit_frame(tmp_path):
    source=fixture(tmp_path);before=(source.read_bytes(),source.stat().st_mtime_ns)
    spec=AnnotationOverlayInput(file=str(source),expected_sha256=file_sha(source),coordinate_frame='native_metres',source_crs=FRAME)
    profile=annotation_profile(source,FRAME,GRID,spec)
    assert profile['records']==[{'id':1,'text':'山峰A','anchor':[500225.,3000255.]}]
    assert profile['record_upper_bound']==2 and profile['feature_count']==1
    stage=tmp_path/'stage';(stage/'native').mkdir(parents=True)
    assert prepare_annotations(stage,spec,FRAME,GRID)==profile
    assert (stage/'native/notes.wt').read_bytes()==before[0]
    assert annotation_info(stage/'native',{'notes.wt':file_sha(source)},FRAME,GRID,profile)==profile
    assert (source.read_bytes(),source.stat().st_mtime_ns)==before
    assert annotation_settings(profile).splitlines()[-1]=='1'

@pytest.mark.parametrize('text',['','  ','a\nb','a\x00b','镕','𠮷','a'*32768],ids=['empty','spaces','newline','nul','gbk-extension','non-bmp','length-overflow'])
def test_unrenderable_text_is_rejected_before_native_loader(tmp_path,text):
    # The source writer has its own NUL/size bounds; they also satisfy this gate.
    with pytest.raises((BridgeError,ValueError)):
        source=fixture(tmp_path,text=text)
        annotation_profile(source,FRAME,GRID,AnnotationOverlayInput(file=str(source),expected_sha256=file_sha(source),coordinate_frame='native_metres',source_crs=FRAME))

@pytest.mark.parametrize('change,error',[({'scale':500},'NATIVE_METRE_STORAGE'),({'kind':1},'ORDINARY_TEXT_REQUIRED'),({'xy':(500000.,3000255.)},'OUTSIDE_TERRAIN')])
def test_invalid_native_storage_type_and_extent_are_rejected(tmp_path,change,error):
    source=fixture(tmp_path,**change)
    spec=AnnotationOverlayInput(file=str(source),expected_sha256=file_sha(source),coordinate_frame='native_metres',source_crs=FRAME)
    with pytest.raises(BridgeError,match=error):annotation_profile(source,FRAME,GRID,spec)

def test_legacy_header_does_not_silently_relabel_a_different_crs(tmp_path):
    source=fixture(tmp_path)
    different={'definition':'EPSG:4549','axis_order':'xy'}
    spec=AnnotationOverlayInput(file=str(source),expected_sha256=file_sha(source),coordinate_frame='native_metres')
    with pytest.raises(BridgeError,match='CRS_MISMATCH'):annotation_profile(source,different,GRID,spec)
    assert annotation_profile(source,FRAME,GRID,spec)['frame_evidence']=='Legacy native header'
    assert annotation_info(tmp_path,{},FRAME,GRID,None) is None

@pytest.mark.parametrize('value',[{'points':0},{'points':31},{'points':True},{'face':'missing\nfont'},{'face':'a'*64},{'bold':1}])
def test_font_fields_are_typed_and_fit_original_67_byte_layout(value):
    with pytest.raises(ValidationError):AnnotationFont.model_validate(value)

def test_changed_annotation_source_refused_before_copy(tmp_path):
    source=fixture(tmp_path);stage=tmp_path/'stage';(stage/'native').mkdir(parents=True)
    spec=AnnotationOverlayInput(file=str(source),expected_sha256='a'*64,coordinate_frame='native_metres',source_crs=FRAME)
    with pytest.raises(BridgeError,match='STALE_SOURCE'):prepare_annotations(stage,spec,FRAME,GRID)
    assert not (stage/'native/notes.wt').exists()

@pytest.mark.parametrize('reason,timeout,expected',[
    ('scene annotation font unavailable; choose an installed GB2312 font',False,'SCENE_ANNOTATION_FONT_UNAVAILABLE'),
    ('scene annotation font missing glyph',False,'SCENE_ANNOTATION_FONT_MISSING_GLYPH'),
    ('scene annotation exceeds original glyph atlas; split the label',False,'SCENE_ANNOTATION_ATLAS_OVERFLOW'),
    ('unrelated original failure',False,'NATIVE_CHILD_FAILED'),
    ('scene annotation font missing glyph',True,'NATIVE_CHILD_FAILED'),
])
def test_specific_native_errors_reach_agent_without_hiding_other_failures(tmp_path,monkeypatch,reason,timeout,expected):
    from mapgis_agent import native_scene
    monkeypatch.setattr(native_scene,'_runtime',lambda root:(Path('worker.exe'),tmp_path,{}))
    class Alias: path=tmp_path
    monkeypatch.setattr(native_scene,'NativePathAlias',lambda folder:nullcontext(Alias()))
    def fail(*args,**kwargs):
        (tmp_path/'result.json').write_text(json.dumps({'returncode':1,'timed_out':timeout,'stderr':reason}),encoding='utf8')
        raise BridgeError('NATIVE_CHILD_FAILED: diagnostic retained')
    monkeypatch.setattr(native_scene,'_run_child',fail)
    with pytest.raises(BridgeError,match=expected):native_scene._invoke(tmp_path,tmp_path,'render','result.json')
