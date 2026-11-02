import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha
from mapgis_agent.scene_lines import LineOverlayInput,line_profile,prepare_lines,line_overlay_info

FRAME={'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}
GRID={'bounds':[500005.,3000005.,500645.,3000485.]}

def line(tmp_path,*,scale=1000,frame=FRAME,name='source.wl',coords=None):
    bridge=Bridge(tmp_path)
    geometry={'type':'LineString','coordinates':coords or [[500015.,3000015.],[500325.,3000245.],[500635.,3000475.]]}
    if name.endswith('.wt'):geometry={'type':'Point','coordinates':[500015.,3000015.]}
    features={'type':'FeatureCollection','features':[{'type':'Feature','id':'line-1','properties':{'sample':'test'},'geometry':geometry}]}
    prepare_creation(bridge,features,{'name':name,'crs':frame,'map_scale':scale},'source')
    return Path(bridge.apply('source')['output'])

def test_overlay_requires_explicit_native_metre_declaration_and_frozen_digest():
    for value in [{}, {'file':'a.wl','expected_sha256':'a'*64}, {'file':'a.wl','expected_sha256':'short','coordinate_frame':'native_metres'},
                  {'file':'a.wl','expected_sha256':'a'*64,'coordinate_frame':'native'}]:
        with pytest.raises(ValidationError):LineOverlayInput.model_validate(value)

def test_line_profile_preserves_physical_ids_world_vertices_and_native_bytes(tmp_path):
    source=line(tmp_path);before=(source.read_bytes(),source.stat().st_mtime_ns)
    profile=line_profile(source,FRAME,GRID,FRAME)
    assert profile['feature_count']==1 and profile['physical_record_ids']==[1] and profile['record_upper_bound']==2
    assert profile['vertices']==3 and profile['native_bounds']==[500015.,3000015.,500635.,3000475.]
    stage=tmp_path/'stage';(stage/'native').mkdir(parents=True)
    spec=LineOverlayInput(file=str(source),expected_sha256=file_sha(source),coordinate_frame='native_metres',source_crs=FRAME)
    assert prepare_lines(stage,spec,FRAME,GRID)==profile
    assert (stage/'native/overlay.wl').read_bytes()==before[0]
    assert (source.read_bytes(),source.stat().st_mtime_ns)==before
    assert line_overlay_info(stage/'native',{'overlay.wl':file_sha(stage/'native/overlay.wl')},FRAME,GRID,FRAME)==profile

@pytest.mark.parametrize('change,message',[
    ({'scale':500},'NATIVE_METRE_STORAGE'),
    ({'name':'source.wt'},'WL_REQUIRED'),
    ({'frame':{'definition':'+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}},'CRS_MISMATCH'),
])
def test_line_overlay_rejects_ambiguous_units_wrong_type_frame_and_outside(tmp_path,change,message):
    source=line(tmp_path,**change)
    with pytest.raises(BridgeError,match=message):line_profile(source,FRAME,GRID)

def test_source_change_refused_before_copy_and_bound_member_change_refused(tmp_path):
    source=line(tmp_path);stage=tmp_path/'stage';(stage/'native').mkdir(parents=True)
    spec=LineOverlayInput(file=str(source),expected_sha256='a'*64,coordinate_frame='native_metres',source_crs=FRAME)
    with pytest.raises(BridgeError,match='STALE_SOURCE'):prepare_lines(stage,spec,FRAME,GRID)
    assert not (stage/'native/overlay.wl').exists()
    spec=spec.model_copy(update={'expected_sha256':file_sha(source)})
    prepare_lines(stage,spec,FRAME,GRID)
    with pytest.raises(BridgeError,match='LINE_MEMBER_CHANGED'):line_overlay_info(stage/'native',{'overlay.wl':'0'*64},FRAME,GRID)

def test_explicit_crs_assignment_is_checked_and_never_claims_native_header_evidence(tmp_path):
    source=line(tmp_path)
    different={'definition':'EPSG:4549','axis_order':'xy'}
    with pytest.raises(BridgeError,match='CRS_MISMATCH'):line_profile(source,different,GRID)
    with pytest.raises(BridgeError,match='CRS_MISMATCH'):line_profile(source,FRAME,GRID,{'definition':'+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'})
    assert line_profile(source,different,GRID,different)['frame_evidence'].startswith('Explicit source CRS assignment')
    assert line_profile(source,FRAME,GRID)['frame_evidence']=='Legacy native header'

def test_outside_line_remains_refused_with_an_explicit_crs_assignment(tmp_path):
    source=line(tmp_path,coords=[[500000.,3000015.],[500325.,3000245.]])
    with pytest.raises(BridgeError,match='OUTSIDE_TERRAIN'):line_profile(source,FRAME,GRID,FRAME)

def test_local_overlay_needs_matching_declared_local_storage(tmp_path):
    local={'kind':'local','name':'test scene','axis_order':'xy','unit':'metre','reference_sha256':'d'*64}
    source=line(tmp_path,frame=local)
    assert line_profile(source,local,GRID)['frame']==local
    with pytest.raises(BridgeError,match='CRS_MISMATCH'):line_profile(source,FRAME,GRID)
    assert line_overlay_info(tmp_path,{},local,GRID) is None
