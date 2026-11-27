import csv
import json
from pathlib import Path

import ezdxf
import pyogrio
import pytest
from shapely.geometry import LineString, Point, Polygon, mapping

from mapgis_agent.core import Bridge, BridgeError, sha
from mapgis_agent.creation import prepare_modern_crs_creation
from mapgis_agent.interchange import DIMENSIONS, TARGETS, export_interchange_package, verify_interchange_package
from mapgis_agent.providers import MapGIS10ProviderStub, detect_providers, select_provider


def _binding(path):
    return {'file': str(path), 'expected_sha256': sha(path.read_bytes())}


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf8')
    return _binding(path)


@pytest.fixture
def inputs(tmp_path):
    bridge = Bridge(tmp_path)
    geometries = {
        'annotation.wt': Point(500010, 2800020),
        'fault.wl': LineString([(500000, 2800000), (500014, 2800030)]),
        'unit.wp': Polygon([(500001, 2800001), (500021, 2800001), (500021, 2800021), (500001, 2800021)],
                           [[(500008, 2800008), (500012, 2800008), (500012, 2800012), (500008, 2800012)]]),
    }
    sources = []
    for i, (name, geometry) in enumerate(geometries.items()):
        feature = {'type': 'Feature', 'id': 'native-a', 'geometry': mapping(geometry),
                   'properties': {'CODE': 'Q', '岩性': '灰岩', 'RGB': '255,255,115'}}
        if name.endswith('.wt'):
            feature |= {'text': '第四系地层', 'style': {'point_type': 0, 'height': 2, 'width': 2, 'color': 1}}
        template = {'name': name, 'map_scale': 50000, 'crs': {'definition': 'EPSG:4549', 'axis_order': 'xy'}}
        request = f'interchange-{i}'
        prepare_modern_crs_creation(bridge, {'type': 'FeatureCollection', 'features': [feature]}, template, request)
        sources.append(_binding(Path(bridge.apply(request)['output'])))

    ledgers = {
        'collar': [{'hole_id': 'ZK01', 'easting': 500010, 'northing': 2800020, 'elevation': 155.5}],
        'survey': [{'hole_id': 'ZK01', 'md': 0, 'inclination': 0, 'azimuth': 0},
                   {'hole_id': 'ZK01', 'md': 20, 'inclination': 10, 'azimuth': 40}],
        'lithology': [{'hole_id': 'ZK01', 'top': 0, 'base': 20, 'unit': '灰岩'}],
    }
    drillholes = {name: _json(tmp_path / f'{name}.json', rows) for name, rows in ledgers.items()}
    return sources, {'drillholes': drillholes, 'default_z': 100}


@pytest.mark.parametrize('target', TARGETS)
def test_export_and_parse_real_interchange_targets(tmp_path, inputs, target):
    sources, metadata = inputs
    output = tmp_path / 'deliverables' / target
    report = export_interchange_package(tmp_path, sources, target, output, metadata=metadata)
    assert report['target'] == target and report['feature_count'] == 3
    assert verify_interchange_package(output)['layers'] == report['layers']
    assert report['layers']['annotation']['bounds'] == [500010, 2800020, 500010, 2800020]
    manifest = json.loads((output / 'interchange-manifest.json').read_text(encoding='utf8'))
    assert set(manifest['retention_matrix']) == set(DIMENSIONS)
    assert manifest['retention_matrix']['topology']['status'] == 'PARTIAL'
    assert manifest['retention_matrix']['resources']['status'] == 'DROPPED'
    assert len(manifest['inputs']) == (9 if target == 'surpac' else 6)
    for entry in manifest['outputs']:
        raw = (output / entry['name']).read_bytes()
        assert entry['bytes'] == len(raw) and entry['sha256'] == sha(raw)
    assert all(_binding(Path(source['file'])) == source for source in sources)

    if target in ('arcgis_pro', 'qgis_gpkg'):
        unit = pyogrio.read_dataframe(output / 'layers.gpkg', layer='unit')
        polygon = unit.geometry.iloc[0]
        rings = sum(len(part.interiors) for part in polygon.geoms) if polygon.geom_type == 'MultiPolygon' else len(polygon.interiors)
        assert unit.crs.to_epsg() == 4549 and rings == 1
        assert unit['岩性'].tolist() == ['灰岩']
        if target == 'qgis_gpkg':
            assert len(list(output.glob('*.qml'))) == 3
    elif target == 'surpac':
        rows = list(csv.reader((output / 'mapgis.str').read_text().splitlines()))
        assert rows[2] == ['1', '2800020.0', '500010.0', '100', 'L1:R1']
        assert rows[-1][-1] == 'END'
        with (output / 'survey.csv').open(encoding='utf-8-sig', newline='') as stream:
            stations = list(csv.DictReader(stream))
        assert [float(row['dip']) for row in stations] == [-90, -80]
    elif target == 'coreldraw':
        dxf = ezdxf.readfile(output / 'mapgis.dxf')
        annotation, = dxf.modelspace().query('TEXT')
        assert annotation.dxf.layer == 'annotation' and annotation.dxf.text == '第四系地层'
        assert tuple(annotation.rgb) == (255, 255, 115)
        palette = json.loads((output / 'dz-t-0179-2025-colors.json').read_text(encoding='utf8'))
        assert palette['entries']['Q']['cmyk_percent'] == [0, 0, 55, 0]
        assert '第四系地层' in (output / 'preview.svg').read_text(encoding='utf8')
    else:
        layer = json.loads((output / 'unit.geojson').read_text(encoding='utf8'))
        assert layer['crs_uri'].endswith('/EPSG/0/4549')
        properties = layer['features'][0]['properties']
        assert properties['stratigraphicCode'] == 'Q' and properties['lithologyCode'] == '灰岩'
        assert properties['source']['sha256'] == sources[-1]['expected_sha256']


def test_verify_rejects_tampering_and_parses_refingerprinted_output(tmp_path, inputs):
    sources, _ = inputs
    output = tmp_path / 'work' / 'survives'
    export_interchange_package(tmp_path, sources, 'geosciml_lite', output)
    path = output / 'unit.geojson'
    document = json.loads(path.read_text(encoding='utf8'))
    document['features'][0]['geometry'] = mapping(Point(700000, 2800000))
    _json(path, document)
    with pytest.raises(BridgeError, match='INTERCHANGE_INTEGRITY'):
        verify_interchange_package(output)
    manifest_path = output / 'interchange-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    row = next(row for row in manifest['outputs'] if row['name'] == path.name)
    row.update(sha256=sha(path.read_bytes()), bytes=path.stat().st_size)
    _json(manifest_path, manifest)
    with pytest.raises(BridgeError, match='INTERCHANGE_READBACK: feature count or XY bounds'):
        verify_interchange_package(output)


def test_interchange_rejects_stale_sources_false_easting_and_unowned_output(tmp_path, inputs):
    sources, _ = inputs
    with pytest.raises(BridgeError, match='INTERCHANGE_WRITE_ROOT'):
        export_interchange_package(tmp_path, sources, 'arcgis_pro', 'originals/out')
    with pytest.raises(BridgeError, match='STALE_SOURCE'):
        export_interchange_package(tmp_path, [sources[0] | {'expected_sha256': 'a' * 64}], 'arcgis_pro', 'work/stale')
    with pytest.raises(BridgeError, match='INTERCHANGE_CRS'):
        export_interchange_package(tmp_path, sources, 'arcgis_pro', 'work/offset', crs='EPSG:4528')
    assert not any((tmp_path / 'work' / name).exists() for name in ('offset', 'stale'))
    assert not list((tmp_path / 'work').glob('.interchange-*'))


def test_provider_detection_and_conservative_selection(tmp_path):
    results = {row['provider_id']: row for row in detect_providers(tmp_path)}
    assert results['mapgis67']['tier'] == 'D' and not results['mapgis67']['available']
    assert len(results['mapgis67']['capabilities']['com']['exact_library_matches']) == 6
    assert not results['mapgis67']['capabilities']['private_ui']['configuration_enabled']
    headless = results['open_headless']
    assert headless['tier'] == 'D' and headless['available']
    assert not headless['capabilities']['mapgis_process_required']
    assert select_provider(tmp_path).provider_id == 'open_headless'
    assert not results['mapgis10']['available']
    assert 'reconcile_job' in results['mapgis10']['capabilities']['reserved_contract']['service']
    with pytest.raises(BridgeError, match='UNKNOWN_PROVIDER'):
        select_provider(tmp_path, 'invalid')
    with pytest.raises(BridgeError, match='PROVIDER_UNAVAILABLE'):
        select_provider(tmp_path, 'mapgis10')


def test_uninstalled_and_installed_10x_never_claim_executable_route(tmp_path, monkeypatch):
    from mapgis_agent.providers import mapgis10_stub

    provider = MapGIS10ProviderStub()
    monkeypatch.setattr(mapgis10_stub, '_installed_products', lambda: ([], True))
    assert provider.probe(tmp_path)['status'] == 'NOT_INSTALLED_ON_HOST'
    monkeypatch.setattr(mapgis10_stub, '_installed_products', lambda: ([{'name': 'MapGIS 10.6', 'version': '10.6'}], True))
    state = provider.probe(tmp_path)
    assert state['status'] == 'INSTALLED_EXTENSION_REQUIRED' and state['installed'] and not state['available']
    assert not provider.supports_operation('native.l2_probe')
