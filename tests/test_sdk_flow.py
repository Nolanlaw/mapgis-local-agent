from pathlib import Path

import pytest

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent import sdk_flow
from test_attribute_schema import sample


@pytest.fixture
def metadata(monkeypatch):
    # Snapshots of original contracts are deliberately limited to the methods
    # these validation tests need. Runtime acceptance reads the installed DLL.
    def describe(root, library, class_name=None, interface=None, limit=250):
        vt = lambda n: {'vt': n}
        methods = []
        if (class_name or interface) in ('D_Dot', 'ID_Dot'):
            for name, memid in [('x', 1), ('y', 2)]:
                methods += [{'name': name, 'member_id': memid, 'invoke_kind': 2,
                    'parameters': [], 'return': {'type': vt(5)}},
                    {'name': name, 'member_id': memid, 'invoke_kind': 4,
                    'parameters': [{'name': 'arg0', 'type': vt(5), 'flags': 1}], 'return': {'type': vt(24)}}]
        else:
            for name, memid in [('Load', 10015), ('Save', 10016)]:
                methods.append({'name': name, 'member_id': memid, 'invoke_kind': 1,
                    'parameters': [{'name': n, 'type': vt(8), 'flags': 49, 'default': ''}
                        for n in ('fName', 'szDSN', 'szLOG', 'szPSW')], 'return': {'type': vt(11)}})
            methods += [{'name': 'count', 'member_id': 10032, 'invoke_kind': 2,
                'parameters': [], 'return': {'type': vt(3)}}]
        return {'methods': methods, 'clsid': '{815B7C63-500E-4D86-BFFD-35677B9CEC53}', 'next_offset': None}
    monkeypatch.setattr(sdk_flow, 'sdk_describe', describe)


def flow():
    return {'inputs': [{'id': 'input', 'file': 'owned.wt', 'expected_sha256': 'a'*64}],
        'outputs': [{'id': 'output', 'name': 'edited.wt', 'source': 'input'}],
        'objects': [{'id': 'area', 'class': 'PntArea'}, {'id': 'dot', 'class': 'D_Dot'}],
        'steps': [{'id': 'loaded', 'target': 'area', 'member': 'Load', 'kind': 'method', 'arguments': [{'input': 'input'}]},
            {'id': 'coordinate', 'target': 'dot', 'member': 'x', 'kind': 'put', 'arguments': [{'value': 1.2345678901234567}]},
            {'id': 'checked', 'target': 'dot', 'member': 'x', 'kind': 'get', 'expect': 1.2345678901234567},
            {'id': 'saved', 'target': 'area', 'member': 'Save', 'kind': 'method', 'arguments': [{'output': 'output'}]}]}


def test_explicit_paths_and_native_assertions_not_literal_original_paths(metadata, tmp_path):
    planner = sdk_flow.Planner(tmp_path, flow())
    control = planner.control({'input': Path('C:/owned/input.wt')}, {'output': Path('C:/owned/edited.wt')})
    assert 'owned.wt' not in control
    assert 'expect\tloaded\t11\t1' in control and 'expect\tsaved\t11\t1' in control
    assert '1.2345678901234567' in control and '\tget\t' not in control
    assert str(Path('C:/owned/input.wt')).encode('utf-16le').hex() in control


@pytest.mark.parametrize('change,error', [
    ('global_class', 'CLASS_NOT_ALLOWED'), ('dialog', 'WORKSPACE_MEMBER_NOT_ALLOWED'),
    ('raw_path', 'EXPLICIT_FILE_BINDING'), ('database', 'LOCAL_FILES_ONLY'),
    ('save_source', 'EXPLICIT_FILE_BINDING'), ('duplicate', 'DUPLICATE_SYMBOL'),
    ('bad_number', 'SCALAR_TYPE'), ('nan', 'SCALAR_TYPE'),
    ('self_reference', 'REFERENCE_TYPE'), ('unused', 'UNUSED_INPUT'),
    ('output_escape', 'OUTPUT_FILENAME'), ('wrong_kind', 'WORKSPACE_FILE_TYPE')])
def test_reject_unsafe_or_ambiguous_calls_before_host_execution(metadata, tmp_path, change, error):
    p = flow()
    if change == 'global_class': p['objects'][0]['class'] = 'GisEnv'
    elif change == 'dialog': p['steps'][0]['member'] = 'SaveAs'
    elif change == 'raw_path': p['steps'][0]['arguments'] = [{'value': 'C:/original.wt'}]
    elif change == 'database': p['steps'][0]['arguments'] += [{'value': 'remote'}]
    elif change == 'save_source': p['steps'][-1]['arguments'] = [{'input': 'input'}]
    elif change == 'duplicate': p['steps'][1]['id'] = 'loaded'
    elif change == 'bad_number': p['steps'][1]['arguments'] = [{'value': True}]
    elif change == 'nan': p['steps'][1]['arguments'] = [{'value': float('nan')}]
    elif change == 'self_reference': p['steps'][1]['id'] = 'self'; p['steps'][1]['arguments'] = [{'ref': 'self'}]
    elif change == 'unused': p['inputs'].append({'id': 'unused', 'file': 'other.wt', 'expected_sha256': 'b'*64})
    elif change == 'output_escape': p['outputs'][0]['name'] = '../original.wt'
    elif change == 'wrong_kind': p['inputs'][0]['file'] = 'wrong.wp'
    with pytest.raises(BridgeError, match=error):
        sdk_flow.Planner(tmp_path, p)


@pytest.mark.parametrize('suffix', ['wt', 'wl', 'wp'])
def test_geometry_save_restores_complete_attribute_table_and_map_header(tmp_path, suffix):
    bridge = Bridge(tmp_path)
    source = sample(bridge, suffix)
    original, view, sections = read_native(source)
    target = source.with_name('sdk-save.'+suffix)
    damaged = bytearray(original)
    index = 9 if suffix == 'wp' else 2
    damaged[sections[index][0]+24:sections[index][0]+152] = b'\0'*128
    damaged[91:260] = b'\0'*169
    target.write_bytes(damaged)
    assert sdk_flow._preserve_saved_tables(source, target) == list(range(1, len(view.geom)+1))
    assert target.read_bytes() == original


def test_scalar_exact_boolean_and_finite_precision():
    assert sdk_flow._scalar(11, True) == '1'
    assert sdk_flow._scalar(5, 1.2345678901234567) == '1.2345678901234567'
    with pytest.raises(BridgeError): sdk_flow._scalar(11, 1)
    with pytest.raises(BridgeError): sdk_flow._scalar(4, 1e100)
    with pytest.raises(BridgeError): sdk_flow._scalar(8, 'bad\0path')
