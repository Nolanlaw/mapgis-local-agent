import json
from pathlib import Path
import struct

import pytest
from pymapgis import Reader
from shapely.geometry import Point, mapping

from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import native_file_info
from mapgis_agent.native_l2 import audit_l2_exports, normalize_survey_wt_header, probe_native_l2

from test_created_scaled_bounds import TM
from test_native_raster import native_settings


@pytest.mark.parametrize(('format', 'payload', 'count', 'items'), [
    ('4x', struct.pack('<b8f', -3, 1.25, -2.5, 3, 4, 9999, 6, 7, 50000), 29,
     {0: -3, 2: 1.25, 3: -2.5, 4: 3, 5: 4, 6: 6, 7: 7, 8: 50000}),
    ('5x', struct.pack('<bb7fbb6f', 5, 1, 1.25, 2.5, -3, 4, 5, 6, 7, -1, 3, 11, 12, 13, 14, 15, 25000), 17,
     dict(enumerate([5, 1, 1.25, 2.5, -3, 4, 5, 6, 7, -1, 3, 11, 12, 13, 14, 15, 25000]))),
])
def test_real_named_legacy_projection_exports(tmp_path, format, payload, count, items):
    native_settings(tmp_path)
    result = probe_native_l2(tmp_path, format, payload.hex())
    assert result['declared_slots'] == count
    assert {item['index']: item['value'] for item in result['items']} == items
    assert result['input_sha256'] == sha(payload) and result['input_preserved']
    assert result['windows_created'] == result['popup_events'] == result['dep_flags'] == 0
    assert result['private_desktop']
    child = json.loads(Path(result['log']).read_text(encoding='utf-8'))
    assert child['returncode'] == 0 and not child['timed_out'] and child['stderr'] == ''
    assert child['pid'] == result['pid']


@pytest.mark.parametrize(('format', 'payload'), [
    ('LeadAdj.dll', '00'*33), ('4x', '00'*32), ('5x', 'g0'*56),
    ('4x', struct.pack('<bf', 1, float('nan')).hex()+'00'*28),
])
def test_invalid_parameters_are_rejected_without_native_start(tmp_path, format, payload):
    with pytest.raises(BridgeError, match='L2_PARAMETER_BLOCK'):
        probe_native_l2(tmp_path, format, payload)
    assert not (tmp_path/'work').exists()


def test_static_audit_enumerates_actual_named_and_ordinal_entries(tmp_path):
    _, bas60 = native_settings(tmp_path)
    audited = {row['library']: row for row in audit_l2_exports(bas60.parent)['libraries']}
    assert set(audited) == {'NetSolver.dll', 'n60_oper.dll', 'NewTin3DFly.dll', 'LeadAdj.dll',
                            'SuvBase60.dll', 'read_4x.dll', 'RdWt_5x.dll', 'SymEdit6x.dll', 'SymConv.dll'}
    assert all(row['status'] == 'AUDITED' and row['machine'] == '0x014c' for row in audited.values())
    assert any(row['name'] is None for row in audited['RdWt_5x.dll']['exports'])
    for name in ('read_4x.dll', 'RdWt_5x.dll'):
        row = audited[name]
        approved = [entry for entry in row['exports'] if entry['runtime_policy'] == 'projection_view_only']
        assert len(approved) == 4
        assert any('__thiscall' in entry['cpp_demangled'] and 'GetItemValue' in entry['cpp_demangled']
                   for entry in approved)
    lead = audited['LeadAdj.dll']['exports']
    assert [(row['name'], row['ordinal'], row['runtime_policy']) for row in lead] == [
        ('__LeadAdj@0', 1, 'static_inventory_only')]


def _make_wt(tmp_path, crs=TM):
    bridge = Bridge(tmp_path)
    features = {'type': 'FeatureCollection', 'features': [
        {'id': 'a', 'type': 'Feature', 'geometry': mapping(Point(700000., 2880000.)),
         'properties': {'NAME': '测点A', 'GRADE': 42},
         'text': '测点 A', 'style': {'height': 3., 'width': 2.4, 'color': 3, 'font_zh': 1}},
        {'id': 'b', 'type': 'Feature', 'geometry': mapping(Point(700100., 2880200.)),
         'properties': {'NAME': '测点B', 'GRADE': -7}},
    ]}
    prepare_creation(bridge, features, {'name': 'survey.wt', 'crs': crs, 'map_scale': 50000}, 'survey')
    return Path(bridge.apply('survey')['output'])


def test_survey_header_repairs_offsets_and_scale_without_rewriting_payload(tmp_path):
    original = _make_wt(tmp_path)
    native_settings(tmp_path)
    raw, _, entries = read_native(original)
    broken = bytearray(raw)
    table, = struct.unpack_from('<i', broken, 12)
    struct.pack_into('<i', broken, 12, table+1)
    struct.pack_into('<d', broken, 143, float('nan'))
    for index, (offset, size) in enumerate(entries):
        if size:
            struct.pack_into('<i', broken, table+10*index, offset+8)
    struct.pack_into('<i', broken, entries[2][0]+12, 294)  # stale attribute record pointer
    source = tmp_path/'work/mapsuv-header-fixture.wt'
    source.write_bytes(broken)
    target = tmp_path/'deliverables/survey-normalized.wt'
    receipt = normalize_survey_wt_header(source, target, expected_sha256=sha(broken), map_scale=50000, root=tmp_path)
    assert receipt['map_scale_before'] is None and receipt['map_scale_after'] == 50000
    assert receipt['section_offset_delta'] == -8
    assert {change['field'] for change in receipt['changes']} == {
        'section_table_offset', 'map_scale', 'attribute_record_offset',
        *(f'section_{i}_offset' for i, (_, size) in enumerate(entries) if size)}
    assert receipt['native_reopen']['active'] == 2 and not receipt['native_reopen']['warnings']
    assert target.read_bytes() == raw and source.read_bytes() == broken
    assert Reader(target, make_valid=False).data['NAME'].tolist() == ['测点A', '测点B']
    assert native_file_info(tmp_path, str(target), sha(raw))['active'] == 2


def test_survey_requires_explicit_nonlocal_scale_and_never_overwrites(tmp_path):
    original = _make_wt(tmp_path)
    source = tmp_path/'work/missing-scale.wt'
    broken = bytearray(original.read_bytes())
    struct.pack_into('<d', broken, 143, 0.)
    source.write_bytes(broken)
    target = tmp_path/'deliverables/normalized.wt'
    with pytest.raises(BridgeError, match='STALE_SOURCE'):
        normalize_survey_wt_header(source, target, expected_sha256='b'*64, root=tmp_path)
    with pytest.raises(BridgeError, match='SURVEY_WT_SCALE_REQUIRED'):
        normalize_survey_wt_header(source, target, root=tmp_path)
    assert not target.exists()
    with pytest.raises(BridgeError, match='OUTPUT_CONFLICT'):
        normalize_survey_wt_header(source, original, map_scale=50000, root=tmp_path)


def test_survey_rejects_unprovable_layout(tmp_path):
    original = _make_wt(tmp_path)
    broken = bytearray(original.read_bytes())
    struct.pack_into('<i', broken, 12, 0)
    struct.pack_into('<ii', broken, 657, 999999, 1)
    source = tmp_path/'work/no-layout.wt'
    source.write_bytes(broken)
    target = tmp_path/'deliverables/normalized.wt'
    with pytest.raises(BridgeError, match='SURVEY_WT_LAYOUT'):
        normalize_survey_wt_header(source, target, map_scale=50000, root=tmp_path)
    assert not target.exists() and source.read_bytes() == broken
