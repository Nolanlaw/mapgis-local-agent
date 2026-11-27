import pytest

from mapgis_agent.core import BridgeError
from mapgis_agent.sdk_flow import LIBRARY, Planner, sdk_policy


@pytest.fixture
def saw_contract(monkeypatch):
    seen = []

    def describe(root, library, *, class_name=None, interface=None, limit):
        seen.append((library, class_name, interface))
        return {'clsid': '{5CEBBED2-DD0E-11D2-9527-000021DC1F51}', 'next_offset': None,
                'interface': interface or 'I'+class_name,
                'methods': [{'name': 'GetEllipParaById', 'member_id': 1, 'invoke_kind': 1,
                             'parameters': [{'name': 'id', 'type': {'vt': 2}, 'flags': 1},
                                            {'name': 'param', 'type': {'vt': 26, 'element': {'vt': 5}}, 'flags': 2}],
                             'return': {'type': {'vt': 24}}}]}

    monkeypatch.setattr('mapgis_agent.sdk_flow.sdk_describe', describe)
    return seen


def test_inputless_objects_bind_to_real_library_ownership(tmp_path, saw_contract):
    p = {'inputs': [], 'outputs': [], 'library': 'PROJ60.DLL',
         'objects': [{'class': 'EllipPara', 'id': 'ellip'}, {'class': 'TblArea', 'id': 'table', 'library': LIBRARY}],
         'steps': [{'target': 'ellip', 'member': 'GetEllipParaById', 'kind': 'method', 'id': 'found',
                    'arguments': [{'value': 7}, {'out': 'axes'}]}]}
    control = Planner(tmp_path, p).control({}, {})
    assert 'out_real4\taxes' in control
    assert saw_contract == [('proj60.dll', 'EllipPara', None), (LIBRARY, 'TblArea', None),
                            ('proj60.dll', None, 'IEllipPara')]
    p['objects'][1]['library'] = 'AttCom.dll'
    with pytest.raises(BridgeError, match='SDK_CLASS_NOT_ALLOWED'):
        Planner(tmp_path, p)


def test_explicit_member_policy_stops_modal_or_unbound_operations(tmp_path, saw_contract):
    p = {'inputs': [], 'outputs': [], 'library': 'AttCom.dll',
         'objects': [{'class': 'GisObj', 'id': 'file_dialog'}], 'steps': []}
    assert Planner(tmp_path, p).control({}, {}).startswith('SDK1\ncreate')
    p['steps'] = [{'target': 'file_dialog', 'member': 'InputFileName1', 'kind': 'method', 'id': 'blocked'}]
    with pytest.raises(BridgeError, match='SDK_MEMBER_NOT_ALLOWED'):
        Planner(tmp_path, p)
    p['library'] = '../AttCom.dll'
    with pytest.raises(BridgeError, match='SDK_LIBRARY_NOT_IN_EXACT_PROFILE'):
        Planner(tmp_path, p)
    assert len(sdk_policy()['libraries']) == 6
