"""Export redacted MapGIS checkpoint and recomputation evidence bundles."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
import hashlib
from importlib.metadata import PackageNotFoundError, version
import io
import json
import math
import os
from pathlib import Path
import platform
import re
from secrets import randbelow
import sqlite3
import struct
import tempfile
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from .core import Bridge, BridgeError, json_text


_VERSION = 'mapgis-fdr/v1'
_MODES = {
    'checkpoint_recovery': 'A redacted index for the local checkpoint. Resume requires the original state and verified CAS.',
    'deterministic_recompute': 'A redacted plan and ordered input fingerprints. Recompute requires matching source bytes, backend and coordinate context.',
}
_JSON = {'environment.json', 'plan_state.json', 'drc_report.json', 'inputs.json'}
_DEFAULT_SENSITIVE = frozenset({'api_key', 'authorization', 'cookie', 'credential', 'password', 'secret',
                                'token', 'access_token', 'refresh_token'})
_PATH = re.compile(r'(?i)(?:\b[A-Z]:[\\/]|[\\/]{2}[^\\/\s]+[\\/]|file://|(?<![\w:/])/(?![/\s]))')
_NUMBER = r'[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?'
_TEXT_COORDINATES = re.compile(rf'[\[(]\s*{_NUMBER}(?:\s+|\s*,\s*){_NUMBER}\s*[\])]')
_XY = {'coords', 'coordinates', 'centroid', 'center', 'anchor', 'xy', 'location'}
_BOUNDS = {'bounds', 'bbox', 'spatial_bounds', 'refresh_region', 'native_bounds', 'native_bbox'}
_PREVIEW = re.compile(r'^previews/preview-\d{3}\.png$')
_MASKED = '[REDACTED]'


@dataclass(frozen=True)
class RedactionPolicy:
    hide_paths: bool = True
    translate_coordinates: bool = True
    coordinate_offset: tuple[float, float] | None = None
    sensitive_fields: tuple[str, ...] = ()
    previews_are_redacted: bool = False

    def __post_init__(self):
        if any(type(value) is not bool for value in (self.hide_paths, self.translate_coordinates, self.previews_are_redacted)):
            raise BridgeError('FDR_REDACTION: policy switches must be boolean')
        if isinstance(self.sensitive_fields, str) or any(not isinstance(field, str) or not field.strip()
                                                         for field in self.sensitive_fields):
            raise BridgeError('FDR_REDACTION: sensitive_fields must name explicit fields')
        offset = self.coordinate_offset
        if offset is not None and (not self.translate_coordinates or len(offset) != 2 or
                                   not all(_numeric(v) for v in offset) or tuple(offset) == (0, 0)):
            raise BridgeError('FDR_REDACTION: use a finite nonzero coordinate translation')

    @property
    def fields(self):
        return _DEFAULT_SENSITIVE | {field.casefold() for field in self.sensitive_fields}


def _numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def _is_path(value):
    public = re.sub(r'https?://[A-Za-z0-9.-]+(?::\d+)?(?:[/#?][^\s"<>]*)?', '', value)
    return bool(_PATH.search(public))


class _Redactor:
    def __init__(self, policy: RedactionPolicy):
        self.policy = policy
        self.offset = policy.coordinate_offset
        self.path_ids = {}
        self.translated = 0

    def _path(self, value):
        if self.policy.hide_paths and _is_path(value):
            identity = value.replace('\\', '/').casefold() if os.name == 'nt' else value
            return self.path_ids.setdefault(identity, f'[PATH_{len(self.path_ids)+1:04}]')
        return value

    def _translate(self, values, box=False):
        if not self.policy.translate_coordinates:
            return list(values)
        if self.offset is None:
            self.offset = tuple(round(values[i], 2) + (randbelow(1_000_000) + 100_000) / 100
                                for i in range(2))
            if self.offset == (0, 0):
                self.offset = (377., 617.)
        count = 4 if box else 2
        self.translated += 1
        return [float(Decimal(str(value)) - Decimal(str(self.offset[i % 2]))) if i < count else value
                for i, value in enumerate(values)]

    def redact(self, value, field=''):
        if field in self.policy.fields:
            return _MASKED
        if isinstance(value, str):
            value = self._path(value)
            if self.policy.translate_coordinates:
                value = _TEXT_COORDINATES.sub('[COORDINATES_REDACTED]', value)
            return value
        if isinstance(value, dict):
            result = {self._path(key): self.redact(item, key.casefold()) for key, item in value.items()}
            pairs = {key.casefold(): key for key in value if key.casefold() in {'x', 'y'}}
            if (len(pairs) == 2 and not set(pairs).intersection(self.policy.fields)
                    and all(_numeric(value[pairs[key]]) for key in ('x', 'y'))):
                x, y = pairs['x'], pairs['y']
                result[x], result[y] = self._translate((value[x], value[y]))
            return result
        if isinstance(value, (list, tuple)):
            if field in _BOUNDS and len(value) == 4 and all(_numeric(v) for v in value):
                return self._translate(value, box=True)
            if field in _XY and 2 <= len(value) <= 4 and all(_numeric(v) for v in value):
                return self._translate(value)
            return [self.redact(item, field) for item in value]
        return value


def _json_bytes(value):
    return (json_text(value) + '\n').encode('utf-8')


def _environment():
    packages = {}
    for name in ('mapgis-local-agent', 'mapgis2shp', 'mcp', 'pydantic', 'numpy', 'shapely', 'pyproj', 'pyvips'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {'python': platform.python_version(), 'system': platform.system(), 'architecture': platform.machine(),
            'packages': packages}


def _request_events(root, identities):
    if not identities:
        return []
    identities = list(dict.fromkeys(Bridge.request_id(value) for value in identities))
    path = root / 'evidence/commands.sqlite3'
    if not path.is_file():
        raise BridgeError('FDR_REQUEST_MISSING: no command ledger in the project')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        events = {}
        for request in identities:
            row = db.execute("SELECT state, json_extract(receipt, '$.kind'), "
                             "json_extract(receipt, '$.candidate_sha256') FROM commands WHERE id=?", (request,)).fetchone()
            if row is None:
                raise BridgeError('FDR_REQUEST_MISSING: unknown selected request')
            events[request] = {'event': 'command_checkpoint', 'request_id': request, 'state': row[0],
                               'kind': row[1], 'sha256': row[2]}
        return list(events.values())


def _read_plan(root, plan_id):
    if plan_id is None:
        return None
    from .map_plan import MapPlanEngine, _digest
    folder = MapPlanEngine(root)._folder(plan_id)
    path = folder / 'state.json'
    if not path.is_file():
        raise BridgeError('PLAN_MISSING')
    state = json.loads(path.read_text(encoding='utf-8'))
    if state['plan_id'] != plan_id or _digest(state['spec']) != state['spec_sha256']:
        raise BridgeError('PLAN_CHECKPOINT_CORRUPT')
    return state


def _plan_members(state, mode):
    if state is None:
        return None, [], []
    common = {key: state[key] for key in ('plan_id', 'schema_version', 'backend_version', 'spec_sha256')}
    inputs, events = [], []
    spec_nodes = {node['id']: node for node in state['spec']['nodes']}
    outputs = state['completed']
    for node_id in state['topological_order']:
        entry = outputs.get(node_id)
        if entry is not None:
            events.append({'event': 'node_checkpoint', 'node': node_id, 'cache_key': entry['cache_key'],
                           'output_sha256': {k: v['sha256'] for k, v in entry['outputs'].items()}})
        for index, item in enumerate(spec_nodes[node_id]['inputs']):
            source = state['sources'].get(item['source_file']) if item['source_file'] else None
            if item['from_node'] in outputs:
                source = outputs[item['from_node']]['outputs'].get(item['output_key'])
            inputs.append({'node': node_id, 'index': index, **item,
                           'sha256': source['sha256'] if source else item['expected_sha256']})
    if state.get('interruption'):
        events.append({'event': 'plan_interrupted', **state['interruption']})
    if mode == 'checkpoint_recovery':
        redacted_completed = {
            node_id: {
                'cache_key': entry['cache_key'],
                'outputs': {
                    out_key: {
                        'name': f'{out_key}{Path(meta["name"]).suffix}' if isinstance(meta.get('name'), str) else out_key,
                        'sha256': meta['sha256'],
                        'bytes': meta['bytes'],
                    }
                    for out_key, meta in entry.get('outputs', {}).items()
                },
                'paths': entry.get('paths', {}),
            }
            for node_id, entry in state['completed'].items()
        }
        return (common | {key: state[key] for key in ('status', 'publisher_epoch', 'topological_order', 'interruption')}
                | {'completed': redacted_completed}), inputs, events
    return common | {'spec': state['spec'], 'topological_order': state['topological_order'],
                     'required_backends_and_inputs': True}, inputs, events


def _preview_png(path):
    from .visual_preview import _vips
    try:
        image = _vips().Image.new_from_buffer(Path(path).read_bytes(), '')
        # A fresh pixel-only image removes comments, EXIF, ICC and custom chunks.
        raster = _vips().Image.new_from_memory(image.write_to_memory(), image.width, image.height, image.bands, image.format)
        return raster.copy(interpretation=image.interpretation, xres=image.xres, yres=image.yres).pngsave_buffer(strip=True)
    except _vips().Error as exc:
        raise BridgeError('FDR_PREVIEW: a readable PNG is required') from exc


def export_fdr_bundle(root, output_zip, *, mode='checkpoint_recovery', plan_id=None, request_ids=None,
                      drc_report=None, preview_paths=None, redaction=None):
    """Write a portable evidence ZIP. Raw source vectors and native logs stay local."""
    if mode not in _MODES:
        raise BridgeError('FDR_MODE: choose checkpoint_recovery or deterministic_recompute')
    if mode == 'deterministic_recompute' and not plan_id:
        raise BridgeError('FDR_MODE: deterministic_recompute requires a saved plan')
    root, path = Path(root).resolve(), Path(output_zip).resolve()
    policy = RedactionPolicy(**redaction) if isinstance(redaction, dict) else (redaction or RedactionPolicy())
    redactor = _Redactor(policy)
    state, inputs, events = _plan_members(_read_plan(root, plan_id), mode)
    events.extend(_request_events(root, request_ids))
    if isinstance(drc_report, (str, Path)):
        drc_report = json.loads(Path(drc_report).read_text(encoding='utf-8'))
    data = {'environment.json': _environment(), 'plan_state.json': state, 'drc_report.json': drc_report,
            'inputs.json': {'input_bindings': inputs, 'topology': (drc_report or {}).get('files', [])}}
    members = {name: _json_bytes(redactor.redact(value)) for name, value in data.items()}
    members['events.jsonl'] = b''.join(_json_bytes(redactor.redact(event)) for event in events)
    paths = list(preview_paths or [])
    if paths and policy.previews_are_redacted:
        if len(paths) > 999:
            raise BridgeError('FDR_PREVIEW: at most 999 preview files')
        members.update({f'previews/preview-{i:03}.png': _preview_png(file) for i, file in enumerate(paths, 1)})
    manifest = {
        'schema_version': _VERSION, 'mode': mode, 'reproducibility': _MODES[mode],
        'redaction': {'hide_paths': policy.hide_paths, 'path_placeholders': len(redactor.path_ids),
                      'translate_coordinates': policy.translate_coordinates, 'translated_groups': redactor.translated,
                      'masked_fields': sorted(policy.fields), 'preview_pixels_attested': policy.previews_are_redacted,
                      'omitted_unattested_previews': len(paths) if not policy.previews_are_redacted else 0},
        'members': {name: {'sha256': hashlib.sha256(value).hexdigest(), 'bytes': len(value)}
                    for name, value in members.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.fdr-', suffix='.zip', dir=path.parent)
    os.close(fd)
    temporary = Path(temp)
    try:
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as bundle:
            for name, content in {'manifest.json': _json_bytes(manifest), **members}.items():
                bundle.writestr(name, content)
        result = verify_fdr_bundle(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return result | {'path': str(path), 'manifest': manifest}


def _assert_redacted(value, rules):
    if isinstance(value, str) and rules['hide_paths'] and _is_path(value):
        raise BridgeError('FDR_LEAK: absolute path in a managed member')
    if isinstance(value, str) and rules['translate_coordinates'] and _TEXT_COORDINATES.search(value):
        raise BridgeError('FDR_LEAK: unmasked coordinate text')
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_redacted(key, rules)
            if key.casefold() in rules['masked_fields'] and item != _MASKED:
                raise BridgeError('FDR_LEAK: sensitive attribute value is not masked')
            _assert_redacted(item, rules)
    elif isinstance(value, list):
        for item in value:
            _assert_redacted(item, rules)


def _check_png(raw):
    if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    start = 8
    allowed = {b'IHDR', b'PLTE', b'IDAT', b'IEND', b'pHYs', b'tRNS', b'bKGD', b'sRGB', b'gAMA', b'cHRM'}
    while start + 12 <= len(raw):
        size, kind = struct.unpack_from('>I4s', raw, start)
        start += 12 + size
        if kind not in allowed or start > len(raw):
            return False
        if kind == b'IEND':
            return size == 0 and start == len(raw)
    return False


def verify_fdr_bundle(zip_path):
    """Validate mode, member shape, digests, and declared structured redaction."""
    try:
        with ZipFile(zip_path) as bundle:
            names = bundle.namelist()
            required = _JSON | {'manifest.json', 'events.jsonl'}
            if len(set(names)) != len(names) or not required <= set(names):
                raise BridgeError('FDR_INTEGRITY: missing or duplicate members')
            if any(name not in required and not _PREVIEW.fullmatch(name) for name in names):
                raise BridgeError('FDR_INTEGRITY: unexpected archive member')
            if any(info.file_size > 100_000_000 for info in bundle.infolist()):
                raise BridgeError('FDR_INTEGRITY: member byte limit exceeded')
            manifest = json.loads(bundle.read('manifest.json'))
            mode, rules = manifest['mode'], manifest['redaction']
            files = manifest['members']
            if (manifest['schema_version'] != _VERSION or mode not in _MODES or
                    manifest['reproducibility'] != _MODES[mode] or set(files) != set(names) - {'manifest.json'} or
                    type(rules['hide_paths']) is not bool or type(rules['translate_coordinates']) is not bool or
                    type(rules['preview_pixels_attested']) is not bool or
                    not _DEFAULT_SENSITIVE <= set(rules['masked_fields']) or
                    _is_path(json.dumps(manifest))):
                raise BridgeError('FDR_INTEGRITY: invalid manifest contract')
            for name, item in files.items():
                raw = bundle.read(name)
                if hashlib.sha256(raw).hexdigest() != item['sha256'] or len(raw) != item['bytes']:
                    raise BridgeError('FDR_INTEGRITY: member fingerprint mismatch')
                if name in _JSON:
                    parsed = json.loads(raw)
                    _assert_redacted(parsed, rules)
                    if name == 'plan_state.json':
                        if mode == 'checkpoint_recovery' and parsed is not None and (
                                'spec' in parsed or not {'publisher_epoch', 'completed', 'status'} <= parsed.keys()):
                            raise BridgeError('FDR_MODE: invalid checkpoint state')
                        if mode == 'deterministic_recompute' and (
                                not isinstance(parsed, dict) or 'completed' in parsed or
                                not {'spec', 'required_backends_and_inputs'} <= parsed.keys()):
                            raise BridgeError('FDR_MODE: invalid recomputation state')
                elif name == 'events.jsonl':
                    for line in io.BytesIO(raw):
                        _assert_redacted(json.loads(line), rules)
                elif not rules['preview_pixels_attested'] or not _check_png(raw):
                    raise BridgeError('FDR_LEAK: preview does not match its redaction contract')
    except (BadZipFile, OSError, UnicodeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        if isinstance(exc, BridgeError):
            raise
        raise BridgeError('FDR_INTEGRITY: invalid ZIP, JSON, or manifest') from exc
    return {'verified': True, 'mode': mode, 'member_count': len(names),
            'structured_redaction_checked': True, 'preview_pixels_attested': rules['preview_pixels_attested']}
