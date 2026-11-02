"""Library curves are native parameters, checked against exact WT primitives."""
import struct
from pathlib import Path

import cv2
import numpy as np
import pytest

from mapgis_agent.core import BridgeError
from mapgis_agent.symbol_library import _primitive_payload, _decode_glyph, prepare_system_library
from test_symbol_library import library_case


CURVES = [
    {'type': 'arc', 'center': [.1, -.2], 'radius': .4, 'start_angle': 30., 'end_angle': 150.},
    {'type': 'circle_filled', 'center': [.1, -.2], 'radius': .4},
    {'type': 'circle_filled_outline', 'center': [.1, -.2], 'radius': .4},
    {'type': 'circle', 'center': [.1, -.2], 'radius': .4},
]


def test_library_curve_stream_uses_six_floats_and_one_three_pair_parameter_part():
    payload = _primitive_payload(CURVES)
    assert struct.unpack_from('<2h', payload) == (4, 24)
    assert [struct.unpack_from('<5h', payload, 4 + 10*i) for i in range(4)] == [
        (kind, 0, 1, 1, 1) for kind in (3, 4, 5, 6)]
    assert struct.unpack_from('<4h', payload, 44) == (3, 3, 3, 3)
    values = struct.unpack_from('<24f', payload, 52)
    assert values == pytest.approx([.1, -.2, .4, 30., 150., 0.] + [.1, -.2, .4, 0., 0., 0.]*3)
    decoded = _decode_glyph(payload)
    assert [d['type'] for d in decoded] == [d['type'] for d in CURVES]
    assert all(d['center'] == pytest.approx([.1, -.2]) and d['radius'] == pytest.approx(.4) for d in decoded)
    assert decoded[0]['start_angle'] == 30. and decoded[0]['end_angle'] == 150.
    # Unknown or structurally different old curves stay observable and opaque.
    unknown = bytearray(_primitive_payload([CURVES[0]]))
    struct.pack_into('<h', unknown, 4, 17)
    assert _decode_glyph(unknown)[0]['type'] == 'native_parameters'
    assert _decode_glyph(unknown)[0]['values'] == pytest.approx([.1, -.2, .4, 30., 150., 0.])


@pytest.mark.parametrize('primitive', [
    {'type': 'circle', 'center': [0, 0], 'radius': 0},
    {'type': 'circle', 'center': [0, 0], 'radius': 1e-60},
    {'type': 'circle', 'center': [0, True], 'radius': 1},
    {'type': 'circle', 'center': [0, float('nan')], 'radius': 1},
    {'type': 'circle', 'center': [0, 0], 'radius': 1, 'start_angle': 0},
    {'type': 'circle_filled', 'center': [0, 0], 'radius': 1, 'fill_color': 6},
    {'type': 'arc', 'center': [0, 0], 'radius': 1, 'start_angle': 90, 'end_angle': 0},
    {'type': 'arc', 'center': [0, 0], 'radius': 1, 'start_angle': 0, 'end_angle': 361},
    {'type': 'arc', 'center': [0, 0], 'radius': 1, 'start_angle': 179.99999999, 'end_angle': 180},
    {'type': 'arc', 'center': [0, 0], 'radius': 1, 'start_angle': 0},
])
def test_library_curve_rejects_ambiguous_or_unrepresentable_parameters(primitive):
    with pytest.raises(BridgeError, match='LIBRARY_'):
        _primitive_payload([primitive])


def test_pattern_circle_has_separate_fill_and_line_fields_and_no_native_center_clamping():
    primitive = {'type': 'circle_filled_outline', 'center': [.5, .5], 'radius': .2,
                 'fill_color': 6, 'color': 1, 'width_index': 7}
    payload = _primitive_payload([primitive], library_kind='pattern')
    assert struct.unpack_from('<5h', payload, 4) == (5, 6, 1, 7, 1)
    with pytest.raises(BridgeError, match='LIBRARY_'):
        _primitive_payload([{**primitive, 'center': [-.2, .5]}], library_kind='pattern')


def test_native_pattern_curves_keep_original_circle_tiles_and_draw_exact_arc_and_two_colors(tmp_path):
    from mapgis_agent.creation import prepare_creation
    from mapgis_agent.native_plot import prepare_native_plot
    from mapgis_agent.symbol_library import system_library_info
    from shapely.geometry import box, mapping

    bridge, source, info = library_case(tmp_path)
    original = system_library_info(str(source), kind='pattern', ids=[2, 3, 124])['records']
    definitions = []
    for record in original:
        primitives = []
        for primitive in record['primitives']:
            keys = {'type', 'color', 'width_index'} | ({'parts', 'fill_color'} if primitive['native_type'] < 3 else {'center', 'radius'})
            if primitive['native_type'] in (4, 5):
                keys.add('fill_color')
            if primitive['native_type'] == 3:
                keys.update({'start_angle', 'end_angle'})
            primitives.append({k: primitive[k] for k in keys})
        definitions.append(primitives)
    definitions += [[{'type': 'arc', 'center': [.5, .5], 'radius': .3, 'start_angle': 30., 'end_angle': 150., 'color': 1, 'width_index': 7}],
                    [{'type': 'circle_filled_outline', 'center': [.5, .5], 'radius': .3, 'fill_color': 6, 'color': 1, 'width_index': 7}]]
    prepared = prepare_system_library(bridge, str(source), info['manifest_sha256'], [
        {'kind': 'pattern', 'action': 'append', 'primitives': ps} for ps in definitions], 'patterns')
    output = bridge.apply('patterns')
    binding = {'folder': str(Path(output['output']).parent), 'expected_manifest_sha256': prepared['manifest_sha256']}

    def render(number, tag):
        prepare_creation(bridge, {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'id': '1',
            'geometry': mapping(box(0, 0, 20, 20)), 'properties': {'ID': 1},
            'style': {'fill_color': 9, 'pattern': number, 'pattern_color': 6, 'pattern_height': 10, 'pattern_width': 10}}]},
            {'name': tag+'.wp', 'map_scale': 1000,
             'crs': {'kind': 'local', 'name': 'curve tiles', 'axis_order': 'xy', 'unit': 'metre', 'reference_sha256': '0'*64}}, tag)
        saved = bridge.apply(tag)
        result = prepare_native_plot(bridge, [{'file': saved['output'], 'expected_sha256': saved['candidate_sha256']}],
            {'native_bounds': [0, 0, 20, 20], 'paper_mm': [100, 100], 'coordinate_frame': 'native'},
            tag+'-plot', system_library=binding)
        return cv2.imdecode(np.frombuffer(Path(result['render_check']['preview']).read_bytes(), np.uint8), 1), Path(result['stage'])/'candidate.PS'

    for reference, edit in zip((2, 3, 124), prepared['changes'][:3]):
        expected, _ = render(reference, 'old-'+str(reference))
        actual, _ = render(edit['id'], 'new-'+str(reference))
        assert np.array_equal(actual, expected)
    arc, postscript = render(prepared['changes'][3]['id'], 'arc')
    assert b'30.00 150.00 a s' in postscript.read_bytes()
    assert np.count_nonzero(np.any(arc < 245, axis=2)) > 200
    colored, _ = render(prepared['changes'][4]['id'], 'two-colors')
    colors, counts = np.unique(colored.reshape(-1, 3), axis=0, return_counts=True)
    assert np.count_nonzero(counts > 30) >= 3  # white background, fill, outline


def test_native_library_curves_equal_exact_wt_and_original_library_controls_after_save_reopen(tmp_path):
    from mapgis_agent.core import read_native
    from mapgis_agent.creation import prepare_creation
    from mapgis_agent.native_backend import prepare_native_operation
    from mapgis_agent.native_plot import prepare_native_plot
    from mapgis_agent.symbol_library import system_library_info

    bridge, source, info = library_case(tmp_path)
    curves = [{**c, 'center': [0, 0], 'width_index': 7, 'color': 1} for c in CURVES]
    # The installed single-primitive symbols34/35 are independent controls for
    # native fill-only / fill-and-outline rendering. Keep their exact floats.
    original = system_library_info(str(source), kind='symbol', ids=[34, 35])['records']
    for index, record in zip((1, 2), original):
        primitive = record['primitives'][0]
        curves[index] = {k: primitive[k] for k in ('type', 'center', 'radius', 'color', 'width_index')}
    composite = [{'type': 'circle_filled', 'center': [0, 0], 'radius': .4, 'color': 6},
                 {'type': 'circle', 'center': [0, 0], 'radius': .4, 'color': 1, 'width_index': 7}]
    prepared = prepare_system_library(bridge, str(source), info['manifest_sha256'], [
        {'kind': 'symbol', 'action': 'append', 'primitives': ps} for ps in [*[ [c] for c in curves], composite]], 'curves')
    saved = bridge.apply('curves')
    binding = {'folder': str(Path(saved['output']).parent), 'expected_manifest_sha256': prepared['manifest_sha256']}
    frame = {'kind': 'local', 'name': 'native curve control', 'axis_order': 'xy', 'unit': 'metre', 'reference_sha256': '0'*64}

    def create(styles, tag):
        prepare_creation(bridge, {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'id': str(i), 'geometry': {'type': 'Point', 'coordinates': [50, 50]},
             'properties': {'ID': i}, 'style': s} for i, s in enumerate(styles, 1)]},
            {'name': tag+'.wt', 'map_scale': 1000, 'crs': frame}, tag)
        made = bridge.apply(tag)
        prepare_native_operation(bridge, 'resave', made['output'], made['candidate_sha256'], {}, tag+'-native')
        result = bridge.apply(tag+'-native')
        _, reader, _ = read_native(Path(result['output']))
        assert len(reader.geom) == len(styles)
        return result

    def render(layer, tag):
        result = prepare_native_plot(bridge, [{'file': layer['output'], 'expected_sha256': layer['candidate_sha256']}],
            {'native_bounds': [0, 0, 100, 100], 'paper_mm': [100, 100], 'coordinate_frame': 'native'},
            tag, system_library=binding)
        return cv2.imdecode(np.frombuffer(Path(result['render_check']['preview']).read_bytes(), np.uint8), 1)

    common = {'radius_mm': 8, 'color': 1, 'pen_width': .3}
    controls = [
        [{'point_type': 3, 'start_angle': 30., 'end_angle': 150., **common}],
        [{'point_type': 1, 'symbol_no': 34, 'height': 20, 'width': 20, 'color': 1}],
        [{'point_type': 1, 'symbol_no': 35, 'height': 20, 'width': 20, 'color': 1}],
        [{'point_type': 2, 'filled': 0, 'border_color': 1, **common}],
        [{'point_type': 2, 'filled': 1, 'border_color': 6, **common, 'color': 6},
         {'point_type': 2, 'filled': 0, 'border_color': 1, **common}],
    ]
    for i, control in enumerate(controls):
        symbol = create([{'point_type': 1, 'symbol_no': prepared['changes'][i]['id'], 'height': 20, 'width': 20, 'color': 1}], f'symbol-{i}')
        actual = render(symbol, f'plot-symbol-{i}')
        expected = render(create(control, f'control-{i}'), f'plot-control-{i}')
        assert np.count_nonzero(np.any(expected < 245, axis=2)) > 30
        # Original native drawing of parameterized library curves must coincide
        # with original native WT circle/arc parameters, including fill/border.
        assert np.array_equal(actual, expected), (i, int(np.max(np.abs(actual.astype(int)-expected.astype(int)))))
