import json
import struct
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, Polygon, mapping

from mapgis_agent.core import Bridge, BridgeError, annotation_text, native_ids, read_native, sha
from mapgis_agent.creation import prepare_creation

ROOT = Path(__file__).resolve().parents[1]
LOCAL = {'kind': 'local', 'name': 'schema test grid', 'unit': 'metre', 'axis_order': 'xy', 'reference_sha256': '0' * 64}


def sample(bridge, suffix='wt'):
    geometries = {'wt': [Point(1, 2), Point(3, 4), Point(5, 6)],
        'wl': [LineString([(0, 0), (5, 5)]), LineString([(5, 5), (10, 0)])],
        'wp': [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(1, 1), (2, 1), (2, 2), (1, 2)]])]}[suffix]
    rows = [{'type': 'Feature', 'id': str(i), 'geometry': mapping(g),
             'properties': {'ID': 7, 'code': '砂岩', 'old': 100 + i}}
            for i, g in enumerate(geometries, 1)]
    if suffix == 'wt':
        rows[0]['text'] = '保留注记'
    prepare_creation(bridge, {'type': 'FeatureCollection', 'features': rows},
                     {'name': 'source.' + suffix, 'crs': LOCAL, 'map_scale': 1000}, 'create')
    return Path(bridge.apply('create')['output'])


SCHEMA = [{'name': '岩性', 'source': 'code'}, {'name': 'ID', 'source': 'ID'},
          {'name': '厚度', 'type': 'double', 'default': 2.5}]


@pytest.mark.parametrize('suffix', ['wt', 'wl', 'wp'])
def test_attribute_header_records_data_offset(tmp_path, suffix):
    # MapGIS reads attribute records from header bytes 12..15 (348 + 39 per field).
    from mapgis_agent.attributes import prepare_attribute_schema
    bridge = Bridge(tmp_path)
    source = sample(bridge, suffix)
    before, _, sections = read_native(source)
    prepare_attribute_schema(bridge, str(source), sha(before), SCHEMA, 'schema')
    after, _, changed = read_native(Path(bridge.apply('schema')['output']))
    attr = 9 if suffix == 'wp' else 2
    for blob, secs in ((before, sections), (after, changed)):
        start = secs[attr][0]
        fields = struct.unpack_from('<h', blob, start + 322)[0]
        assert struct.unpack_from('<i', blob, start + 12)[0] == 348 + 39 * fields


def anomalies(bridge):
    rows = [{'type': 'Feature', 'id': f'W-{i}', 'properties': {'ID': i, '异常编号': f'W-{i}', '面积km2': i / 8},
             'geometry': mapping(Polygon([(20 * i, 0), (20 * i + 10, 0), (20 * i + 10, 10), (20 * i, 10)]))}
            for i in (1, 2, 36)]
    prepare_creation(bridge, {'type': 'FeatureCollection', 'features': rows},
                     {'name': 'W_anomalies.wp', 'crs': LOCAL, 'map_scale': 1000}, 'create')
    return Path(bridge.apply('create')['output'])


def test_created_wp_string_field_rows_are_where_mapgis_reads_them(tmp_path):
    # W_anomalies.wp (2026-11-05) kept the old stamp 972 at bytes 12..15 instead of 543, so
    # 属性管理 browsed its 异常编号 table from the wrong byte; native tables have no gap.
    blob, reader, sections = read_native(anomalies(Bridge(tmp_path)))
    start, size = sections[9]
    fields, count, width = struct.unpack_from('<hih', blob, start + 322)
    data = start + struct.unpack_from('<i', blob, start + 12)[0]  # MapGIS record origin
    assert data == start + 348 + 39 * fields and data + count * width == start + size
    descriptors = [blob[start + 348 + 39 * i:start + 387 + 39 * i] for i in range(fields)]
    names = [d[:20].rstrip(b'\0').decode('gbk') for d in descriptors]
    assert names == ['ID', '面积', '周长', '异常编号', '面积km2']
    raw = descriptors[3]
    offset, storage, declared = struct.unpack_from('<ihh', raw, 21)
    # Native string descriptor: type 0, capacity plus NUL, flag bytes 00 01 00 00, own index.
    assert (raw[20], storage, declared, raw[29:35]) == (0, 5, 4, b'\0\x01\0\0\x03\0')
    assert [blob[data + i * width] for i in range(count)] == [1] * 4
    assert blob[data + 3 * width + offset:data + 3 * width + offset + storage] == b'W-36\0'
    assert reader.data['异常编号'].tolist() == ['W-1', 'W-2', 'W-36']


def test_stale_writer_offset_is_read_in_place_reported_and_repairable(tmp_path):
    # Pre-fix files keep the stamp while their records follow the descriptors.
    from mapgis_agent.attributes import prepare_attribute_schema
    bridge = Bridge(tmp_path)
    fresh = anomalies(bridge)
    blob, reader, sections = read_native(fresh)
    stale = bytearray(blob)
    struct.pack_into('<i', stale, sections[9][0] + 12, 972)
    path = tmp_path / 'work' / 'W_anomalies.wp'
    path.write_bytes(stale)
    assert read_native(path)[1].data.equals(reader.data)
    flagged = [w for w in bridge.inspect(str(path))['warnings'] if w.startswith('ATTRIBUTE_HEADER_STALE')]
    assert len(flagged) == 1 and '属性表头偏移过期，原生属性管理会错位' in flagged[0]
    assert not any(w.startswith('ATTRIBUTE_HEADER_STALE') for w in bridge.inspect(str(fresh))['warnings'])
    prepare_attribute_schema(bridge, str(path), sha(bytes(stale)),
                             [{'name': n, 'source': n} for n in reader.data.columns], 'hdrfix')
    assert Path(bridge.apply('hdrfix')['output']).read_bytes() == blob


def test_installed_sample_with_leftover_descriptor_reads_from_header_offset(tmp_path):
    # Ku6_2.wl declares two fields but keeps a third descriptor; MapGIS reads its records from 465.
    config = ROOT / 'config.local.json'
    if not config.is_file():
        pytest.skip('installed MapGIS samples are not available')
    source = Path(json.loads(config.read_text(encoding='utf8'))['mapgis_installation']) / 'sample/Ku6_2.wl'
    if not source.is_file():
        pytest.skip('installed MapGIS sample Ku6_2.wl is not available')
    _, reader, _ = read_native(source)
    assert reader.fields == [('ID', 'integer', 8), ('长度', 'double', 15)] and len(reader.data) == 751
    assert reader.data['ID'].tolist()[:3] == [1, 2, 3]
    assert reader.data['长度'].iloc[0] == pytest.approx(18.45528992460185)
    assert not any(w.startswith('ATTRIBUTE_HEADER_STALE') for w in Bridge(tmp_path).inspect(str(source))['warnings'])


@pytest.mark.parametrize('suffix', ['wt', 'wl', 'wp'])
def test_schema_add_drop_rename_reorder_preserves_all_other_sections(tmp_path, suffix):
    from mapgis_agent.attributes import prepare_attribute_schema
    bridge = Bridge(tmp_path)
    source = sample(bridge, suffix)
    before, reader, sections = read_native(source)
    prepared = prepare_attribute_schema(bridge, str(source), sha(before), SCHEMA, 'schema')
    output = Path(bridge.apply('schema')['output'])
    after, result, changed_sections = read_native(output)
    attr = 9 if suffix == 'wp' else 2
    for i, ((start, size), (new_start, new_size)) in enumerate(zip(sections, changed_sections)):
        if i != attr:
            assert after[new_start:new_start + new_size] == before[start:start + size]
    assert native_ids(result) == native_ids(reader)
    assert list(result.data.columns) == ['岩性', 'ID', '厚度']
    assert result.data['岩性'].tolist() == ['砂岩'] * len(reader.geom)
    assert result.data['ID'].tolist() == [7] * len(reader.geom)
    assert result.data['厚度'].tolist() == [2.5] * len(reader.geom)
    assert all(a.equals_exact(b, 0) for a, b in zip(reader.geom, result.geom))
    if suffix == 'wt':
        assert annotation_text(after, changed_sections, 1) == '保留注记'
    assert prepared['source_preserved'] and source.read_bytes() == before
    config_file = ROOT / 'config.local.json'
    if config_file.is_file():
        cfg = json.loads(config_file.read_text(encoding='utf8'))
        if cfg.get('native_backend_legacy_dep_compatibility'):
            from mapgis_agent.native_backend import prepare_native_operation
            (tmp_path / 'config.local.json').write_text(json.dumps({k: cfg[k] for k in
                ('mapgis_installation', 'native_backend_legacy_dep_compatibility')}), encoding='utf8')
            result = prepare_native_operation(bridge, 'resave', str(output), sha(after), {}, 'schema-native')
            assert result['comparison']['records_equal']
            assert result['native_execution']['pid'] != result['native_reopen']['pid']


@pytest.mark.parametrize('fields', [
    [{'name': 'ID', 'source': 'absent'}],
    [{'name': 'ID', 'source': 'ID'}, {'name': 'id', 'type': 'double', 'default': 1}],
    [{'name': 'ID', 'source': 'ID'}, {'name': '很长的超过字段容量的中文字段名', 'type': 'int', 'default': 1}],
    [{'name': 'ID', 'source': 'ID'}, {'name': 'code', 'type': 'string', 'length': 1, 'default': '砂岩'}],
    [{'name': 'ID', 'source': 'ID'}, {'name': 'bad', 'type': 'short', 'default': 1.5}],
    [{'name': 'ID', 'source': 'ID'}, {'name': 'bad', 'type': 'byte', 'default': 256}],
])
def test_schema_rejects_missing_fields_collisions_truncation_and_numeric_loss(tmp_path, fields):
    from mapgis_agent.attributes import prepare_attribute_schema
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    original = source.read_bytes()
    with pytest.raises(BridgeError):
        prepare_attribute_schema(bridge, str(source), sha(original), fields, 'invalid')
    assert source.read_bytes() == original
    assert not (bridge.root / 'deliverables/artifacts/invalid').exists()


def test_schema_numeric_conversion_and_native_save_keep_deleted_physical_slots(tmp_path):
    from mapgis_agent.attributes import prepare_attribute_schema
    from mapgis_agent.native_backend import prepare_native_operation
    config_file = ROOT / 'config.local.json'
    if not config_file.is_file():
        pytest.skip('installed native backend required')
    cfg = json.loads(config_file.read_text(encoding='utf8'))
    if not cfg.get('native_backend_legacy_dep_compatibility'):
        pytest.skip('explicit native compatibility opt-in required')
    (tmp_path / 'config.local.json').write_text(json.dumps({k: cfg[k] for k in
        ('mapgis_installation', 'native_backend_legacy_dep_compatibility')}), encoding='utf8')
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    prepare_native_operation(bridge, 'feature.delete', str(source), sha(source.read_bytes()), {'record_ids': [2]}, 'delete')
    deleted = Path(bridge.apply('delete')['output'])
    prepare_attribute_schema(bridge, str(deleted), sha(deleted.read_bytes()),
        [{'name': 'ID', 'source': 'ID'}, {'name': 'value', 'source': 'old', 'type': 'double'}], 'schema')
    changed = Path(bridge.apply('schema')['output'])
    assert native_ids(read_native(changed)[1]) == [1, 3]
    assert read_native(changed)[1].data['value'].tolist() == [101., 103.]
    native = prepare_native_operation(bridge, 'resave', str(changed), sha(changed.read_bytes()), {}, 'native-save')
    reopened = Path(bridge.apply('native-save')['output'])
    assert native['comparison']['records_equal']
    assert native['native_execution']['pid'] != native['native_reopen']['pid']
    assert native_ids(read_native(reopened)[1]) == [1, 3]


def test_schema_native_types_and_missing_date_time_have_independent_readback(tmp_path):
    from mapgis_agent.attributes import prepare_attribute_schema
    bridge = Bridge(tmp_path)
    source = sample(bridge)
    fields = [{'name': 'ID', 'source': 'ID'}] + [
        {'name': name, 'type': kind, 'default': value} for name, kind, value in [
            ('u8', 'byte', 255), ('i16', 'short', -123), ('i32', 'int', 2147483647),
            ('f32', 'float', 1.25), ('f64', 'double', 2.5), ('date', 'date', '2026-10-29'),
            ('time', 'time', '15:10:20.125'), ('missing_date', 'date', None)]]
    prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()), fields, 'types')
    result = read_native(Path(bridge.apply('types')['output']))[1]
    row = result.data.iloc[0]
    assert [row[c] for c in ['u8', 'i16', 'i32', 'f32', 'f64']] == [255, -123, 2147483647, 1.25, 2.5]
    assert row['date'].isoformat() == '2026-10-29'
    assert row['time'].isoformat() == '15:10:20.125000'
    assert row['missing_date'] is None
    from mapgis_agent.exchange import query_attributes
    typed_file = Path(bridge.status('types')['output'])
    queried = query_attributes(str(typed_file), sha(typed_file.read_bytes()),
        'SELECT "date","time",missing_date FROM features WHERE __record_id=1')
    assert queried['rows'] == [['2026-10-29', '15:10:20.125000', None]]
    with pytest.raises(BridgeError, match='FIELD_TIME_NULL_UNREPRESENTABLE'):
        prepare_attribute_schema(bridge, str(source), sha(source.read_bytes()),
            [{'name': 'ID', 'source': 'ID'}, {'name': 'missing_time', 'type': 'time', 'default': None}], 'null-time')


@pytest.mark.parametrize('types', [('date',), ('time',), ('date', 'time')])
def test_many_regions_with_string_and_date_time_keep_all_attribute_rows_in_native_save(tmp_path, types):
    from shapely.geometry import box
    from mapgis_agent.attributes import prepare_attribute_schema
    from mapgis_agent.native_backend import prepare_native_operation
    from test_native_wp import enable_native
    bridge=Bridge(tmp_path);enable_native(bridge)
    features=[{'type':'Feature','id':str(i+1),'geometry':mapping(box(i%8,i//8,i%8+1,i//8+1)),
               'properties':{'ID':i+1,'height':float(i)}} for i in range(32)]
    prepare_creation(bridge,{'type':'FeatureCollection','features':features},
                     {'name':'many.wp','crs':LOCAL,'map_scale':1000},'many')
    source=Path(bridge.apply('many')['output'])
    fields=[{'name':'ID','source':'ID'},{'name':'岩性','type':'string','length':8,'default':'砂岩'},
            {'name':'value','type':'double','default':1}] + [
        {'name':kind,'type':kind,'default':'2026-10-29' if kind=='date' else '15:10:20.125'} for kind in types]
    prepare_attribute_schema(bridge,str(source),sha(source.read_bytes()),fields,'schema-many')
    edited=Path(bridge.apply('schema-many')['output'])
    result=prepare_native_operation(bridge,'resave',str(edited),sha(edited.read_bytes()),{},'native-many')
    output=Path(bridge.apply('native-many')['output'])
    assert result['comparison']['records_equal']
    assert read_native(output)[1].data['岩性'].tolist()==['砂岩']*32
    assert read_native(output)[1].data['ID'].tolist()==list(range(1,33))
