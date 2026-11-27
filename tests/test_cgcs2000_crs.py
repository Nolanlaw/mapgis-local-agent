import json
from pathlib import Path
import struct

from pyproj import CRS
import pytest
from shapely.geometry import LineString, Point, Polygon, mapping

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.creation import prepare_creation, prepare_modern_crs_creation
from mapgis_agent.crs_contract import resolve_effective_crs, sidecar_path
from mapgis_agent.drc import run_drc_audit


def make_layer(bridge, name, geom, definition, request):
    features = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'id': 'section-a',
                'properties': {'CODE': 'Q'}, 'geometry': mapping(geom)}]}
    template = {'name': name, 'crs': {'definition': definition, 'axis_order': 'xy'}, 'map_scale': 50000}
    prepared = prepare_modern_crs_creation(bridge, features, template, request)
    assert prepared['state'] == 'PREPARED'
    assert prepare_modern_crs_creation(bridge, features, template, request)['members'] == prepared['members']
    assert {entry['name'] for entry in prepared['members']} == {name, name+'.crs.json'}
    assert not Path(prepared['output']).exists()
    path = Path(bridge.apply(request)['output'])
    assert bridge.apply(request)['reconciled_existing_output']
    return path


@pytest.mark.parametrize(('epsg', 'name', 'geometry', 'offset', 'zone'), [
    (4490, 'collar.wt', Point(119.96, 26.14), None, None),
    (4528, 'fault.wl', LineString([(40500123.25, 2800001.5), (40501123.25, 2800301.5)]), 40500000, 40),
    (4549, 'unit.wp', Polygon([(500010, 2800010), (500030, 2800010), (500030, 2800030),
                                (500010, 2800030)]), 500000, 40),
    (4491, 'six-degree.wt', Point(13500231, 2800010), 13500000, 13),
])
def test_modern_crs_native_geometry_and_sidecar(tmp_path, epsg, name, geometry, offset, zone):
    bridge = Bridge(tmp_path)
    path = make_layer(bridge, name, geometry, f'EPSG:{epsg}', 'modern')
    blob, reader, _ = read_native(path)
    assert not reader.crs and blob[91:93] == blob[109:111] == b'\0\0'
    assert struct.unpack_from('<d', blob, 143)[0] == 0
    assert reader.geom[0].equals_exact(geometry, 1e-8, normalize=True)
    document = json.loads(sidecar_path(path).read_text(encoding='utf-8'))
    assert document['file_sha256'] == sha(blob)
    assert document['declared_epsg'] == epsg
    assert document['false_easting_m'] == offset and document['zone'] == zone
    assert document['ellipsoid']['inverse_flattening'] == pytest.approx(298.257222101)
    assert resolve_effective_crs(path, reader).to_epsg() == epsg
    assert CRS.from_wkt(bridge.inspect(str(path))['crs']).to_epsg() == epsg


def test_strict_crs_and_drc_with_hash_bound_zone_offsets(tmp_path):
    bridge = Bridge(tmp_path)
    prefixed = make_layer(bridge, 'prefixed.wt', Point(40503000, 2840000), 'EPSG:4528', 'prefixed')
    plain = make_layer(bridge, 'plain.wt', Point(503000, 2840000), 'EPSG:4549', 'plain')
    with pytest.raises(BridgeError, match='AUDIT_FALSE_EASTING'):
        run_drc_audit([str(prefixed), str(plain)], enforce_frames=True)
    run_drc_audit([str(prefixed)], reference_crs='EPSG:4528', enforce_frames=True)
    _, reader, _ = read_native(prefixed)
    prefixed.write_bytes(b'X' + prefixed.read_bytes()[1:])
    with pytest.raises(BridgeError, match='CRS_SIDECAR_STALE'):
        resolve_effective_crs(prefixed, reader)
    with pytest.raises(BridgeError, match='CRS_SIDECAR_STALE'):
        run_drc_audit([str(prefixed)], enforce_frames=True)


def test_custom_tmerc_and_unmodified_strict_default(tmp_path):
    bridge = Bridge(tmp_path)
    custom = '+proj=tmerc +lat_0=0 +lon_0=120 +k=1 +x_0=740025 +y_0=0 +ellps=GRS80 +units=m +no_defs'
    path = make_layer(bridge, 'custom.wt', Point(740126, 2800101), custom, 'custom')
    assert json.loads(sidecar_path(path).read_text(encoding='utf-8'))['false_easting_m'] == 740025
    features = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'id': '1',
                'properties': {}, 'geometry': mapping(Point(40500005, 2800000))}]}
    with pytest.raises(BridgeError, match='UNREPRESENTABLE_NATIVE_CRS'):
        prepare_creation(bridge, features, {'name': 'strict.wt', 'map_scale': 50000,
            'crs': {'definition': 'EPSG:4528', 'axis_order': 'xy'}}, 'strict')
