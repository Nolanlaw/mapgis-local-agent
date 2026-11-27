"""Inspection of the configured MapGIS 6.7 ABI, COM and private-UI routes."""
import json
from pathlib import Path
import sys

from ..abi_profiles import probe_installation
from ..core import BridgeError
from ..native_backend import backend_configuration
from ..object_store import file_sha256
from .base import MapGISProvider, operation_backend


_NATIVE_BACKENDS = frozenset({
    'mapgis_native', 'native_x86', 'mapgis_owned_private_ui', 'mapgis_native_private_ui',
})


def _profile_check(program, files):
    matches = {}
    for name, expected in files.items():
        path = program / name if program else None
        try:
            matches[name] = bool(path and path.is_file() and file_sha256(path) == expected)
        except OSError:
            matches[name] = False
    return matches


class MapGIS67Provider(MapGISProvider):
    provider_id = 'mapgis67'
    generation = '6.7/x86'

    def probe(self, root):
        from ..private_control import _configuration, _profile

        root = Path(root)
        config = _configuration(root)
        installation = config.get('mapgis_installation')
        program = Path(installation) / 'program' if installation else None
        abi = probe_installation(program)
        native_enabled = config.get('native_backend_legacy_dep_compatibility') is True
        native_status = 'NOT_CONFIGURED' if not installation else 'NOT_PROBED'
        native_ready = False
        if installation:
            try:
                backend_configuration(root)
            except (BridgeError, OSError) as exc:
                native_status = str(exc).split(':', 1)[0] if isinstance(exc, BridgeError) else 'NATIVE_RUNTIME'
            else:
                native_ready = abi['capabilities']['native_saved_file'] and sys.platform == 'win32'
                native_status = 'READY_FOR_ISOLATED_CALL' if native_ready else 'ABI_NOT_CERTIFIED'

        com_profile = Path(__file__).resolve().parents[1] / 'data' / 'com67-profile.json'
        com_items = json.loads(com_profile.read_text(encoding='utf8'))['files']
        libraries = _profile_check(program, {item['name']: item['sha256'] for item in com_items})
        modules = _profile()['modules']
        ui_files = _profile_check(program, {item['executable']: item['sha256'] for item in modules.values()})
        ui_enabled = config.get('native_general_private_ui') is True
        private_ready = sys.platform == 'win32' and ui_enabled and abi['capabilities']['com_and_private_ui']
        private_modules = {key: private_ready and ui_files[item['executable']] for key, item in modules.items()}
        com_ready = native_ready and all(libraries.values())
        available = native_ready or com_ready or any(private_modules.values())
        return {
            'available': available, 'installed': abi['tier'] != 'D',
            'status': 'CONFIGURED' if available else native_status,
            'tier': abi['tier'],
            'capabilities': {
                'abi': abi,
                'native_saved_file': {'available': native_ready, 'configuration_enabled': native_enabled,
                                      'status': native_status},
                'com': {'available': com_ready, 'exact_library_matches': libraries,
                        'activation': 'CHECKED_PER_CALL'},
                'private_ui': {'available': any(private_modules.values()), 'configuration_enabled': ui_enabled,
                               'exact_executable_matches': ui_files, 'available_modules': private_modules,
                               'desktop': 'NEW_OWNED_PRIVATE_DESKTOP', 'activation': 'CHECKED_PER_OPEN'},
                'raw_memory_offsets': abi['capabilities']['raw_memory_offsets'],
            },
        }

    def supports_operation(self, op_id):
        route = operation_backend(op_id)
        return route.get('backend') in _NATIVE_BACKENDS or route.get('kind') == 'host_sky'
