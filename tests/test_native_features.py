import json
from pathlib import Path

import pytest

from mapgis_agent.core import Bridge, BridgeError, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.deletion import prepare_deletion
from mapgis_agent.features import read_features, prepare_file_operation


@pytest.fixture
def native_points(tmp_path):
    bridge = Bridge(tmp_path / "project")
    collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": str(i), "geometry": {"type": "Point", "coordinates": [i * 10, 20]},
         "properties": {"ID": 9, "name": "甲" + str(i)}} for i in range(1, 4)]}
    template = {"name": "points.wt", "map_scale": 1000,
                "crs": {"kind": "local", "name": "explicit metre grid", "axis_order": "xy",
                        "unit": "metre", "reference_sha256": "0" * 64},
                "style": {"symbol_no": 1, "height": 1, "width": 1, "color": 2}}
    prepare_creation(bridge, collection, template, "create")
    source = Path(bridge.apply("create")["output"])
    return bridge, source, template


def test_native_features_are_real_geometry_with_physical_ids_and_paging(native_points):
    bridge, source, _ = native_points
    frozen = source.read_bytes()
    result = read_features(str(source), sha(frozen), offset=1, limit=1)
    assert result["count"] == 3 and result["next_offset"] == 2
    feature = result["collection"]["features"][0]
    assert feature["id"] == "2" and feature["properties"]["ID"] == 9
    assert feature["geometry"] == {"type": "Point", "coordinates": (20.0, 20.0)}
    assert feature["style"]["color"] == 2
    json.dumps(result, allow_nan=False)
    assert source.read_bytes() == frozen
    prepare_deletion(bridge, str(source), sha(frozen), [2], "delete")
    deleted = Path(bridge.apply("delete")["output"])
    result = read_features(str(deleted), sha(deleted.read_bytes()))
    assert [f["id"] for f in result["collection"]["features"]] == ["1", "3"]


def test_saved_native_file_runs_through_existing_vector_backend(native_points):
    bridge, source, template = native_points
    frozen = source.read_bytes()
    out_template = {**template, "name": "buffer.wp", "style": {"fill_color": 2}}
    prepared = prepare_file_operation(bridge, "geometry.buffer", str(source), sha(frozen),
                                      out_template, {"distance": 2}, "buffer", record_ids=[1, 3])
    output = Path(bridge.apply("buffer")["output"])
    result = bridge.inspect(str(output))
    assert result["type"] == "POLYGON" and result["count"] == 2
    assert [r["attributes"]["name"] for r in result["rows"]] == ["甲1", "甲3"]
    assert prepared["qa"]["native_source"]["sha256"] == sha(frozen)
    assert prepared["qa"]["native_source"]["physical_record_ids"] == [1, 3]
    assert source.read_bytes() == frozen
    assert prepare_file_operation(bridge, "geometry.buffer", str(source), sha(frozen),
                                  out_template, {"distance": 2}, "buffer", record_ids=[1, 3])["id"] == "buffer"


def test_native_feature_identity_and_limits_are_validated(native_points):
    _, source, _ = native_points
    digest = sha(source.read_bytes())
    with pytest.raises(BridgeError, match="STALE_SOURCE"):
        read_features(str(source), "f" * 64)
    for ids in ([], [True], [1, 1], [4], ["1"]):
        with pytest.raises(BridgeError, match="RECORD_IDS"):
            read_features(str(source), digest, record_ids=ids)
    for offset, limit in ((True, 1), (-1, 1), (0, False), (0, 0), (0, 101)):
        with pytest.raises(BridgeError, match="PAGING"):
            read_features(str(source), digest, offset=offset, limit=limit)


def test_annotation_readback_can_be_reused_without_null_symbol_parameters(tmp_path):
    bridge=Bridge(tmp_path)
    template={'name':'text.wt','map_scale':1000,'crs':{'kind':'local','name':'annotation grid','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}}
    collection={'type':'FeatureCollection','features':[{'type':'Feature','id':'1','geometry':{'type':'Point','coordinates':[10,20]},
        'properties':{'ID':1},'text':'T#-2#=d','style':{'height':5,'width':4,'font_zh':1,'font_en':0,'color':1}}]}
    prepare_creation(bridge,collection,template,'text');original=bridge.apply('text')
    snapshot=read_features(original['output'],original['candidate_sha256'])['collection']
    assert all(v is not None for v in snapshot['features'][0]['style'].values())
    prepare_creation(bridge,snapshot,{**template,'name':'copy.wt'},'copy')
    copied=bridge.apply('copy')
    assert read_features(copied['output'],copied['candidate_sha256'])['collection']==snapshot


def test_symbol_point_readback_and_clipping_keep_nondefault_pen_width(tmp_path):
    bridge=Bridge(tmp_path)
    template={'name':'symbol.wt','map_scale':1000,'crs':{'kind':'local','name':'symbol grid','axis_order':'xy','unit':'metre','reference_sha256':'0'*64}}
    collection={'type':'FeatureCollection','features':[{'type':'Feature','id':'a','geometry':{'type':'Point','coordinates':[10,20]},
        'properties':{'ID':7},'style':{'point_type':1,'symbol_no':2,'height':5,'width':4,'color':2,'pen_width':.37}}]}
    prepare_creation(bridge,collection,template,'symbol');original=bridge.apply('symbol')
    snapshot=read_features(original['output'],original['candidate_sha256'])['collection']
    assert snapshot['features'][0]['style']['pen_width']==pytest.approx(.37,abs=1e-7)
    prepared=prepare_file_operation(bridge,'overlay.clip',original['output'],original['candidate_sha256'],
        {**template,'name':'clip.wt'},{'mask':{'type':'Polygon','coordinates':[[[0,0],[30,0],[30,30],[0,30],[0,0]]]}},'clip-symbol')
    saved=bridge.apply('clip-symbol')
    assert read_features(saved['output'],saved['candidate_sha256'])['collection']==snapshot


def test_symbol_pen_width_matches_original_sdk_legend_and_survives_native_patch_undo(tmp_path):
    import struct
    from mapgis_agent.native_legend import prepare_native_legend
    from mapgis_agent.native_backend import prepare_native_operation
    from mapgis_agent.core import read_native,native_ids
    from test_native_raster import native_settings
    bridge=Bridge(tmp_path);native_settings(tmp_path)
    prepare_native_legend(bridge,[{'kind':'point','name':'笔宽样本','code':7,'style':{'pen_width':.37}}],
        {'name':'pen.cln','title':'笔宽','bounds':[0.,0.,100.,100.],'columns':1},'legend')
    original=Path(bridge.apply('legend')['output']).with_suffix('.wt')
    blob,reader,sections=read_native(original)
    rid=next(rid for rid,kind in zip(native_ids(reader),reader.point_params['point_type']) if kind==1)
    assert struct.unpack_from('<f',blob,sections[0][0]+rid*93+49)[0]==pytest.approx(.37,abs=1e-7)
    expected=read_features(str(original),sha(blob),record_ids=[rid])['collection']['features'][0]
    assert expected['style']['pen_width']==pytest.approx(.37,abs=1e-7)
    bridge.prepare(str(original),sha(blob),[rid],'pen-width',style={'pen_width':.8})
    patched=bridge.apply('pen-width')
    prepare_native_operation(bridge,'resave',patched['output'],patched['candidate_sha256'],{},'native-pen')
    saved=bridge.apply('native-pen')
    actual=read_features(saved['output'],saved['candidate_sha256'],record_ids=[rid])['collection']['features'][0]
    assert actual['style']['pen_width']==pytest.approx(.8,abs=1e-7)
    assert actual['geometry']==expected['geometry'] and actual['properties']==expected['properties']
    bridge.recover('pen-width',patched['output'],patched['candidate_sha256'],'undo-pen')
    assert Path(bridge.apply('undo-pen')['output']).read_bytes()==blob
