import datetime as dt
import struct
from pathlib import Path

import pytest

from mapgis_agent.attributes import prepare_attribute_schema
from mapgis_agent.core import Bridge, BridgeError, read_native, sha
from test_attribute_schema import sample


def schema_file(bridge, suffix='wt'):
    source = sample(bridge, suffix)
    fields = [{'name': 'ID', 'source': 'ID'},
              {'name': 'enabled', 'type': 'boolean', 'default': False},
              {'name': 'stamp', 'type': 'time', 'default': '12:34:56.123456'}]
    fields += [{'name': name, 'type': kind, 'default': value} for name, kind, value in
               [('note', 'multimedia_text', 'assets/notes.txt'),
                ('photo', 'multimedia_image', 'assets/露头.bmp'),
                ('mapref', 'multimedia_map', 'assets/section.wt'),
                ('audio', 'multimedia_audio', 'assets/sample.wav'),
                ('video', 'multimedia_video', '')]]
    prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()), fields, 'extended',
                             attribute_updates={'1': {'enabled': True}})
    return source, Path(bridge.apply('extended')['output'])


@pytest.mark.parametrize('suffix', ['wt', 'wl', 'wp'])
def test_native_boolean_and_five_multimedia_fields_keep_all_descriptors_and_slots(tmp_path, suffix):
    bridge = Bridge(tmp_path)
    source, output = schema_file(bridge, suffix)
    before, old, old_sections = read_native(source)
    after, new, sections = read_native(output)
    assert len(new.fields) == 8
    assert new.data['enabled'].tolist() == [True] + [False] * (len(old.geom) - 1)
    assert new.data['stamp'].tolist() == [dt.time(12, 34, 56, 123456)] * len(old.geom)
    assert new.data['photo'].tolist() == ['assets/露头.bmp'] * len(old.geom)
    assert new.data['video'].tolist() == [''] * len(old.geom)
    assert [f['type_code'] for f in new._field_meta['fields']] == [3, 8, 7, 9, 10, 11, 12, 13]
    index = 9 if suffix == 'wp' else 2
    assert all(before[a:a+n] == after[b:b+m] for i, ((a,n),(b,m)) in
               enumerate(zip(old_sections, sections)) if i != index)
    retained = [{'name': name, 'source': name} for name in reversed(new.data.columns)]
    prepare_attribute_schema(bridge, str(output), sha(after), retained, 'retained')
    again = read_native(Path(bridge.apply('retained')['output']))[1]
    assert again.data['enabled'].tolist() == new.data['enabled'].tolist()
    assert again.data['photo'].tolist() == new.data['photo'].tolist()


def test_fractional_time_readback_does_not_floor_a_binary64_rounding_residual(tmp_path):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()),
                             [{'name': 'clock', 'type': 'time', 'default': '12:34:56.123456'}], 'time')
    output = Path(bridge.apply('time')['output'])
    assert read_native(output)[1].data['clock'].iloc[0] == dt.time(12, 34, 56, 123456)


@pytest.mark.parametrize('value', [None, 2, -1, 'false', 0.0])
def test_boolean_requires_json_boolean_or_exact_integer_zero_one(tmp_path, value):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    with pytest.raises(BridgeError, match='BOOLEAN'):
        prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()),
                                 [{'name': 'flag', 'type': 'boolean', 'default': value}], 'bad')


@pytest.mark.parametrize('value', ['../private.txt', 'C:/file.bmp', 'https://host/file', '\\server\\share', 'a\x00b'])
def test_new_multimedia_reference_is_a_relative_local_path_without_traversal(tmp_path, value):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    with pytest.raises(BridgeError, match='MULTIMEDIA'):
        prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()),
                                 [{'name': 'image', 'type': 'multimedia_image', 'default': value}], 'bad')


def test_unknown_native_type_is_visible_as_an_explicit_error_instead_of_dropped(tmp_path):
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    blob, _, sections = read_native(source)
    raw = bytearray(blob)
    raw[sections[2][0] + 348 + 20] = 250
    unsupported = tmp_path/'work'/'unsupported.wt'
    unsupported.write_bytes(raw)
    with pytest.raises(BridgeError, match='ATTRIBUTE_TYPE_UNSUPPORTED.*250'):
        read_native(unsupported)


def patch_source(bridge):
    source = sample(bridge)
    prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()),
        [{'name': '岩性', 'source': 'code'}, {'name': '启用', 'type': 'boolean', 'default': True},
         {'name': '照片', 'type': 'multimedia_image', 'default': 'media/photo.bmp'},
         {'name': '尾字段', 'type': 'string', 'length': 20, 'default': 'untouched'}], 'patch-source')
    return Path(bridge.apply('patch-source')['output'])


def test_ordinary_text_patch_never_zeroes_intervening_boolean_or_media_fields(tmp_path):
    bridge = Bridge(tmp_path)
    source = patch_source(bridge)
    bridge.prepare(str(source), sha(source.read_bytes()), [1], 'patch', attributes={'岩性': '泥岩'})
    output = Path(bridge.apply('patch')['output'])
    data = read_native(output)[1].data
    assert data['岩性'].tolist() == ['泥岩', '砂岩', '砂岩']
    assert data['启用'].tolist() == [True, True, True]
    assert data['照片'].tolist() == ['media/photo.bmp'] * 3
    assert data['尾字段'].tolist() == ['untouched'] * 3


def test_fixed_patch_and_recovery_support_boolean_and_media_without_rebuilding_schema(tmp_path):
    bridge = Bridge(tmp_path)
    source = patch_source(bridge)
    bridge.prepare(str(source), sha(source.read_bytes()), [1], 'patch', attributes={'启用': False, '照片': ''})
    output = Path(bridge.apply('patch')['output'])
    assert read_native(output)[1].data['启用'].tolist() == [False, True, True]
    assert read_native(output)[1].data['照片'].tolist() == ['', 'media/photo.bmp', 'media/photo.bmp']
    bridge.recover('patch', str(output), sha(output.read_bytes()), 'recovered')
    restored = Path(bridge.apply('recovered')['output'])
    assert restored.read_bytes() == source.read_bytes()

