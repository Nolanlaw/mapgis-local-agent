"""External MapGIS ABI profiles and conservative install routing."""
from __future__ import annotations

from functools import cache
import json
from pathlib import Path
import re
import struct

from .core import BridgeError
from .object_store import file_sha256


PROFILE_DIRECTORY = Path(__file__).with_name('data') / 'abi-profiles'
DEFAULT_PROFILE = 'mapgis-6.7-051118-x86.json'
CORE_MODULES = ('mdiedit6x.exe', 'maped6x.dll', 'mapprj60.dll', 'bas60.dll')
ROUTES = {
    'A': ('EXACT_CERTIFIED', True, True, True),
    'B': ('CANDIDATE_GOLDEN_VERIFIED', False, True, True),
    'C': ('COM_UI_SAFE_MODE', False, False, True),
    'D': ('HEADLESS_OPEN_ENGINE', False, False, False),
}


def _load(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    modules = data.get('modules', {})
    if (data.get('schema_version') != 'mapgis-abi-profile/v1' or
            data.get('arch') != 'x86' or not isinstance(data.get('profile_id'), str) or
            set(modules) != set(CORE_MODULES) or
            not all(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) for value in modules.values())):
        raise BridgeError('NATIVE_WORKER_PROFILE: invalid external ABI profile')
    return data | {'modules': {name: modules[name] for name in CORE_MODULES}}


@cache
def default_profile():
    return _load(PROFILE_DIRECTORY / DEFAULT_PROFILE)


def _pe_x86(path, dll):
    try:
        with path.open('rb') as stream:
            head = stream.read(64)
            if len(head) != 64 or head[:2] != b'MZ':
                return False
            offset = struct.unpack_from('<I', head, 60)[0]
            stream.seek(offset)
            pe = stream.read(26)
            if len(pe) != 26 or pe[:4] != b'PE\0\0':
                return False
            machine, sections = struct.unpack_from('<HH', pe, 4)
            flags, optional_magic = struct.unpack_from('<HH', pe, 22)
            return machine == 0x14c and sections > 0 and optional_magic == 0x10b and bool(flags & 0x2000) == dll
    except (OSError, struct.error):
        return False


def _golden_passes(observed):
    if not isinstance(observed, dict) or not isinstance(observed.get('fixtures'), list) or not observed['fixtures']:
        return False
    return all(isinstance(row, dict) and all(type(row.get(k)) is int and row[k] >= 0
                   for k in ('expected_count', 'opened_count', 'resaved_count', 'reopened_count')) and
               len({row[k] for k in ('expected_count', 'opened_count', 'resaved_count', 'reopened_count')}) == 1 and
               row.get('opened') is True and row.get('resaved') is True and row.get('reopened') is True
               for row in observed['fixtures'])


def probe_installation(install_dir, profile_dir=None, *, golden_probe=None):
    """Inventory core PE modules. An optional callback supplies observed fixture receipts.

    Baseline callback signature: (program_directory, module_hashes, reference_profiles).
    A candidate earns only the validated file route, while live offsets stay exact-only.
    """
    given = Path(install_dir).resolve() if install_dir else None
    program = given / 'program' if given is not None and (given / 'program').is_dir() else given
    profiles = [default_profile()] if profile_dir is None else [
        _load(path) for path in sorted(Path(profile_dir).glob('*.json'))]
    hashes = {}
    if program is not None:
        for name in CORE_MODULES:
            path = program / name
            if path.is_file() and _pe_x86(path, name.endswith('.dll')):
                hashes[name] = file_sha256(path)
    exact = next((profile for profile in profiles if hashes == profile['modules']), None)
    golden = None
    if exact is not None and exact.get('certified') is True:
        tier = 'A'
    elif not {'mdiedit6x.exe', 'bas60.dll'}.intersection(hashes):
        tier = 'D'
    elif set(hashes) == set(CORE_MODULES) and golden_probe is not None:
        golden = golden_probe(program, dict(hashes), tuple(profile['profile_id'] for profile in profiles))
        tier = 'B' if _golden_passes(golden) else 'C'
    else:
        tier = 'C'
    route, memory, native, ui = ROUTES[tier]
    return {'tier': tier, 'route': route, 'profile_id': exact['profile_id'] if tier == 'A' else None,
            'modules': hashes, 'missing_or_invalid': [name for name in CORE_MODULES if name not in hashes],
            'capabilities': {'raw_memory_offsets': memory, 'native_saved_file': native,
                             'com_and_private_ui': ui, 'headless_open_engine': True},
            'golden_verified': tier == 'B'}
