from pathlib import Path

import pytest

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from test_attribute_schema import sample


def test_per_record_values_use_physical_identity_and_recover_without_losing_later_edits(tmp_path):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    original = source.read_bytes()
    prepared = bridge.prepare(str(source), sha(original), [1, 3], 'varied',
        attribute_updates={'1': {'code': '泥岩'}, '3': {'code': '灰岩', 'old': 300}})
    first = Path(bridge.apply('varied')['output'])
    assert read_native(first)[1].data['code'].tolist() == ['泥岩', '砂岩', '灰岩']
    assert read_native(first)[1].data['ID'].tolist() == [7, 7, 7]
    assert {c['record_id'] for c in prepared['changes']} == {1, 3}
    bridge.prepare(str(first), sha(first.read_bytes()), [2], 'later', style={'color': 2})
    later = Path(bridge.apply('later')['output'])
    bridge.recover('varied', str(later), sha(later.read_bytes()), 'recover')
    restored = Path(bridge.apply('recover')['output'])
    result = read_native(restored)[1]
    assert result.data.equals(read_native(source)[1].data)
    assert result.point_params['color'].iloc[1] == 2
    assert source.read_bytes() == original


def test_per_record_updates_are_bound_into_idempotent_request_identity(tmp_path):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    args = (str(source), sha(source.read_bytes()), [1], 'same')
    bridge.prepare(*args, attribute_updates={'1': {'old': 1}})
    assert bridge.prepare(*args, attribute_updates={'1': {'old': 1}})['state'] == 'PREPARED'
    with pytest.raises(BridgeError, match='REQUEST_ID_CONFLICT'):
        bridge.prepare(*args, attribute_updates={'1': {'old': 2}})


@pytest.mark.parametrize('updates,uniform', [
    ({'2': {'old': 1}}, None), ({'01': {'old': 1}}, None),
    ({'1': {'code': '太长的字段值'}}, None), ({'1': {'old': 1}}, {'old': 2}),
])
def test_per_record_updates_reject_unselected_identity_truncation_and_ambiguous_override(tmp_path, updates, uniform):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    original = source.read_bytes()
    with pytest.raises(BridgeError):
        bridge.prepare(str(source), sha(original), [1], 'bad', attributes=uniform, attribute_updates=updates)
    assert source.read_bytes() == original


@pytest.mark.parametrize('suffix', ['wt', 'wl', 'wp'])
def test_date_time_existing_fields_edit_native_save_and_byte_exact_recovery(tmp_path, suffix):
    from mapgis_agent.attributes import prepare_attribute_schema
    from mapgis_agent.native_backend import prepare_native_operation
    from test_native_wp import enable_native
    bridge = Bridge(tmp_path)
    enable_native(bridge)
    source = sample(bridge, suffix)
    prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()), [
        {'name': 'ID', 'source': 'ID'},
        {'name': '日期', 'type': 'date', 'default': '2026-10-29'},
        {'name': '时间', 'type': 'time', 'default': '15:10:20.125'},
        {'name': 'after', 'type': 'int', 'default': 987}], 'typed')
    typed = Path(bridge.apply('typed')['output'])
    before = typed.read_bytes()
    prepared = bridge.prepare(str(typed), sha(before), [1], 'edit-time',
        attributes={'日期': '2024-02-29', '时间': '00:00:00.000001'})
    output = Path(bridge.apply('edit-time')['output'])
    result = read_native(output)[1]
    assert result.data.iloc[0]['日期'].isoformat() == '2024-02-29'
    assert result.data.iloc[0]['时间'].isoformat() == '00:00:00.000001'
    assert result.data['after'].tolist() == [987] * len(result.geom)
    assert {c['field'] for c in prepared['changes']} == {'日期', '时间'}
    native = prepare_native_operation(bridge, 'resave', str(output), sha(output.read_bytes()), {}, 'native')
    assert native['comparison']['records_equal'] and native['native_execution']['pid'] != native['native_reopen']['pid']
    bridge.recover('edit-time', str(output), sha(output.read_bytes()), 'undo')
    assert Path(bridge.apply('undo')['output']).read_bytes() == before
    bridge.prepare(str(output), sha(output.read_bytes()), [1], 'null-date', attributes={'日期': None})
    assert read_native(Path(bridge.apply('null-date')['output']))[1].data.iloc[0]['日期'] is None
    for i, bad in enumerate([{'时间': None}, {'时间': '12:00:00+08:00'}, {'日期': '2023-02-29'}]):
        with pytest.raises(BridgeError, match='FIELD_'):
            bridge.prepare(str(output), sha(output.read_bytes()), [1], 'bad-time-' + str(i), attributes=bad)
    assert typed.read_bytes() == before
