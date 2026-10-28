"""Offline, package-local setup and diagnostics. No global configuration writes."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mapgis_agent.native_state import PROFILE


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}


def write_json(path: Path, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(data)
    os.replace(temporary, path)


def installation(value: str) -> Path:
    path = Path(value.strip().strip('"')).expanduser().resolve()
    if path.name.lower() == 'mdiedit6x.exe':
        path = path.parent.parent
    elif path.name.lower() == 'program':
        path = path.parent
    if not (path / 'program/mdiedit6x.exe').is_file():
        raise ValueError('MapGIS path must contain program/mdiedit6x.exe')
    return path


def configure(root: Path, python: Path, mapgis: str | None = None, native_legacy: bool = False) -> dict:
    """Generate only our local client config; preserve independently edited files."""
    config_file, state_file = root / 'mcp-config.local.json', root / 'setup-state.local.json'
    state = load(state_file)
    if config_file.exists():
        actual = hashlib.sha256(config_file.read_bytes()).hexdigest()
        if actual != state.get('generated_mcp_sha256'):
            raise ValueError('mcp-config.local.json was independently edited; preserve it and resolve before setup')
    native_file = root / 'config.local.json'
    native = load(native_file)
    chosen = installation(mapgis) if mapgis else None
    if chosen and native.get('mapgis_installation'):
        if Path(native['mapgis_installation']).resolve() != chosen:
            raise ValueError('Existing MapGIS binding points elsewhere; preserve it and resolve before rebinding')
    entry = {'mcpServers': {'mapgis_local': {'command': str(python.resolve()),
        'args': ['-s', '-B', '-X', 'utf8', str(root / 'scripts/launch_mcp.py'), '--root', str(root)]}}}
    if native_legacy and not (chosen or native.get('mapgis_installation')):
        raise ValueError('An existing licensed MapGIS installation is required for the legacy native host')
    if chosen:
        native['mapgis_installation'] = str(chosen)
    if native_legacy:
        native['native_backend_legacy_dep_compatibility'] = True
    if chosen or native_legacy:
        write_json(native_file, native)
    write_json(config_file, entry)
    write_json(state_file, {'generated_mcp_sha256': hashlib.sha256(config_file.read_bytes()).hexdigest(),
        'root': str(root), 'runtime': str(python.resolve())})
    return {'status': 'CONFIGURED', 'client_configuration': str(config_file),
        'mapgis': str(chosen) if chosen else native.get('mapgis_installation'),
        'native_legacy_host_enabled': native.get('native_backend_legacy_dep_compatibility') is True,
        'next': 'Open this package folder in a computer-use-capable agent host; or load the generated stdio config in your MCP client.'}


def profile(root: Path) -> dict:
    value = load(root / 'config.local.json').get('mapgis_installation')
    if not value:
        return {'status': 'NOT_CONFIGURED', 'next': 'Run setup --mapgis with your existing MapGIS installation path'}
    folder = Path(value) / 'program'
    checks = []
    for name, expected in PROFILE.items():
        binary = folder / name
        actual = hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else None
        checks.append({'module': name, 'matches': actual == expected, 'sha256': actual})
    return {'status': 'MATCH' if all(x['matches'] for x in checks) else 'UNSUPPORTED_BUILD',
        'installation': value, 'modules': checks,
        'licence': 'Confirm in the installed MapGIS application',
        'desktop_host': 'Host-provided Sky required for native UI execution'}


def doctor(root: Path) -> dict:
    imports = {}
    for name in ('numpy', 'pandas', 'geopandas', 'shapely', 'pyproj', 'psutil', 'mcp', 'pymapgis',
                 'pyvips', 'cv2', 'rasterio', 'mapgis_agent'):
        module = importlib.import_module(name)
        imports[name] = str(Path(module.__file__).resolve())
    gs = root / 'runtime/ghostscript/bin/gswin64c.exe'
    gs_result = None
    if gs.is_file():
        completed = subprocess.run([str(gs), '-version'], capture_output=True, text=True, timeout=15)
        if completed.returncode:
            raise RuntimeError('Bundled Ghostscript failed: ' + completed.stderr)
        gs_result = completed.stdout.splitlines()[0]
    return {'status': 'FILE_TOOLS_READY',
        'version': tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version'],
        'python': sys.version, 'executable': sys.executable, 'bits': struct.calcsize('P') * 8,
        'imports': imports, 'sys_path': sys.path,
        'runtime_flags': {'isolated': sys.flags.isolated, 'no_user_site': sys.flags.no_user_site,
                          'ignore_environment': sys.flags.ignore_environment},
        'native': profile(root),
        'pdf_converter': gs_result, 'global_configuration_modified': False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('setup', 'doctor'))
    parser.add_argument('--mapgis')
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--enable-native-legacy-host', action='store_true',
                        help='Accept DEP compatibility for this package-owned x86 child only; Windows and MapGIS binaries stay unchanged')
    args = parser.parse_args()
    try:
        if args.action == 'setup':
            selected = args.mapgis
            if args.interactive and not selected and not (ROOT / 'config.local.json').exists():
                print('MapGIS 6.7 installation folder (Enter = file tools only):', flush=True)
                selected = input().strip() or None
            result = configure(ROOT, Path(sys.executable), selected, args.enable_native_legacy_host)
            result['diagnostics'] = doctor(ROOT)
            if result['mapgis']:
                completed = subprocess.run([sys.executable, '-s', '-B', '-X', 'utf8',
                    str(ROOT / 'scripts/index_native_help.py')], capture_output=True, text=True, encoding='utf8')
                result['native_help_index'] = {'exit_code': completed.returncode,
                    'results': completed.stdout.strip(), 'error': completed.stderr.strip()}
        else:
            result = doctor(ROOT)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, ImportError, RuntimeError) as exc:
        print(json.dumps({'status': 'ERROR', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
