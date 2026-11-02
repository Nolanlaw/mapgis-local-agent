"""Portable, hash-bound native multimedia references and their actual assets."""
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
import shutil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .artifacts import begin_bundle, finish_bundle, fail_bundle, _file_sha
from .attribute_codec import MEDIA_TYPES
from .attributes import _attribute_table, prepare_attribute_schema
from .core import Bridge, BridgeError, json_text, native_ids, read_native, sha

MediaType = Literal['multimedia_text', 'multimedia_image', 'multimedia_map', 'multimedia_audio', 'multimedia_video']


class MediaField(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    name: str
    type: MediaType


class MediaAsset(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
    file: str
    expected_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class MediaLink(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    record_id: StrictInt = Field(ge=1)
    field: str
    asset: str


class MultimediaParameters(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    fields: list[MediaField] = Field(min_length=1, max_length=100)
    assets: list[MediaAsset] = Field(max_length=10000)
    links: list[MediaLink] = Field(max_length=200000)
    native_resave: bool = False


def media_dependencies(file, decoded=None):
    """Freeze portable primary references without launching their targets.

    Nonempty legacy directory settings require an explicit multimedia rebuild.
    Relative references must stay inside the layer folder. A project may share
    the same relative asset name only when the bytes agree.
    """
    path = Path(file).resolve(strict=True)
    if path.suffix.lower() not in ('.wt', '.wl', '.wp'):
        return []
    blob, reader, sections = decoded if decoded is not None else read_native(path)
    fields = [f['name'] for f in reader._field_meta['fields'] if f['type_code'] in MEDIA_TYPES]
    if not fields:
        return []
    refs = sorted({value for name in fields for value in reader.data[name] if value})
    if not refs:
        return []
    start = sections[9 if reader.shapeType == 'POLYGON' else 2][0]
    if blob[start+24:start+280].strip(b'\0'):
        raise BridgeError('MULTIMEDIA_PORTABLE_DIRECTORY_REQUIRED: rebuild the complete media bundle with explicit assets')
    result = []
    for ref in refs:
        relative = PureWindowsPath(ref)
        if relative.drive or relative.root or '..' in relative.parts or any(':' in p for p in relative.parts):
            raise BridgeError('MULTIMEDIA_PORTABLE_REFERENCE_REQUIRED')
        asset = (path.parent/Path(*relative.parts)).resolve(strict=True)
        if not asset.is_relative_to(path.parent) or not asset.is_file():
            raise BridgeError('MULTIMEDIA_ASSET_OUTSIDE_BUNDLE')
        result.append({'file': str(asset), 'sha256': _file_sha(asset), 'bundle_name': '/'.join(relative.parts)})
    return result


def project_media_dependencies(layers):
    members = {}
    for layer in layers:
        for asset in media_dependencies(layer['file']):
            key = asset['bundle_name'].casefold()
            if key in members and members[key]['sha256'] != asset['sha256']:
                raise BridgeError('MULTIMEDIA_PROJECT_NAME_COLLISION: use unique asset IDs or identical shared bytes')
            members.setdefault(key, asset)
    return list(members.values())


def copy_media_dependencies(members, folder):
    for member in members:
        target = folder/member['bundle_name']
        if target.exists() and _file_sha(target) != member['sha256']:
            raise BridgeError('MULTIMEDIA_PROJECT_NAME_COLLISION')
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(member['file'], target)
        if _file_sha(target) != member['sha256']:
            raise BridgeError('STALE_SOURCE: project multimedia asset changed')


def prepare_multimedia(bridge, file, expected_sha256, parameters, request_id):
    """Full desired primary media schema/links on a new branch, including blanks.

    All ordinary fields remain. Every existing primary media field must be
    declared explicitly; omitted links become empty cells in this new branch.
    Auxiliary multimedia, embedded binary/table values and playback are outside
    this operation. Actual asset bytes are copied, never run or transcoded.
    """
    p = MultimediaParameters.model_validate(parameters)
    path = bridge.owned_path(file)
    before, reader, sections = read_native(path)
    if sha(before) != expected_sha256:
        raise BridgeError('STALE_SOURCE')
    identities = native_ids(reader)
    if not 1 <= len(identities) <= 200000:
        raise BridgeError('MULTIMEDIA_FEATURE_LIMIT')
    primary = 9 if reader.shapeType == 'POLYGON' else 2
    table = _attribute_table(before, sections, primary)
    existing = {f.name: f.type_code for f in table['fields']}
    names = [f.name for f in p.fields]
    if len(set(n.casefold() for n in names)) != len(names):
        raise BridgeError('MULTIMEDIA_DUPLICATE_FIELD')
    if any(existing.get(n, 9) not in MEDIA_TYPES for n in names):
        raise BridgeError('MULTIMEDIA_ORDINARY_FIELD_COLLISION')
    if not {n for n, code in existing.items() if code in MEDIA_TYPES} <= set(names):
        raise BridgeError('MULTIMEDIA_FULL_SCHEMA_REQUIRED: declare all existing primary media fields')
    if reader.shapeType == 'POLYGON':
        for index in (2, 5):
            auxiliary = _attribute_table(before, sections, index)
            if auxiliary and any(f.type_code in MEDIA_TYPES for f in auxiliary['fields']):
                raise BridgeError('MULTIMEDIA_AUXILIARY_REFERENCES_UNSUPPORTED')
    asset_paths, asset_names, sources = {}, {}, [{'file': str(path), 'sha256': expected_sha256}]
    asset_ids = set()
    size = 0
    for asset in p.assets:
        if asset.id.casefold() in asset_ids:
            raise BridgeError('MULTIMEDIA_DUPLICATE_ASSET')
        asset_ids.add(asset.id.casefold())
        asset_path = bridge.owned_path(asset.file)
        suffix = asset_path.suffix.lower()
        if not suffix or len(suffix) > 12 or not suffix[1:].isascii() or not suffix[1:].isalnum():
            raise BridgeError('MULTIMEDIA_ASSET_EXTENSION')
        if PureWindowsPath(asset.id + suffix).is_reserved():
            raise BridgeError('MULTIMEDIA_ASSET_ID: reserved Windows device name')
        size += asset_path.stat().st_size
        if size > 1024 * 1024 * 1024:
            raise BridgeError('MULTIMEDIA_ASSET_LIMIT: total 1 GiB')
        asset_paths[asset.id] = asset_path
        asset_names[asset.id] = 'media/' + asset.id + suffix
        sources.append({'file': str(asset_path), 'sha256': asset.expected_sha256})
    fields = [{'name': n, 'source': n} for n, code in existing.items() if code not in MEDIA_TYPES]
    fields += [{'name': f.name, 'type': f.type, 'default': ''} for f in p.fields]
    updates, used, seen = {}, set(), set()
    active = set(identities)
    for link in p.links:
        key = (link.record_id, link.field)
        if key in seen or link.record_id not in active or link.field not in names or link.asset not in asset_paths:
            raise BridgeError('MULTIMEDIA_LINK_IDENTITY')
        seen.add(key)
        used.add(link.asset)
        updates.setdefault(str(link.record_id), {})[link.field] = asset_names[link.asset]
    if used != set(asset_paths):
        raise BridgeError('MULTIMEDIA_UNUSED_ASSET: bundle only referenced assets')
    payload = {'operation': 'attributes.multimedia', 'protocol': 1, 'file': str(path),
               'expected_sha256': expected_sha256, 'parameters': p.model_dump()}
    stage, receipt, fresh = begin_bundle(bridge, payload, request_id, sources, path.name)
    if not fresh:
        return receipt
    try:
        encoder = Bridge(stage/'encoder')
        input_dir = encoder.root/'work/input'
        input_dir.mkdir(parents=True)
        snapshot = input_dir/path.name
        shutil.copyfile(path, snapshot)
        prepare_attribute_schema(encoder, str(snapshot), expected_sha256, fields, 'schema',
                                 attribute_updates=updates or None)
        encoded = Path(encoder.apply('schema')['output'])
        raw, view, layout = read_native(encoded)
        portable = bytearray(raw)
        # The original SetMMDPath writes two 128-byte directory slots at 24
        # and 152 in each signed 320-byte table prefix. Empty means the native
        # working/file directory; references contain the copied media/ path.
        changed_prefixes = []
        for index in ((2, 5, 9) if view.shapeType == 'POLYGON' else (2,)):
            a, n = layout[index]
            if n >= 348 and raw[a:a+2] == b'\x60\x44' and raw[a+320:a+322] == b'\x60\x44':
                portable[a+24:a+280] = b'\0'*256
                changed_prefixes.append(index)
        portable_dir = encoder.root/'work/portable'
        portable_dir.mkdir()
        encoded = portable_dir/path.name
        encoded.write_bytes(portable)
        native_evidence = None
        if p.native_resave:
            from .native_backend import prepare_native_operation
            config = bridge.root/'config.local.json'
            if config.is_file():
                settings = json.loads(config.read_text(encoding='utf8'))
                (encoder.root/'config.local.json').write_text(json_text({k: settings[k] for k in
                    ('mapgis_installation', 'native_backend_legacy_dep_compatibility') if k in settings}), encoding='utf8')
            native_evidence = prepare_native_operation(encoder, 'resave', str(encoded), sha(portable), {}, 'native')
            encoded = Path(encoder.apply('native')['output'])
        files = stage/'files'
        files.mkdir()
        target = files/path.name
        shutil.copyfile(encoded, target)
        members = []
        for asset in p.assets:
            destination = files/asset_names[asset.id]
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(asset_paths[asset.id], destination)
            if _file_sha(destination) != asset.expected_sha256:
                raise BridgeError('STALE_SOURCE: copied media differs')
            members.append({'id': asset.id, 'name': asset_names[asset.id], 'sha256': asset.expected_sha256,
                            'original_file': str(asset_paths[asset.id])})
        checked = read_native(target)[1]
        if native_ids(checked) != identities:
            raise BridgeError('MULTIMEDIA_READBACK_IDENTITY')
        for row, rid in enumerate(identities):
            for field in p.fields:
                expected = updates.get(str(rid), {}).get(field.name, '')
                if checked.data[field.name].iloc[row] != expected:
                    raise BridgeError('MULTIMEDIA_READBACK_VALUE')
                if expected and not (files/expected).is_file():
                    raise BridgeError('MULTIMEDIA_MISSING_BUNDLE_MEMBER')
        report = {'schema': 'mapgis-agent/multimedia/1', 'source': sources[0], 'parameters': p.model_dump(),
                  'assets': members, 'media_directory': '', 'normalized_table_prefixes': changed_prefixes,
                  'links': [l.model_dump() for l in p.links], 'source_preserved': True,
                  'blank_semantics': 'Full desired media links; absent link is an empty cell in the new branch.',
                  'native_resave': native_evidence, 'viewer_execution': 'NOT_VERIFIED',
                  'viewer_scope': 'Native field codes/references and portable file bytes; no viewer/player or codec execution.'}
        (files/'multimedia-lineage.json').write_text(json_text(report), encoding='utf8')
        return finish_bundle(bridge, request_id, files, {'backend': 'exact original multimedia field ABI; existing schema encoder and atomic nested bundle',
            'source_preserved': True, 'physical_records': len(identities), 'assets': len(members), 'links': len(p.links),
            'media_directory': '', 'native_resave_verified': p.native_resave, 'viewer_execution': 'NOT_VERIFIED'})
    except Exception as exc:
        fail_bundle(bridge, request_id, exc)
        raise
