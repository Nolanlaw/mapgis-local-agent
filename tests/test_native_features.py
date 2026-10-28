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
