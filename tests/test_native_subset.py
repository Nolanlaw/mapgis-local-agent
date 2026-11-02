"""Real native subset must retain duplicate attributes, holes, style and text."""
import json,struct
from pathlib import Path
import pytest
from shapely.geometry import shape,mapping
from shapely.affinity import translate
from mapgis_agent.core import Bridge,BridgeError,read_native,native_ids
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_com import prepare_native_subset,_check_subset,_restore_primary_attributes,_table
from mapgis_agent.raster import file_sha
from test_native_raster import native_settings

def frame():return {'kind':'local','name':'subset fixture','axis_order':'xy','unit':'metre','reference_sha256':'a'*64}

@pytest.mark.parametrize('kind',['wt','wl','wp'])
def test_native_subset_preserves_physical_selection_holes_repeated_attributes_and_native_reopen(tmp_path,kind):
    b=Bridge(tmp_path);native_settings(tmp_path)
    shapes={'wt':{'type':'Point','coordinates':[10,20]},'wl':{'type':'LineString','coordinates':[[0,0],[4,2],[8,0]]},
        'wp':{'type':'Polygon','coordinates':[[[0,0],[20,0],[20,20],[0,20],[0,0]],[[5,5],[5,10],[10,10],[10,5],[5,5]]]}}
    features=[]
    for i in range(3):
        geometry=mapping(translate(shape(shapes[kind]),xoff=(20 if kind=='wp' else 100)*i))
        feature={'type':'Feature','id':str(i+1),'geometry':geometry,'properties':{'ID':9,'岩性':'第'+str(i+1)+'区'}}
        if kind=='wt':feature.update(text='地层'+str(i+1),style={'color':3,'height':2,'width':1.6,'angle':30})
        features.append(feature)
    prepare_creation(b,{'type':'FeatureCollection','features':features},{'name':'source.'+kind,'crs':frame(),'map_scale':1000},'source')
    source=Path(b.apply('source')['output']);before=bytearray(source.read_bytes())
    # Nonzero vertical/display tail fields in the native 169-byte MAP_PARA.
    struct.pack_into('<d',before,91+141,10000.);before[91+149]=2
    struct.pack_into('<d',before,91+161,1.);source.write_bytes(before);before=bytes(before)
    selected=[2,1] if kind=='wp' else [3,1]
    receipt=prepare_native_subset(b,str(source),file_sha(source),selected,{'name':'selected.'+kind},'subset')
    target=Path(b.apply('subset')['output']);_,reader,_=read_native(target)
    assert receipt['verification']['primary_attribute_records_byte_identical']
    assert list(reader.data['ID'])==[9,9] and list(reader.data['岩性'])==['第'+str(i)+'区' for i in selected]
    if kind=='wp':
        assert all(g.area==375 for g in reader.geom)
        from mapgis_agent.topology import _wp_arc_records
        assert any(set(arc['owner_region_ids'])=={1,2} for arc in _wp_arc_records(target)[3].values())
    assert receipt['native_execution']['pid']!=receipt['native_reopen']['pid']
    assert source.read_bytes()==before and len(native_ids(reader))==2
    assert prepare_native_subset(b,str(source),file_sha(source),selected,{'name':'selected.'+kind},'subset')['state']=='APPLIED'

def test_subset_rejects_invalid_selection_and_stale_input_before_launch(tmp_path):
    b=Bridge(tmp_path)
    prepare_creation(b,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':{'type':'Point','coordinates':[0,0]},'properties':{'ID':1}}]},
        {'name':'source.wt','crs':frame(),'map_scale':1000},'source')
    source=Path(b.apply('source')['output'])
    with pytest.raises(BridgeError,match='STALE_SOURCE'):prepare_native_subset(b,str(source),'0'*64,[1],{'name':'new.wt'},'stale')
    for i,ids in enumerate([[],[True],[2],[1,1]]):
        with pytest.raises(BridgeError,match='SUBSET_RECORD_IDS'):prepare_native_subset(b,str(source),file_sha(source),ids,{'name':'new.wt'},'invalid-'+str(i))
    assert not (tmp_path/'work/artifacts').is_dir()

def test_sdk_bad_attribute_active_flag_is_repaired_before_strict_readback(tmp_path):
    b=Bridge(tmp_path)
    prepare_creation(b,{'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':{'type':'Point','coordinates':[2,3]},'properties':{'ID':7,'地层':'T1'}}]},
        {'name':'source.wt','crs':frame(),'map_scale':1000},'source')
    source=Path(b.apply('source')['output']);blob,reader,sections=read_native(source)
    _,_,data,_,width=_table(blob,reader,sections);damaged=bytearray(blob);damaged[data+width]=0
    candidate=tmp_path/'sdk-output.wt';candidate.write_bytes(damaged)
    with pytest.raises(BridgeError,match='INCONSISTENT_ACTIVE_FLAGS'):read_native(candidate)
    mapping=[{'source_record_id':1,'output_record_id':1}]
    _restore_primary_attributes(source,candidate,mapping)
    assert _check_subset(source,candidate,mapping)['primary_attribute_records_byte_identical']
