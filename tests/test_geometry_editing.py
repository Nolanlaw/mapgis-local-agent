from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon, box, mapping

from mapgis_agent.core import Bridge, BridgeError, read_native
from mapgis_agent.vector import prepare_vector_operation


LOCAL = {'kind': 'local', 'name': 'analytic editing grid', 'axis_order': 'xy',
         'unit': 'metre', 'reference_sha256': '0' * 64}


def features(*geometries, differing=False):
    return {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'id': str(i), 'geometry': mapping(g),
         'properties': {'ID': 7, '地层': 'T2d' if not differing or i == 1 else 'J1'}}
        for i, g in enumerate(geometries, 1)]}


def run_edit(tmp_path, operation, source, parameters, suffix='wl'):
    bridge = Bridge(tmp_path)
    result = prepare_vector_operation(bridge, operation, source,
        {'name': 'edited.' + suffix, 'crs': LOCAL, 'map_scale': 1000}, parameters, 'edit')
    output = Path(bridge.apply('edit')['output'])
    _, reader, _ = read_native(output)
    return result, reader, output


def test_vertex_replacement_keeps_other_geometry_and_metadata(tmp_path):
    source = features(LineString([(0, 0), (5, 0), (10, 0)]), LineString([(20, 0), (30, 0)]))
    result, reader, _ = run_edit(tmp_path, 'geometry.replace', source,
        {'updates': [{'feature_id': '1', 'geometry': mapping(LineString([(0, 0), (5, 2), (7, 2), (10, 0)]))}]})
    assert list(reader.geom[0].coords) == [(0, 0), (5, 2), (7, 2), (10, 0)]
    assert list(reader.geom[1].coords) == [(20, 0), (30, 0)]
    assert reader.data['地层'].tolist() == ['T2d', 'T2d']
    assert result['qa']['edited_feature_ids'] == ['1']
    assert source['features'][0]['geometry']['coordinates'][1] == (5.0, 0.0)


def test_split_line_at_exact_point_preserves_length_and_lineage(tmp_path):
    result, reader, _ = run_edit(tmp_path, 'line.split', features(LineString([(0, 0), (10, 0)])),
        {'splitter': mapping(Point(3, 0))})
    assert sorted(g.length for g in reader.geom) == pytest.approx([3, 7])
    assert reader.data['地层'].tolist() == ['T2d', 'T2d']
    assert [m['feature_id'] for m in result['feature_mapping']] == ['1:split-1', '1:split-2']


def test_split_polygon_keeps_hole_and_rebuilds_shared_native_arcs(tmp_path):
    source = features(Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                              [[(1, 1), (2, 1), (2, 2), (1, 2)]]))
    _, reader, output = run_edit(tmp_path, 'polygon.split', source,
        {'splitter': mapping(LineString([(5, -1), (5, 11)]))}, 'wp')
    assert sorted(g.area for g in reader.geom) == pytest.approx([49, 50])
    from shapely import get_parts
    assert sum(len(part.interiors) for g in reader.geom for part in get_parts(g)) == 1
    from mapgis_agent.topology import validate_wp_topology
    assert validate_wp_topology(output)['shared_arcs'] == 1


def test_merge_lines_requires_explicit_attribute_resolution_and_contiguous_direction(tmp_path):
    source = features(LineString([(0, 0), (3, 0)]), LineString([(3, 0), (10, 0)]), differing=True)
    bridge = Bridge(tmp_path)
    template = {'name': 'merged.wl', 'crs': LOCAL, 'map_scale': 1000}
    with pytest.raises(BridgeError, match='MERGE_METADATA_CONFLICT'):
        prepare_vector_operation(bridge, 'line.merge', source, template, {}, 'conflict')
    result, reader, _ = run_edit(tmp_path, 'line.merge', source, {'attribute_source_id': '2'})
    assert list(reader.geom[0].coords) == [(0, 0), (3, 0), (10, 0)]
    assert reader.data['地层'].tolist() == ['J1']
    assert result['qa']['merged_source_ids'] == ['1', '2']


@pytest.mark.parametrize(('operation', 'parameters', 'coordinates'), [
    ('line.reverse', {}, [(10, 0), (0, 0)]),
    ('line.substring', {'start': 2, 'end': 7}, [(2, 0), (7, 0)]),
    ('line.offset', {'distance': -2}, [(0, -2), (10, -2)]),
    ('geometry.densify', {'max_segment_length': 3}, [(0, 0), (2.5, 0), (5, 0), (7.5, 0), (10, 0)]),
])
def test_linear_edits_have_analytic_coordinates_after_native_encoding(tmp_path, operation, parameters, coordinates):
    _, reader, _ = run_edit(tmp_path, operation, features(LineString([(0, 0), (10, 0)])), parameters)
    assert list(reader.geom[0].coords) == coordinates
    assert reader.data['ID'].tolist() == [7]


def test_replace_rejects_unknown_identity_and_polygon_self_intersection(tmp_path):
    source = features(box(0, 0, 10, 10))
    bridge = Bridge(tmp_path)
    template = {'name': 'bad.wp', 'crs': LOCAL, 'map_scale': 1000}
    with pytest.raises(BridgeError, match='EDIT_UNKNOWN_FEATURE'):
        prepare_vector_operation(bridge, 'geometry.replace', source, template,
            {'updates': [{'feature_id': 'absent', 'geometry': mapping(box(0, 0, 1, 1))}]}, 'unknown')
    with pytest.raises(BridgeError, match='EDIT_INVALID_GEOMETRY'):
        prepare_vector_operation(bridge, 'geometry.replace', source, template,
            {'updates': [{'feature_id': '1', 'geometry': mapping(Polygon([(0, 0), (10, 10), (0, 10), (10, 0)]))}]}, 'invalid')


def test_no_implicit_snap_or_geographic_distance_and_disconnected_merge(tmp_path):
    bridge = Bridge(tmp_path)
    template = {'name': 'bad.wl', 'crs': LOCAL, 'map_scale': 1000}
    with pytest.raises(BridgeError, match='SPLIT_DID_NOT_CUT'):
        prepare_vector_operation(bridge, 'line.split', features(LineString([(0, 0), (10, 0)])), template,
            {'splitter': mapping(Point(3, .001))}, 'off-line')
    with pytest.raises(BridgeError, match='MERGE_NOT_CONTIGUOUS'):
        prepare_vector_operation(bridge, 'line.merge', features(LineString([(0, 0), (1, 0)]), LineString([(2, 0), (3, 0)])),
            template, {}, 'disconnected')
    with pytest.raises(BridgeError, match='PROJECTED_UNITS_REQUIRED'):
        prepare_vector_operation(bridge, 'line.offset', features(LineString([(110, 30), (111, 30)])),
            {**template, 'crs': {'definition': 'EPSG:4326', 'axis_order': 'xy'}}, {'distance': 2}, 'degrees')
