"""Read exact-build legacy projection blocks and normalize a saved survey WT."""
from __future__ import annotations

import ctypes
import io
import json
import math
import os
from pathlib import Path
import re
import struct
from tempfile import TemporaryDirectory
import uuid

from pymapgis import Reader
from pymapgis import reader as native

from .attribute_codec import read_header
from .core import BridgeError, read_native, sha
from .crs_contract import sidecar_path
from .native_backend import _run_child, backend_configuration, native_file_info


_DLLS = ('NetSolver.dll', 'n60_oper.dll', 'NewTin3DFly.dll', 'LeadAdj.dll', 'SuvBase60.dll',
         'read_4x.dll', 'RdWt_5x.dll', 'SymEdit6x.dll', 'SymConv.dll')
_LEGACY_PROFILES = {
    '4x': {
        'library': 'read_4x.dll', 'sha256': 'cc6794165ed0316cc6820b07e21d03685bde680bacc67b3bdd612c7623fce3a9',
        'class': 'C4x_MAP_PARA', 'size': 33, 'slots': 29,
        'layout': {0: (0, 'b'), **{i: (offset, 'f') for i, offset in enumerate((1, 5, 9, 13, 21, 25, 29), 2)}},
    },
    '5x': {
        'library': 'RdWt_5x.dll', 'sha256': '17c2c4808e48a38da937c8abd486450fa0d8536e8bff0c3794b6f209ae955779',
        'class': 'C50_MAP_PARA', 'size': 56, 'slots': 17,
        'layout': {0: (0, 'b'), 1: (1, 'b'), **{i: (2+4*(i-2), 'f') for i in range(2, 9)},
                   9: (30, 'b'), 10: (31, 'b'), **{i: (32+4*(i-11), 'f') for i in range(11, 17)}},
    },
}


def _symbols(class_name):
    return (f'??0{class_name}@@QAE@PAX@Z', f'??1{class_name}@@UAE@XZ',
            f'?GetItemNum@{class_name}@@QAEFXZ', f'?GetItemValue@{class_name}@@QAENF@Z')


def audit_l2_exports(program):
    """Enumerate the PE export tables without loading any MapGIS DLL as code."""
    import pefile

    program = Path(program)
    demangle = ctypes.WinDLL('dbghelp', use_last_error=True).UnDecorateSymbolName
    demangle.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32)
    demangle.restype = ctypes.c_uint32
    rows = []
    for name in _DLLS:
        path = program/name
        if not path.is_file():
            rows.append({'library': name, 'status': 'MISSING', 'path': str(path)})
            continue
        blob = path.read_bytes()
        digest = sha(blob)
        try:
            pe = pefile.PE(data=blob, fast_load=True)
            try:
                pe.parse_data_directories([pefile.DIRECTORY_ENTRY[key] for key in (
                    'IMAGE_DIRECTORY_ENTRY_EXPORT', 'IMAGE_DIRECTORY_ENTRY_IMPORT')])
                table = pe.DIRECTORY_ENTRY_EXPORT
                allowed = {symbol for profile in _LEGACY_PROFILES.values()
                           if profile['library'] == name and profile['sha256'] == digest
                           for symbol in _symbols(profile['class'])}
                exports = []
                for symbol in table.symbols:
                    spelling = symbol.name.decode('ascii', errors='backslashreplace') if symbol.name else None
                    decoded = ctypes.create_string_buffer(8192)
                    unmangled = (decoded.value.decode('ascii', errors='backslashreplace')
                                 if symbol.name and demangle(symbol.name, decoded, len(decoded), 0) else None)
                    section = pe.get_section_by_rva(symbol.address)
                    executable = bool(section and section.Characteristics & 0x20000000 and not symbol.forwarder)
                    exports.append({'ordinal': symbol.ordinal, 'name': spelling, 'rva': symbol.address,
                                    'cpp_demangled': unmangled if spelling and spelling.startswith('?') else None,
                                    'forwarder': symbol.forwarder.decode('ascii') if symbol.forwarder else None,
                                    'executable_section': executable,
                                    'runtime_policy': ('projection_view_only' if executable and spelling in allowed
                                                       else 'static_inventory_only')})
                rows.append({'library': name, 'status': 'AUDITED', 'path': str(path), 'sha256': digest,
                             'machine': f'0x{pe.FILE_HEADER.Machine:04x}',
                             'named_export_count': table.struct.NumberOfNames,
                             'export_address_count': table.struct.NumberOfFunctions,
                             'imports': [entry.dll.decode('ascii') for entry in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', ())],
                             'exports': sorted(exports, key=lambda row: (row['ordinal'], row['name'] or ''))})
            finally:
                pe.close()
        except (pefile.PEFormatError, AttributeError, ValueError) as exc:
            rows.append({'library': name, 'status': 'PE_READ_FAILED', 'path': str(path), 'sha256': digest,
                         'error': str(exc)})
    return {'schema': 'mapgis-agent/l2-exports-audit/1', 'provider': 'pefile + Windows DbgHelp',
            'scope': 'Static PE declarations and section permissions. Runtime allowlist is hash-bound.',
            'libraries': rows}


def probe_native_l2(root, format, parameter_block_hex):
    """Read only the allowlisted 4.x or 5.x projection block through its own DLL."""
    profile = _LEGACY_PROFILES.get(format)
    if (profile is None or not isinstance(parameter_block_hex, str)
            or len(parameter_block_hex) != 2*profile['size']
            or not re.fullmatch(r'[0-9a-fA-F]+', parameter_block_hex)):
        raise BridgeError('L2_PARAMETER_BLOCK: 4x requires 33 packed bytes, 5x requires 56 packed bytes')
    blob = bytes.fromhex(parameter_block_hex)
    expected = [{'index': index, 'value': struct.unpack_from('<'+kind, blob, offset)[0]}
                for index, (offset, kind) in profile['layout'].items()]
    if any(not math.isfinite(item['value']) for item in expected):
        raise BridgeError('L2_PARAMETER_BLOCK: all decoded scalar slots must be finite')
    root = Path(root).resolve()
    executable, bas60 = backend_configuration(root)
    library = (bas60.parent/profile['library']).resolve()
    if library.parent != bas60.parent or not library.is_file() or sha(library.read_bytes()) != profile['sha256']:
        raise BridgeError('NATIVE_L2_BUILD_MISMATCH: expected the pinned legacy reader')
    folder = (root/'work/native-l2'/uuid.uuid4().hex).resolve()
    if not folder.is_relative_to(root/'work'):
        raise BridgeError('Writes require a project-owned destination')
    folder.mkdir(parents=True)
    packet = folder/'parameters.bin'
    packet.write_bytes(blob)
    host, events = _run_child([str(executable), 'l2', format, str(library), str(packet)], folder, 'workers.json',
                              mfc_windows=True, guard_process=True)
    data = next((event for event in events if event['event'] == 'l2_projection_items'), None)
    windows = next((event for event in events if event['event'] == 'l2_window_trace'), None)
    if (data is None or windows is None or data.get('format') != format or data.get('items') != expected
            or data.get('declared_slots') != profile['slots'] or data.get('input_preserved') is not True
            or windows.get('windows_created') != 0 or windows.get('popup_events') != 0
            or packet.read_bytes() != blob or sha(library.read_bytes()) != profile['sha256']):
        raise BridgeError('NATIVE_L2_READBACK: DLL, input, native scalars or window trace differ')
    return {**host, 'format': format, 'library': profile['library'], 'dll_sha256': profile['sha256'],
            'exports': list(_symbols(profile['class'])), 'input_sha256': sha(blob), 'input_preserved': True,
            'declared_slots': data['declared_slots'], 'items': data['items'],
            'windows_created': windows['windows_created'], 'popup_events': windows['popup_events']}


_WT_HEADER_TABLE = 657
_WT_RECORD_SIZE = 93
_WT_ATTRIBUTE_INDEX = native._ATTR_HEADER_INDEX['POINT']
_SCALE_OFFSET = 143


def _wt_sections(blob, table, shift=0):
    if table < 260 or table + 160 > len(blob):
        raise ValueError('section table outside the file')
    entries = native._read_header_entries(io.BytesIO(blob), table, 16)
    sections = [(start+shift if width else start, width) for start, width in entries]
    spans = sorted((start, start+width) for start, width in sections if width > 0)
    if (not spans or any(width < 0 or start < 0 for start, width in sections)
            or spans[0][0] < table+160 or spans[-1][1] > len(blob)
            or any(later[0] < prior[1] for prior, later in zip(spans, spans[1:]))):
        raise ValueError('invalid section spans')
    if shift and (spans[0][0] != table+160 or spans[-1][1] != len(blob)
                  or any(later[0] != prior[1] for prior, later in zip(spans, spans[1:]))):
        raise ValueError('translated sections lack a contiguous packed layout')
    _, point_bytes = sections[0]
    start, size = sections[_WT_ATTRIBUTE_INDEX]
    if not point_bytes or point_bytes % _WT_RECORD_SIZE or size < 348:
        raise ValueError('unsupported point or attribute record layout')
    count, width, fields = read_header(io.BytesIO(blob), start)
    origin, stale = native._attribute_record_origin(blob[start:start+348], size)
    if (count != point_bytes//_WT_RECORD_SIZE or origin < 348+39*len(fields)
            or origin+count*width > size):
        raise ValueError('attribute records do not match point slots')
    return sections, (start, origin, stale)


def _find_wt_sections(blob, declared):
    try:
        return declared, 0, *_wt_sections(blob, declared)
    except ValueError:
        pass
    candidates = []
    for table in dict.fromkeys((declared, _WT_HEADER_TABLE)):
        if not 260 <= table <= len(blob)-160:
            continue
        first, = struct.unpack_from('<i', blob, table)
        for shift in dict.fromkeys((0, table+160-first)):
            try:
                candidates.append((table, shift, *_wt_sections(blob, table, shift)))
            except ValueError:
                continue
    if len(candidates) != 1:
        raise BridgeError('SURVEY_WT_LAYOUT: a unique point and attribute layout could not be established')
    return candidates[0]


def normalize_survey_wt_header(source_path, output_path, *, expected_sha256=None, map_scale=None, root=None):
    """Repair only evidenced header offsets or an explicit scale, preserving native payload bytes."""
    source = Path(source_path).resolve(strict=True)
    requested = Path(output_path)
    output = requested.parent.resolve()/requested.name
    if source.suffix.lower() != '.wt' or output.suffix.lower() != '.wt':
        raise BridgeError('SURVEY_WT_FORMAT: source and output must be WT files')
    if os.path.lexists(output) or os.path.lexists(sidecar_path(output)):
        raise BridgeError('OUTPUT_CONFLICT: survey normalization requires a fresh output')
    if os.path.lexists(sidecar_path(source)):
        raise BridgeError('SURVEY_WT_CRS_CONTRACT: use the hash-bound CRS workflow for this layer')
    blob = source.read_bytes()
    before = sha(blob)
    if expected_sha256 is not None and expected_sha256 != before:
        raise BridgeError('STALE_SOURCE')
    if len(blob) < 817 or blob[:8] != b'WMAP`D22':
        raise BridgeError('SURVEY_WT_FORMAT: expected a MapGIS 6.x point layer')
    declared, = struct.unpack_from('<i', blob, 12)
    table, shift, sections, (attr, origin, stale) = _find_wt_sections(blob, declared)
    candidate = bytearray(blob)
    changes = []

    def replace(offset, fmt, value, field):
        packed = struct.pack(fmt, value)
        old = candidate[offset:offset+len(packed)]
        if old != packed:
            changes.append({'field': field, 'offset': offset, 'before_hex': old.hex(), 'after_hex': packed.hex()})
            candidate[offset:offset+len(packed)] = packed

    replace(12, '<i', table, 'section_table_offset')
    if shift:
        for i, (start, size) in enumerate(sections):
            if size:
                replace(table+10*i, '<i', start, f'section_{i}_offset')
    if stale:
        replace(attr+12, '<i', origin, 'attribute_record_offset')
    old_scale, = struct.unpack_from('<d', blob, _SCALE_OFFSET)
    local = blob[91:93] == blob[109:111] == b'\0\0'
    invalid_scale = not math.isfinite(old_scale) or old_scale < 0 or (old_scale == 0 and not local)
    if map_scale is None and invalid_scale:
        if not local:
            raise BridgeError('SURVEY_WT_SCALE_REQUIRED: declare the source scale for a nonlocal or ambiguous header')
        map_scale = 0.
    if map_scale is not None:
        if (type(map_scale) not in (int, float) or not math.isfinite(map_scale)
                or map_scale < 0 or (map_scale == 0 and not local)):
            raise BridgeError('SURVEY_WT_SCALE_REQUIRED: finite positive scale, or zero for a documented local header')
        replace(_SCALE_OFFSET, '<d', map_scale, 'map_scale')
    projected_scale = struct.unpack_from('<d', candidate, _SCALE_OFFSET)[0]
    output.parent.mkdir(parents=True, exist_ok=True)
    config_root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    config = json.loads((config_root/'config.local.json').read_text(encoding='utf-8'))
    runtime = {key: config[key] for key in ('mapgis_installation', 'native_backend_legacy_dep_compatibility')
               if key in config}
    with TemporaryDirectory(prefix='survey-wt-', dir=output.parent) as temporary:
        sandbox = Path(temporary)
        (sandbox/'config.local.json').write_text(json.dumps(runtime), encoding='utf-8')
        staged = sandbox/output.name
        staged.write_bytes(candidate)
        final_blob, reader, final_sections = read_native(staged)
        pymapgis = Reader(staged, make_valid=False)
        if final_sections != sections or not all(math.isfinite(value) for point in reader.geom for value in point.coords[0]):
            raise BridgeError('SURVEY_WT_READBACK: invalid point coordinates or section directory')
        digest = sha(final_blob)
        reopened = native_file_info(sandbox, str(staged), digest)
        if reopened['active'] != len(reader.geom) or reopened['warnings']:
            raise BridgeError('SURVEY_WT_READBACK: BAS60 counts or attribute warnings differ')
        if source.read_bytes() != blob or os.path.lexists(sidecar_path(source)):
            raise BridgeError('STALE_SOURCE: survey input changed before publication')
        try:
            os.link(staged, output)
        except FileExistsError as exc:
            raise BridgeError('OUTPUT_CONFLICT: survey WT output already exists') from exc
    return {'file': str(output), 'input_sha256': before, 'sha256': digest, 'source_preserved': True,
            'changes': changes, 'map_scale_before': old_scale if math.isfinite(old_scale) else None,
            'map_scale_after': projected_scale, 'coordinate_header_hex': blob[91:111].hex(),
            'section_table_offset': table, 'section_offset_delta': shift,
            'pymapgis': {'type': pymapgis.shapeType, 'record_slots': len(pymapgis.geom)},
            'native_reopen': {**reopened, 'file': str(output)}}
