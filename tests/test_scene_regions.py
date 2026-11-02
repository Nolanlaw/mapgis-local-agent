from pathlib import Path
import pytest
from pydantic import ValidationError
from mapgis_agent.core import Bridge,BridgeError
from mapgis_agent.creation import prepare_creation
from mapgis_agent.raster import file_sha
from mapgis_agent.scene_regions import RegionOverlayInput,region_profile,prepare_regions,region_info,region_settings

FRAME={'definition':'+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +no_defs','axis_order':'xy'}
GRID={'bounds':[500005.,3000005.,500645.,3000485.],'rows':49,'columns':65}

def fixture(tmp_path,*,hole=True,scale=1000,outside=False):
    x=500000. if outside else 500080.
    outer=[[x,3000100.],[x+200,3000100.],[x+200,3000390.],[x,3000390.],[x,3000100.]]
    rings=[outer]
    if hole:rings.append([[x+50,3000200.],[x+150,3000200.],[x+150,3000300.],[x+50,3000300.],[x+50,3000200.]])
    bridge=Bridge(tmp_path);features={'type':'FeatureCollection','features':[{'type':'Feature','id':'zone','geometry':{'type':'Polygon','coordinates':rings},'properties':{'name':'zone'},'style':{'fill_color':4}}]}
    prepare_creation(bridge,features,{'name':'source.wp','crs':FRAME,'map_scale':scale},'source')
    return Path(bridge.apply('source')['output'])

def spec(source,**options):
    return RegionOverlayInput(file=str(source),expected_sha256=file_sha(source),coordinate_frame='native_metres',source_crs=FRAME,**options)

def test_region_mesh_preserves_hole_native_ids_and_source_bytes(tmp_path):
    source=fixture(tmp_path);before=(source.read_bytes(),source.stat().st_mtime_ns)
    profile,mesh=region_profile(source,FRAME,GRID,spec(source))
    assert profile['feature_count']==1 and profile['record_upper_bound']==2
    assert profile['records'][0]['id']==1 and profile['records'][0]['area']==48000.
    assert profile['records'][0]['rings']==2 and profile['mesh_vertices']>400
    assert mesh.startswith(b'MGSR0001\n1\n')
    stage=tmp_path/'stage';(stage/'native').mkdir(parents=True)
    assert prepare_regions(stage,spec(source),FRAME,GRID)==profile
    assert (stage/'native/regions.wp').read_bytes()==before[0] and (stage/'native/regions.mesh').read_bytes()==mesh
    assert region_info(stage/'native',{'regions.wp':file_sha(source),'regions.mesh':file_sha(stage/'native/regions.mesh')},FRAME,GRID,profile)==profile
    assert (source.read_bytes(),source.stat().st_mtime_ns)==before
    assert region_settings(profile).split()[0]=='1'

def test_boundary_sampling_and_dem_nodes_are_deterministic_and_keep_footprint(tmp_path):
    source=fixture(tmp_path,hole=False)
    first,mesh=region_profile(source,FRAME,GRID,spec(source))
    assert (first,mesh)==region_profile(source,FRAME,GRID,spec(source))
    assert first['boundary_step_m']==10. and first['records'][0]['area']==58000.
    assert first['records'][0]['mesh_vertices']>580
    assert first['mesh_vertices']<=100000

@pytest.mark.parametrize('change,error',[({'scale':500},'NATIVE_METRE_STORAGE'),({'outside':True},'OUTSIDE_TERRAIN')])
def test_region_wrong_native_units_and_extent_are_rejected(tmp_path,change,error):
    source=fixture(tmp_path,**change)
    with pytest.raises(BridgeError,match=error):region_profile(source,FRAME,GRID,spec(source))

def test_ambiguous_header_and_changed_mesh_refused(tmp_path):
    source=fixture(tmp_path);options=spec(source).model_copy(update={'source_crs':None})
    different={'definition':'EPSG:4549','axis_order':'xy'}
    with pytest.raises(BridgeError,match='CRS_MISMATCH'):region_profile(source,different,GRID,options)
    assert region_profile(source,FRAME,GRID,options)[0]['frame_evidence']=='Legacy native header'
    stage=tmp_path/'stage';(stage/'native').mkdir(parents=True);profile=prepare_regions(stage,spec(source),FRAME,GRID)
    members={'regions.wp':file_sha(source),'regions.mesh':file_sha(stage/'native/regions.mesh')}
    (stage/'native/regions.mesh').write_bytes(b'forged')
    members['regions.mesh']=file_sha(stage/'native/regions.mesh')
    with pytest.raises(BridgeError,match='MESH_BINDING'):region_info(stage/'native',members,FRAME,GRID,profile)
    assert region_info(tmp_path,{},FRAME,GRID,None) is None

@pytest.mark.parametrize('options',[{'opacity':-1.},{'opacity':1.1},{'visible':1},{'height_offset_m':float('nan')},{'max_view_distance_m':0.}])
def test_region_display_fields_are_typed(options):
    with pytest.raises(ValidationError):RegionOverlayInput(file='a.wp',expected_sha256='a'*64,coordinate_frame='native_metres',**options)
