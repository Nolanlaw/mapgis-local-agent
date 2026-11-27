"""Exact native extensions to the pinned geometry reader; no global monkeypatch.

Types 8..13 are declared by the installed AttCom Enum_Field_Type. BASROOT's
CalLengthOffset/GetFieldOnNumb establish their 1/256-byte storage. Media values
are references only: reading this module never opens or plays their targets.
"""
from __future__ import annotations

import datetime as dt
import io
import math
import struct

import pandas as pd
from pymapgis import reader as native

MEDIA_TYPES = {9: 'multimedia_text', 10: 'multimedia_image', 11: 'multimedia_map',
               12: 'multimedia_audio', 13: 'multimedia_video'}


def invalid(message):
    from .core import BridgeError
    raise BridgeError(message)


class Field(native._Field):
    @property
    def type_name(self):
        return 'boolean' if self.type_code == 8 else MEDIA_TYPES.get(self.type_code, super().type_name)


def read_header(stream, start):
    """Keep every descriptor in order; reject types whose storage is not decoded."""
    stream.seek(start)
    head = stream.read(348)
    if len(head) != 348:
        invalid('ATTRIBUTE_HEADER_TRUNCATED')
    if head[:2] != b'\x60\x44' or head[320:322] != b'\x60\x44':
        invalid('ATTRIBUTE_HEADER_SIGNATURE')
    field_count, count, width = struct.unpack_from('<hih', head, 322)
    if not 0 <= field_count <= 1000 or count < 1 or width < 1:
        invalid('ATTRIBUTE_HEADER_LAYOUT')
    fields = []
    for _ in range(field_count):
        raw = stream.read(39)
        if len(raw) != 39:
            invalid('ATTRIBUTE_DESCRIPTOR_TRUNCATED')
        code = raw[20]
        if code > 13:
            invalid(f'ATTRIBUTE_TYPE_UNSUPPORTED: {code}; descriptor retained in original file')
        name, code, offset, length, decimals = native._FIELD_DESCRIPTOR_STRUCT.unpack(raw)
        storage = struct.unpack_from('<h', raw, 25)[0]
        if storage < 1 or offset < 1 or offset + storage > width:
            invalid('ATTRIBUTE_FIELD_STORAGE')
        minimum = {1: 1, 2: 2, 3: 4, 4: 4, 5: 8, 6: 4, 7: 10, 8: 1}.get(code, 256 if code in MEDIA_TYPES else 1)
        if storage < minimum:
            invalid('ATTRIBUTE_FIELD_STORAGE')
        fields.append(Field(native._decode_gbk(name), code, offset, length, decimals))
    return count, width, fields


def decode(record, field):
    code, offset = field.type_code, field.offset
    if code == 8:
        value = record[offset]
        if value not in (0, 1):
            invalid(f'ATTRIBUTE_BOOLEAN_INVALID: {field.name}={value}')
        return bool(value)
    if code in MEDIA_TYPES:
        raw = record[offset:offset + 256]
        if len(raw) != 256 or b'\0' not in raw:
            invalid('ATTRIBUTE_MULTIMEDIA_STORAGE')
        try:
            return raw.split(b'\0', 1)[0].decode('gbk', errors='strict')
        except UnicodeError:
            invalid('ATTRIBUTE_MULTIMEDIA_ENCODING')
    if code == 6:
        raw = record[offset:offset + 4]
        if raw == b'\0' * 4:
            return None
        try:
            return dt.date(*struct.unpack('<hBB', raw))
        except (ValueError, struct.error):
            invalid(f'ATTRIBUTE_DATE_INVALID: {field.name}; nonzero invalid date is not NULL')
    if code == 7:
        hour, minute, seconds = struct.unpack_from('<BBd', record, offset)
        if not math.isfinite(seconds) or not 0 <= seconds < 60:
            invalid('ATTRIBUTE_TIME_INVALID')
        micros = round(seconds * 1_000_000)
        if abs(seconds - micros / 1_000_000) > 1e-12:
            invalid('ATTRIBUTE_TIME_SUBMICROSECOND_UNREPRESENTABLE')
        try:
            return dt.time(hour, minute, micros // 1_000_000, micros % 1_000_000)
        except ValueError:
            invalid('ATTRIBUTE_TIME_INVALID')
    return native._decode_attribute_field(record, field)


class Reader(native.Reader):
    def _parse(self, stream):
        # The pinned reader's geometry and topology implementation is reused.
        # Its temporary attribute view uses a known byte placeholder for types
        # it would silently skip. Original metadata and values replace that
        # view before this Reader can escape; disk bytes never change.
        stream.seek(0)
        blob = stream.read()
        start = struct.unpack_from('<i', blob, 12)[0]
        sections = native._read_header_entries(io.BytesIO(blob), start, 16)
        kind = native._GEOMETRY_TYPE_MAP[blob[:8]]
        attr, size = sections[native._ATTR_HEADER_INDEX[kind]]
        count, width, fields = read_header(io.BytesIO(blob), attr)
        origin = attr + native._attribute_record_origin(blob[attr:attr + 348], size)[0]
        if origin + count * width > attr + size:
            invalid('ATTRIBUTE_RECORDS_TRUNCATED')
        extended = any(f.type_code >= 6 for f in fields)
        if extended:
            view = bytearray(blob)
            for i, field in enumerate(fields):
                if field.type_code >= 6:
                    view[attr + 348 + i * 39 + 20] = 1
            super()._parse(io.BytesIO(view))
            self._raw_sections[native._ATTR_HEADER_INDEX[kind]] = blob[attr:attr + size]
        else:
            super()._parse(io.BytesIO(blob))
        self.fields = [(f.name, f.type_name, f.length) for f in fields]
        self._field_meta['fields'] = [dict(name=f.name, type_code=f.type_code, offset=f.offset,
                                           length=f.length, decimals=f.decimals) for f in fields]
        self._field_meta['raw_descriptors'] = {f.name: blob[attr + 348 + i * 39:attr + 348 + (i + 1) * 39]
                                               for i, f in enumerate(fields)}
        ids = self._polygon_active_ids if kind == 'POLYGON' else range(1, count)
        # Only columns requiring the exact decoder are rebuilt. Unchanged
        # numeric/string dtypes and the existing byte-preserving paths stay intact.
        for f in fields:
            if f.type_code >= 6:
                values = []
                for rid in ids:
                    active = blob[origin + rid * width] == 1
                    if kind != 'POLYGON':
                        active = active and blob[sections[0][0] + rid * (93 if kind == 'POINT' else 57)] == 1
                    values.append(decode(blob[origin + rid * width:origin + (rid + 1) * width], f) if active else None)
                self.data[f.name] = pd.Series(values, index=self.data.index, dtype=object)
        self._build_geodataframe()
