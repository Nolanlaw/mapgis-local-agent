"""Change native attribute structure without re-encoding geometry or graphics.

The pinned mapgis2shp reader/writer supplies field descriptors and codecs.
Unchanged field values, record flags and every other section retain bytes.
The changed table is packed densely, as required by the native structure loader.
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
import io
import math
from pathlib import Path
import struct
import re
import pandas as pd

from pymapgis import reader as native
from pymapgis import writer

from .artifacts import begin_bundle, finish_bundle, fail_bundle
from .core import Bridge, BridgeError, native_ids, read_native, sha
from .creation import _replace_sections
from .attribute_codec import MEDIA_TYPES, read_header, decode


# Reuse the pinned codecs. Native BAS60 needs a declared display width of ten
# for DATE/TIME, independently of their 4/10 storage bytes. Width eight can
# silently discard later string values when a larger WP table is saved.
FIELD_TYPES = {'string': (0, None, None, 0), 'byte': (1, 1, 3, 0),
    'short': (2, 2, 6, 0), 'int': (3, 4, 8, 0), 'float': (4, 4, 15, 6),
    'double': (5, 8, 15, 6), 'date': (6, 4, 10, 0), 'time': (7, 10, 10, 0),
    'boolean': (8, 1, 1, 0), **{name: (code, 256, 5, 0) for code, name in MEDIA_TYPES.items()}}


def _name(value):
    if not isinstance(value, str) or not value or '\x00' in value or value.casefold() == 'geometry':
        raise BridgeError('FIELD_NAME')
    try:
        if len(value.encode('gbk')) > 19:
            raise ValueError('native field-name capacity')
    except (ValueError, UnicodeError) as exc:
        raise BridgeError('FIELD_NAME: 1..19 GBK bytes required') from exc
    return value


def _convert(value, spec):
    try:
        if spec.type_code == 8:
            if type(value) not in (bool, int) or value not in (False, True):
                raise ValueError('FIELD_BOOLEAN_REQUIRED: true/false or integer 0/1; no NULL storage')
            return bytes([int(value)])
        if spec.type_code in MEDIA_TYPES:
            from pathlib import PureWindowsPath
            if not isinstance(value, str) or '\x00' in value or len(value.encode('gbk')) > 255:
                raise ValueError('FIELD_MULTIMEDIA_REFERENCE: 0..255 GBK bytes required')
            reference = PureWindowsPath(value)
            if reference.drive or reference.root or '..' in reference.parts or any(':' in p for p in reference.parts):
                raise ValueError('FIELD_MULTIMEDIA_REFERENCE: relative local reference without traversal required')
            if any(PureWindowsPath(p).is_reserved() or p.endswith((' ', '.')) or any(c in p for c in '<>"|?*') for p in reference.parts):
                raise ValueError('FIELD_MULTIMEDIA_REFERENCE: valid Windows file components required')
            return value.encode('gbk').ljust(256, b'\0')
        if spec.type_code == 0:
            value = '' if value is None else str(value)
            if '\x00' in value or len(value.encode('gbk')) >= spec.storage:
                raise ValueError('FIELD_STRING_TRUNCATION')
        elif spec.type_code in (1, 2, 3):
            if isinstance(value, bool) or value is None:
                raise ValueError('FIELD_INTEGER_REQUIRED')
            number = Decimal(str(value))
            limits = {1: (0, 255), 2: (-32768, 32767), 3: (-2147483648, 2147483647)}[spec.type_code]
            if not number.is_finite() or number != number.to_integral_value() or not limits[0] <= number <= limits[1]:
                raise ValueError('FIELD_CONVERSION_LOSS')
            value = int(number)
        elif spec.type_code in (4, 5):
            if isinstance(value, bool) or value is None or not math.isfinite(float(value)):
                raise ValueError('FIELD_FINITE_NUMBER_REQUIRED')
            value = float(value)
            if spec.type_code == 4 and abs(value) > 3.4028234663852886e38:
                raise ValueError('FIELD_CONVERSION_LOSS')
        elif spec.type_code == 6 and isinstance(value, str):
            value = datetime.date.fromisoformat(value)
        elif spec.type_code == 7 and isinstance(value, str):
            value = datetime.time.fromisoformat(value)
        if spec.type_code == 7 and value is None:
            raise ValueError('FIELD_TIME_NULL_UNREPRESENTABLE: zero bytes mean midnight, not NULL')
        if spec.type_code == 7 and value is not None and value.tzinfo is not None:
            raise ValueError('FIELD_TIMEZONE_UNREPRESENTABLE')
        return writer._pack_attribute_field(value, spec)
    except (ValueError, TypeError, OverflowError, InvalidOperation, UnicodeError, AttributeError, struct.error) as exc:
        raise BridgeError('FIELD_CONVERSION: ' + spec.name + ': ' + str(exc)) from exc


def _attribute_table(blob:bytes,sections:list,index:int):
    """Decode an actual signed attribute section, not a metadata-only block."""
    start,size=sections[index]
    if size<348 or blob[start:start+2]!=b'\x60\x44' or blob[start+320:start+322]!=b'\x60\x44':return None
    stream=io.BytesIO(blob);count,width,fields=read_header(stream,start);data=stream.tell()
    declared=struct.unpack_from('<h',blob,start+322)[0]
    if count<1 or width<1 or len(fields)!=declared or data+count*width>start+size:raise BridgeError('SCHEMA_ATTRIBUTE_TABLE_LAYOUT')
    descriptors={f.name:blob[start+348+i*39:start+348+(i+1)*39] for i,f in enumerate(fields)}
    if len(descriptors)!=len(fields) or any(f.offset<1 or struct.unpack_from('<h',descriptors[f.name],25)[0]<=0 or f.offset+struct.unpack_from('<h',descriptors[f.name],25)[0]>width for f in fields):
        raise BridgeError('SCHEMA_ATTRIBUTE_FIELD_LAYOUT')
    return {'count':count,'width':width,'fields':fields,'data':data,'descriptors':descriptors}


def prepare_attribute_schema(bridge: Bridge, file: str, expected_sha256: str,
                             fields: list[dict], request_id: str,table:str='primary',
                             attribute_updates:dict|None=None) -> dict:
    """A full desired schema: retained/renamed sources, typed additions/conversions.

    Omitting a source field drops it from the new table. Original bytes are
    retained by the source file and request ledger, not securely erased.
    Names are GBK, string length is capacity in bytes, numeric narrowing is strict.
    New fields require an explicit default; no expression evaluation is provided.
    """
    path = bridge.owned_path(file)
    before, reader, sections = read_native(path)
    if sha(before) != expected_sha256:
        raise BridgeError('STALE_SOURCE')
    if not isinstance(fields, list) or not 1 <= len(fields) <= 1000:
        raise BridgeError('SCHEMA_FIELDS: full desired list of 1..1000 fields required')
    if table not in {'primary','arc','node'}:raise BridgeError('SCHEMA_ATTRIBUTE_TABLE: primary, arc or node required')
    if table!='primary' and reader.shapeType!='POLYGON':raise BridgeError('SCHEMA_ATTRIBUTE_TABLE: arc/node require WP')
    index = native._ATTR_HEADER_INDEX[reader.shapeType] if table=='primary' else {'arc':2,'node':5}[table]
    start, size = sections[index]
    old = before[start:start + size]
    metadata=_attribute_table(before,sections,index)
    if metadata is None:
        if table=='primary':raise BridgeError('SCHEMA_ATTRIBUTE_TABLE_LAYOUT')
        old=writer._build_attribute_section(pd.DataFrame(),[],{'arc':426,'node':387}[table])
        metadata=_attribute_table(old,[(0,len(old))],0);start=0
    count,old_length,source_fields=metadata['count'],metadata['width'],metadata['fields']
    records_start=metadata['data']-start;source_count=count
    if table=='primary':active=set(native_ids(reader))
    else:
        from .topology import _wp_arc_records,_wp_nodes
        arcs=_wp_arc_records(path)[3];active=set(arcs if table=='arc' else _wp_nodes(arcs))
        if max(active,default=0)>=count:
            if any(isinstance(field,dict) and 'source' in field for field in fields):raise BridgeError('SCHEMA_EMPTY_AUXILIARY_TABLE: initialise every field with explicit type/default')
            count=max(active)+1
    by_name = {f.name: f for f in source_fields}
    raw_descriptors = metadata['descriptors']
    planned, used, new_length = [], set(), 1
    for position, field in enumerate(fields):
        if not isinstance(field, dict) or set(field) - {'name', 'source', 'type', 'length', 'default'}:
            raise BridgeError('SCHEMA_FIELD_PARAMETERS')
        name = _name(field.get('name'))
        if name.casefold() in used:
            raise BridgeError('SCHEMA_DUPLICATE_FIELD')
        used.add(name.casefold())
        source = field.get('source')
        if source is not None and (not isinstance(source, str) or source not in by_name):
            raise BridgeError('SCHEMA_SOURCE_FIELD_MISSING')
        if source is None and ('type' not in field or 'default' not in field):
            raise BridgeError('SCHEMA_NEW_FIELD_REQUIRES_TYPE_AND_DEFAULT')
        original = by_name.get(source)
        if 'type' not in field:
            if 'length' in field or 'default' in field:
                raise BridgeError('SCHEMA_RETAINED_FIELD_PARAMETERS')
            raw = bytearray(raw_descriptors[source])
            storage = struct.unpack_from('<h', raw, 25)[0]
            if storage <= 0 or original.offset < 1 or original.offset + storage > old_length:
                raise BridgeError('SCHEMA_INVALID_SOURCE_FIELD_STORAGE')
            raw[:20] = name.encode('gbk').ljust(20, b'\0')
            struct.pack_into('<i', raw, 21, new_length)
            struct.pack_into('<h', raw, 33, position)
            spec = writer._FieldSpec(name, original.type_code, new_length, storage,
                struct.unpack_from('<h', raw, 27)[0], original.decimals, source, bytes(raw))
            new_length += storage
            copy_raw = True
        else:
            kind = field['type']
            if kind not in FIELD_TYPES:
                raise BridgeError('SCHEMA_FIELD_TYPE')
            code, storage, declared, decimals = FIELD_TYPES[kind]
            if kind == 'string':
                capacity = field.get('length')
                if type(capacity) is not int or not 1 <= capacity <= 32000:
                    raise BridgeError('SCHEMA_STRING_LENGTH: 1..32000 GBK bytes required')
                storage, declared = capacity + 1, capacity
            elif 'length' in field:
                raise BridgeError('SCHEMA_NUMERIC_WIDTH_IS_FIXED')
            offset = new_length
            new_length += storage
            spec = writer._FieldSpec(name, code, offset, storage, declared, decimals, name)
            copy_raw = False
        planned.append((field, original, spec, copy_raw))
    if new_length > 32767:
        raise BridgeError('SCHEMA_NATIVE_RECORD_LENGTH')
    if attribute_updates is not None and (not isinstance(attribute_updates,dict) or not attribute_updates):raise BridgeError('SCHEMA_ATTRIBUTE_UPDATES: nonempty physical-ID mapping required')
    updates={}
    for identity,values in (attribute_updates or {}).items():
        if not isinstance(identity,str) or not re.fullmatch(r'[1-9][0-9]*',identity) or int(identity) not in active:
            raise BridgeError('SCHEMA_ATTRIBUTE_UPDATE_ID: active canonical physical ID required')
        if not isinstance(values,dict) or not values or not set(values)<={p[2].name for p in planned}:raise BridgeError('SCHEMA_ATTRIBUTE_UPDATE_FIELDS')
        updates[int(identity)]=values
    descriptors = bytearray()
    for i, (_, _, spec, _) in enumerate(planned):
        raw = bytearray(writer._pack_field_descriptor(i, spec))
        if spec.raw is None and spec.type_code in MEDIA_TYPES:
            raw[30] |= 0x10  # Native CalLengthOffset multimedia flag; no external DB flag.
        descriptors.extend(raw)
    records = bytearray()
    for rid in range(count):
        record = old[records_start + rid * old_length:records_start + (rid + 1) * old_length] if rid<source_count else b'\0'*old_length
        output_record = bytearray(new_length)
        output_record[0] = 1 if table!='primary' and (source_count==1 or rid>=source_count) and rid in active else record[0]
        for field, original, spec, copy_raw in planned:
            if rid in updates and spec.name in updates[rid]:
                output_record[spec.offset:spec.offset+spec.storage]=_convert(updates[rid][spec.name],spec)
            elif copy_raw:
                output_record[spec.offset:spec.offset + spec.storage] = record[original.offset:original.offset + spec.storage]
            else:
                value = decode(record, original) if original is not None else field['default']
                # Slot zero is a dummy; its numeric/date storage is not a data row.
                encoded = b'\0' * spec.storage if rid == 0 else _convert(value, spec)
                output_record[spec.offset:spec.offset + spec.storage] = encoded
        records.extend(output_record)
    header = bytearray(old[:348])
    struct.pack_into('<hih', header, 322, len(planned), count, new_length)
    candidate = _replace_sections(before, sections, {index: bytes(header) + bytes(descriptors) + records})
    payload = {'operation': 'attributes.schema', 'file': str(path), 'expected_sha256': expected_sha256, 'fields': fields}
    if table!='primary':payload['table']=table
    if attribute_updates is not None:payload['attribute_updates']=attribute_updates
    stage, receipt, fresh = begin_bundle(bridge, payload, request_id,
        [{'file': str(path), 'sha256': expected_sha256}], path.name)
    if not fresh:
        return receipt
    try:
        files = stage / 'files'
        files.mkdir()
        result = files / path.name
        result.write_bytes(candidate)
        after, checked, new_sections = read_native(result)
        checked_table=_attribute_table(after,new_sections,index)
        if native_ids(reader) != native_ids(checked) or [f.name for f in checked_table['fields']] != [p[2].name for p in planned]:
            raise BridgeError('SCHEMA_READBACK_IDENTITY')
        if any(after[b:b + n] != before[a:a + m] for i, ((a, m), (b, n)) in enumerate(zip(sections, new_sections)) if i != index):
            raise BridgeError('SCHEMA_NON_ATTRIBUTE_SECTION_CHANGED')
        return finish_bundle(bridge, request_id, files, {'backend': 'pinned mapgis2shp field codecs; native byte-preserving section replacement',
            'source_preserved': True, 'physical_record_ids_preserved': True, 'native_type': reader.shapeType,
            'fields': [f.name for f in checked_table['fields']], 'record_length': new_length, 'non_attribute_sections_byte_identical': True,
            'attribute_table':table,'physical_attribute_rows':len(active),'updated_record_ids':sorted(updates),
            'attribute_layout': 'dense native field storage; declared widths/flags and unchanged value bytes retained',
            'dropped_field_storage': 'omitted in the new table; source and ledger remain available, not secure erasure',
            'native_save_reopen': 'separate native operation; not inferred from the file reader'})
    except Exception as exc:
        fail_bundle(bridge, request_id, exc)
        raise
