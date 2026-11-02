"""Typed original SDK calls, confined to owned copies and a private desktop.

The initial policy exposes value objects and geometry workspaces. The installed
type library supplies parameter and result contracts. It is not a promise that
every Automation method or every MapGIS menu has been tested.
"""
from __future__ import annotations

import io
import math
from pathlib import Path, PureWindowsPath
import re
import shutil
import struct
from pymapgis import reader as native

from .artifacts import begin_bundle, finish_bundle, fail_bundle
from .core import Bridge, BridgeError, json_text, read_native, native_ids
from .native_backend import backend_configuration, _run_child, native_file_info
from .native_com import private_com_runtime
from .creation import _replace_sections
from .multimedia import project_media_dependencies, copy_media_dependencies
from .topology import _wp_arc_records, validate_wp_topology
from .native_paths import NativePathAlias
from .raster import file_sha
from .sdk_catalog import sdk_describe

LIBRARY = 'MapGisBasCom1.DLL'
VALUE_CLASSES = frozenset({
    'D_Dot', 'L_Dot', 'D_3Dot', 'L_3Dot', 'D_Rect', 'D_RectSet',
    'D_DotSet', 'L_DotSet', 'D_3DotSet', 'L_3DotSet',
    'Date_Stru', 'Time_Stru', 'TimeStamp_Stru', 'Tran_Par', 'Map_Para',
    'Pnt_Info_Sub', 'Pnt_Info_Note', 'Pnt_Info_Cir', 'Pnt_Info_Arc',
    'Pnt_Info_Image', 'Pnt_Info_Text', 'Pnt_Info', 'Lin_Info', 'Reg_Info',
    'Pnt_Pad_Note', 'Pnt_Pad_Sub', 'Pnt_Pad_Cir', 'Pnt_Pad_Arc',
    'Pnt_Pad_Image', 'Pnt_Pad_Text', 'Pnt_Pad', 'Lin_Pad', 'Reg_Pad',
    'LayerOnOffPad', 'LayerPad', 'IDList', 'LONGList', 'Tic_Dot', 'Tic_DotSet'})
AREA_CLASSES = frozenset({'PntArea', 'LinArea', 'RegArea'})
AREA_METHODS = frozenset({'Load', 'Save', 'RefreshRect', 'GetExistFlag',
    'GetRect', 'GetRectLayer', 'GetLayer', 'GetInfo', 'UpdateInfo',
    'GetType', 'GetDatLen', 'GetPos', 'GetPos3D', 'Get3D', 'Get',
    'UpdatePos', 'UpdatePos3D', 'Update3D', 'Update', 'GetAllExistNo',
    'GetMapParam', 'GetFileName', 'GetMMDPath', 'Near', 'Transform'})
AREA_PROPERTIES = frozenset({'type', 'Name', 'Changed', 'Empty', 'LastErrCode',
    'rect', 'count', 'logCount'})
SAFE_INTERFACES = frozenset('I'+n for n in VALUE_CLASSES | AREA_CLASSES)
SUPPORTED_VT = {0, 1, 2, 3, 4, 5, 7, 8, 9, 11, 12}


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,79}', value):
        raise BridgeError('SDK_IDENTIFIER')
    return value


def _hex(value):
    if not isinstance(value, str) or '\0' in value or len(value.encode('utf-16le')) > 524288:
        raise BridgeError('SDK_STRING')
    return value.encode('utf-16le').hex() or '-'


def _shape(description):
    depth = 0
    while description.get('vt') == 26:
        description = description['element']; depth += 1
    vt = description.get('vt')
    if vt == 29:
        if description.get('typekind') == 0:
            vt = 3
        elif description.get('typekind') in (3, 4):
            return {'vt': 9, 'interface': description['declared_name'], 'depth': depth}
    if vt == 24:
        vt = 0
    if vt not in SUPPORTED_VT:
        raise BridgeError('SDK_TYPE_UNSUPPORTED: '+str(description))
    return {'vt': vt, 'depth': depth}


def _scalar(vt, value):
    if vt in (0, 1) and value is None:
        return '-'
    if vt == 8:
        return _hex(value)
    if vt == 11 and type(value) is bool:
        return '1' if value else '0'
    if vt in (2, 3) and type(value) is int:
        bound = 1 << (15 if vt == 2 else 31)
        if -bound <= value < bound:
            return str(value)
    if vt in (4, 5, 7) and type(value) in (int, float) and math.isfinite(value):
        number = float(value)
        if vt == 4:
            try:
                number = struct.unpack('<f', struct.pack('<f', number))[0]
            except (OverflowError, struct.error) as exc:
                raise BridgeError('SDK_FLOAT32_OVERFLOW') from exc
        if math.isfinite(number):
            return repr(number)
    raise BridgeError('SDK_SCALAR_TYPE: vt='+str(vt))


def sdk_policy():
    return {'library': LIBRARY, 'value_classes': sorted(VALUE_CLASSES),
        'geometry_workspaces': sorted(AREA_CLASSES),
        'workspace_methods': sorted(AREA_METHODS),
        'workspace_read_properties': sorted(AREA_PROPERTIES),
        'scope': 'Typed original SDK in private process; workspace outputs preserve source attributes and map parameters.',
        'coordinates': 'Original SDK workspace/drawing coordinates, without automatic source-unit conversion. Read GetPos/rect first; use existing higher-level tools for source-unit transforms.',
        'limitations': ['No dialogs, frontend input, database/remote access or physical printing.',
            'No SAFEARRAY, raw binary, in-out parameters or arbitrary native addresses.',
            'Geometry output keeps physical IDs, primary attributes, full map parameters; use dedicated tools to edit attributes/map parameters.',
            'Original count is a physical slot count: observed three-record samples return 4 including slot zero; use native_file_info for active records.',
            'A catalog signature is not an execution claim.']}


class Planner:
    def __init__(self, root, parameters):
        self.root = Path(root)
        if not isinstance(parameters, dict) or set(parameters) != {'inputs', 'outputs', 'objects', 'steps'}:
            raise BridgeError('SDK_FLOW_PARAMETERS')
        self.parameters = parameters
        self.inputs, self.outputs, self.objects, self.steps = (parameters[k] for k in ('inputs', 'outputs', 'objects', 'steps'))
        if not all(isinstance(v, list) for v in (self.inputs, self.outputs, self.objects, self.steps)) or not 1 <= len(self.inputs) <= 20 or len(self.outputs) > 20 or not 1 <= len(self.objects) <= 100 or not 1 <= len(self.steps) <= 1000:
            raise BridgeError('SDK_FLOW_LIMITS')
        self.sources = {}; self.destinations = {}; self.symbols = {}; self.contracts = {}; self.used_inputs = set(); self.saved_outputs = set()
        for entry in self.inputs:
            if not isinstance(entry, dict) or set(entry) != {'id', 'file', 'expected_sha256'}:
                raise BridgeError('SDK_INPUT')
            identity = _id(entry['id'])
            if identity in self.sources:
                raise BridgeError('SDK_DUPLICATE_INPUT')
            self.sources[identity] = entry
        output_names = set()
        for entry in self.outputs:
            if not isinstance(entry, dict) or set(entry) != {'id', 'name', 'source'}:
                raise BridgeError('SDK_OUTPUT')
            identity, name = _id(entry['id']), entry['name']
            if identity in self.destinations or entry['source'] not in self.sources:
                raise BridgeError('SDK_OUTPUT_SOURCE')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,79}\.(?:wt|wl|wp)', name) or PureWindowsPath(name).is_reserved() or name.casefold() in output_names:
                raise BridgeError('SDK_OUTPUT_FILENAME')
            output_names.add(name.casefold()); self.destinations[identity] = entry
        self.lines = ['SDK1']; self.target_sources = {}
        for obj in self.objects:
            if not isinstance(obj, dict) or set(obj) != {'id', 'class'} or obj['class'] not in VALUE_CLASSES | AREA_CLASSES:
                raise BridgeError('SDK_CLASS_NOT_ALLOWED')
            identity = self.add(obj['id'], {'vt': 9, 'interface': 'I'+obj['class'], 'class': obj['class']})
            contract = self.contract(obj['class'])
            self.lines.append('\t'.join(('create', identity, _hex(LIBRARY), _hex(contract['clsid']))))
        for step in self.steps:
            self.step(step)
        if self.used_inputs != set(self.sources) or self.saved_outputs != set(self.destinations):
            raise BridgeError('SDK_UNUSED_INPUT_OR_UNSAVED_OUTPUT')

    def contract(self, class_name=None, interface=None):
        key = class_name or interface
        if key not in self.contracts:
            self.contracts[key] = sdk_describe(self.root, LIBRARY, class_name=class_name, interface=interface, limit=250)
            if self.contracts[key]['next_offset'] is not None:
                raise BridgeError('SDK_CONTRACT_PAGE_REQUIRED')
        return self.contracts[key]

    def add(self, identity, shape):
        identity = _id(identity)
        if identity in self.symbols:
            raise BridgeError('SDK_DUPLICATE_SYMBOL: '+identity)
        self.symbols[identity] = shape
        return identity

    def step(self, step):
        if not isinstance(step, dict) or not {'id', 'target', 'member', 'kind'} <= set(step) or set(step) - {'id', 'target', 'member', 'kind', 'arguments', 'expect'}:
            raise BridgeError('SDK_STEP')
        target = self.symbols.get(step['target'])
        if not target or target.get('vt') != 9 or target.get('interface') not in SAFE_INTERFACES:
            raise BridgeError('SDK_TARGET_NOT_ALLOWED')
        flags = {'method': 1, 'get': 2, 'put': 4, 'putref': 8}.get(step['kind'])
        if not flags:
            raise BridgeError('SDK_INVOKE_KIND')
        interface, member = target['interface'], step['member']
        if not isinstance(member, str):
            raise BridgeError('SDK_MEMBER')
        if interface in {'I'+n for n in AREA_CLASSES}:
            if not ((flags == 1 and member in AREA_METHODS) or (flags == 2 and member in AREA_PROPERTIES)):
                raise BridgeError('SDK_WORKSPACE_MEMBER_NOT_ALLOWED: '+member)
        elif re.search(r'(?:load|save|file|path|print|dialog|modparam|db|register)', member, re.I):
            raise BridgeError('SDK_VALUE_IO_MEMBER_NOT_ALLOWED: '+member)
        methods = [m for m in self.contract(interface=interface)['methods'] if m['name'] == member and m['invoke_kind'] == flags]
        if len(methods) != 1:
            raise BridgeError('SDK_MEMBER_NOT_FOUND: '+member)
        method = methods[0]; params = method['parameters']; arguments = step.get('arguments', [])
        if not isinstance(arguments, list) or len(arguments) > len(params):
            raise BridgeError('SDK_ARGUMENT_COUNT')
        arguments = list(arguments)
        for p in params[len(arguments):]:
            if 'default' not in p or p['flags'] & 2:
                raise BridgeError('SDK_ARGUMENT_REQUIRED: '+p['name'])
            arguments.append({'value': p['default']})
        # No implicit file selection or save-to-original. The other Load/Save
        # parameters are database/login arguments and must be empty strings.
        if member in {'Load', 'Save'}:
            if len(arguments) != 4 or any(a != {'value': ''} for a in arguments[1:]):
                raise BridgeError('SDK_LOCAL_FILES_ONLY')
            key = 'input' if member == 'Load' else 'output'
            if set(arguments[0]) != {key}:
                raise BridgeError('SDK_EXPLICIT_FILE_BINDING_REQUIRED')
            file_id = arguments[0][key]
            if member == 'Load':
                if file_id not in self.sources:
                    raise BridgeError('SDK_INPUT_NOT_FOUND')
                self.target_sources[step['target']] = file_id; self.used_inputs.add(file_id)
            else:
                output = self.destinations.get(file_id)
                if output is None or output['source'] != self.target_sources.get(step['target']) or file_id in self.saved_outputs:
                    raise BridgeError('SDK_OUTPUT_OWNERSHIP')
                self.saved_outputs.add(file_id)
            suffix = Path(self.sources[file_id if member == 'Load' else self.destinations[file_id]['source']]['file']).suffix.lower()
            required = {'IPntArea': '.wt', 'ILinArea': '.wl', 'IRegArea': '.wp'}[interface]
            if suffix != required or (member == 'Save' and Path(output['name']).suffix != required):
                raise BridgeError('SDK_WORKSPACE_FILE_TYPE')
        result = _shape(method['return']['type'])
        identity = self.add(step['id'], result)
        self.lines.append('\t'.join(('invoke', identity, _id(step['target']), _hex(member), str(method['member_id']), str(flags), str(len(arguments)), '-')))
        for p, arg in zip(params, arguments):
            shape = _shape(p['type']); vt = shape['vt']
            if not isinstance(arg, dict):
                raise BridgeError('SDK_TYPED_ARGUMENT_REQUIRED')
            if not p['flags'] & 2 and shape['depth'] and vt != 9:
                raise BridgeError('SDK_INPUT_POINTER_UNSUPPORTED')
            if p['flags'] & 2:
                if p['flags'] & 1 or set(arg) != {'out'}:
                    raise BridgeError('SDK_OUT_PARAMETER_REQUIRED')
                self.add(arg['out'], shape)
                self.lines.append('\t'.join(('out', str(vt), arg['out'])))
            elif set(arg) == {'ref'}:
                previous = self.symbols.get(arg['ref'])
                if arg['ref'] == identity or not previous or previous['vt'] != vt or (vt == 9 and previous.get('interface') != shape.get('interface')):
                    raise BridgeError('SDK_REFERENCE_TYPE')
                self.lines.append('ref\t'+_id(arg['ref']))
            elif set(arg) == {'value'}:
                self.lines.append('\t'.join(('value', str(vt), _scalar(vt, arg['value']))))
            elif member in {'Load', 'Save'} and (set(arg) == {'input'} or set(arg) == {'output'}):
                self.lines.append('path\t'+next(iter(arg))+'\t'+_id(next(iter(arg.values()))))
            else:
                raise BridgeError('SDK_ARGUMENT_BINDING')
        expected = True if member in {'Load', 'Save'} else step.get('expect', ...)
        if expected is not ...:
            self.lines.append('\t'.join(('expect', identity, str(result['vt']), _scalar(result['vt'], expected))))

    def control(self, input_paths, output_paths):
        lines = []
        for line in self.lines:
            if line.startswith('path\t'):
                _, kind, identity = line.split('\t')
                path = (input_paths if kind == 'input' else output_paths)[identity]
                line = 'value\t8\t'+_hex(str(path))
            lines.append(line)
        return '\n'.join(lines)+'\n'


def _preserve_saved_tables(original, candidate):
    """Whole-table reuse also retains sparse physical IDs and auxiliary fields."""
    source, view, sections = read_native(original)
    current = candidate.read_bytes()
    if len(current) < 817 or current[:8] != source[:8]:
        raise BridgeError('SDK_OUTPUT_NATIVE_TYPE')
    table = struct.unpack_from('<i', current, 12)[0]
    if table < 16 or table+160 > len(current):
        raise BridgeError('SDK_OUTPUT_SECTION_TABLE')
    entries = native._read_header_entries(io.BytesIO(current), table, 16)
    if any(o < 0 or n < 0 or o+n > len(current) for o, n in entries):
        raise BridgeError('SDK_OUTPUT_SECTION_RANGE')
    indexes = (2, 5, 9) if view.shapeType == 'POLYGON' else (2,)
    replacements = {i: source[sections[i][0]:sum(sections[i])] for i in indexes}
    restored = bytearray(_replace_sections(current, entries, replacements))
    restored[91:260] = source[91:260]
    candidate.write_bytes(restored)
    checked = read_native(candidate)[1]
    if native_ids(checked) != native_ids(view) or not checked.data.equals(view.data):
        raise BridgeError('SDK_OUTPUT_PHYSICAL_IDENTITIES_OR_ATTRIBUTES_CHANGED')
    if view.shapeType == 'POLYGON':
        old, new = _wp_arc_records(original)[3], _wp_arc_records(candidate)[3]
        if set(old) != set(new) or any(any(old[i][k] != new[i][k] for k in ('owner_region_ids', 'node_ids')) for i in old):
            raise BridgeError('SDK_OUTPUT_AUXILIARY_IDENTITIES_CHANGED')
        validate_wp_topology(candidate)
    return native_ids(view)


def prepare_sdk_flow(bridge: Bridge, parameters: dict, request_id: str):
    planner = Planner(bridge.root, parameters)
    executable, dll = backend_configuration(bridge.root)
    sources = []
    for entry in planner.inputs:
        path = bridge.owned_path(entry['file'])
        if file_sha(path) != entry['expected_sha256']:
            raise BridgeError('STALE_SOURCE')
        read_native(path)
        sources.append({'file': str(path), 'sha256': entry['expected_sha256']})
    media = project_media_dependencies([{'file': s['file']} for s in sources])
    for asset in media:
        bridge.owned_path(asset['file'])
        sources.append({'file': asset['file'], 'sha256': asset['sha256']})
    payload = {'operation': 'native.sdk_flow', 'protocol': 1, 'parameters': parameters}
    stage, receipt, fresh = begin_bundle(bridge, payload, request_id, sources, 'sdk-flow.json')
    if not fresh:
        return receipt
    try:
        runtime = private_com_runtime(dll.parent, stage/'runtime')
        files = stage/'files'; files.mkdir()
        working = stage/'working'; working.mkdir()
        input_paths = {}
        for index, entry in enumerate(planner.inputs):
            source = Path(sources[index]['file']); target = working/('input'+str(index)+source.suffix.lower())
            shutil.copyfile(source, target)
            if file_sha(target) != sources[index]['sha256']:
                raise BridgeError('STALE_SOURCE')
            input_paths[entry['id']] = target
        output_paths = {entry['id']: working/entry['name'] for entry in planner.outputs}
        copy_media_dependencies(media, working)
        with NativePathAlias(stage) as alias:
            short_inputs = {k: alias.path/v.relative_to(stage) for k, v in input_paths.items()}
            short_outputs = {k: alias.path/v.relative_to(stage) for k, v in output_paths.items()}
            control = stage/'flow.txt'
            control.write_text(planner.control(short_inputs, short_outputs), encoding='ascii', newline='\n')
            host, events = _run_child([str(executable), 'sdk', str(dll), str(alias.path/runtime.relative_to(stage)), str(alias.path/'flow.txt')], stage, 'sdk-native.json')
        values = []
        for event in events:
            if event.get('event') == 'sdk_value':
                value = {k: v for k, v in event.items() if k != 'event'}
                if 'utf16' in value:
                    value['value'] = bytes.fromhex(value.pop('utf16')).decode('utf-16le')
                values.append(value)
        outputs = []
        for entry in planner.outputs:
            candidate = output_paths[entry['id']]; original = input_paths[entry['source']]
            # Reuse the existing exact-build CopyList repair. BASCOM Save can
            # recalculate attributes and omit map-parameter tail fields.
            ids = _preserve_saved_tables(original, candidate)
            native = native_file_info(bridge.root, str(candidate), file_sha(candidate))
            target = files/entry['name']; shutil.copyfile(candidate, target)
            outputs.append({'id': entry['id'], 'name': entry['name'], 'sha256': file_sha(target),
                'source': entry['source'], 'physical_records': len(ids), 'native_reopen': native})
        if outputs:
            copy_media_dependencies(media, files)
        for source in sources:
            if file_sha(Path(source['file'])) != source['sha256']:
                raise BridgeError('STALE_SOURCE')
        report = {'schema': 'mapgis-agent/sdk-flow/1', 'policy': sdk_policy(), 'parameters': parameters,
            'sources': sources, 'outputs': outputs, 'values': values, 'host': host,
            'desktop_input_used': False, 'native_method_calls': len(planner.steps),
            'validation_scope': 'Executed listed methods only; saved geometry and readback are not verification of all MapGIS6.7 functions.'}
        (files/'sdk-flow.json').write_text(json_text(report), encoding='utf8')
        return finish_bundle(bridge, request_id, files, {'backend': 'original exact-build IDispatch; existing private x86 host',
            'native_method_calls': len(planner.steps), 'outputs': len(outputs), 'source_preserved': True,
            'desktop_input_used': False})
    except Exception as exc:
        fail_bundle(bridge, request_id, exc)
        raise
