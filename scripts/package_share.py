"""Build a relocatable Windows package from explicit source and runtime inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import sysconfig
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_SHA = '1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149'
BASE_BUNDLE_SHA = 'dd1766249ed6498a6bd127f34f053f2c4c2784e52e1068d06841e1eb532c7982'
SOURCE_SHA = {'ghostscript-10.08.0.tar.xz': 'c20492bc8ebb96c87fa2e52a0926e1cda8cde95d66145e018ac713fed5da38cf',
              'geos-3.14.1.tar.bz2': '3c20919cda9a505db07b5216baa980bacdaa0702da715b43f176fb07eff7e716'}


def sha(file: Path) -> str:
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ordinary_copy(source: Path, target: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f'Not an ordinary input: {source.name}')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    shutil.copyfile(source, target)


def public_sources(portable: bool = False) -> dict[str, Path]:
    """One explicit public source set shared by both archive builders."""
    selected = {name: ROOT/name for name in
                ('AGENTS.md', 'NOTICE.md', 'pyproject.toml', 'requirements.lock.txt', '.gitignore')}
    selected['README.md'] = ROOT/('docs/SHARE.md' if portable else 'README.GITHUB.md')
    selected['docs/NATIVE.md'] = ROOT/'docs/NATIVE-PORTABLE.md'
    for name in ('SHARE.md', 'THIRD-PARTY.md', 'CAPABILITIES.md', 'SOURCES.md', 'VALIDATION.md', 'AUTOMATION.md',
                 'LOCAL67-CALLING-CONTRACT.md', 'LOCAL67-ROUTES.md', 'PRIVATE-CALLS-PROPOSAL.md',
                 'SDK-CALLS.md', 'MULTIMEDIA.md', 'FULL-COVERAGE.md', 'DATABASES.md', 'VOLUME.md', 'RELEASE-0.7.0.md'):
        selected['docs/'+name] = ROOT/'docs'/name
    for folder in ('src', 'native', 'tests', 'vendor/mapgis2shp', 'vendor/geoscan',
                   'vendor/stratigraphic-amenity', 'vendor/cdt', 'third_party/winhlp', 'workflows', 'packaging'):
        for file in (ROOT/folder).rglob('*'):
            parts = file.relative_to(ROOT).parts
            if not file.is_file() or any(p in ('__pycache__', '.git', '.pytest_cache', 'archive') for p in parts):
                continue
            if file.suffix not in ('.pyc', '.pyo'):
                selected[file.relative_to(ROOT).as_posix()] = file
    for name in ('launch_mcp.py', 'mapgis_cli.py', 'native_support.py', 'native_session.mjs',
                 'native_workflows.mjs', 'palette_probe.py', 'package.py', 'package_share.py',
                 'index_native_help.py', 'Prepare-NativeOutput.ps1', 'Setup.ps1', 'portable.py', 'self_test.py', 'self_test_native.py',
                 'self_test_workspace.py', 'self_test_modules.py'):
        selected['scripts/'+name] = ROOT/'scripts'/name
    return selected


def copy_base_runtime(stage: Path, bundle: Path, expected_sha256: str | None = None) -> None:
    """Reuse a pinned accepted runtime, replacing its application dependencies."""
    if sha(bundle) != (expected_sha256 or BASE_BUNDLE_SHA):
        raise ValueError('Changed base bundle; the exact expected archive digest is required')
    source_names = {'third_party/sources/' + name for name in SOURCE_SHA}
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read('MapGIS-Agent/bundle-manifest.json'))
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith('MapGIS-Agent/'):
                continue
            name = info.filename.removeprefix('MapGIS-Agent/')
            if not (name.startswith(('runtime/python/', 'runtime/ghostscript/')) or name in source_names):
                continue
            target = (stage / name).resolve()
            if not target.is_relative_to(stage.resolve()) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unsafe base archive path')
            if name.startswith(('runtime/python/Lib/site-packages/', 'runtime/python/Scripts/')):
                continue
            expected = manifest[name]
            data = archive.read(info)
            if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise ValueError('Base payload mismatch: ' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
    for name, digest in SOURCE_SHA.items():
        if sha(stage / 'third_party/sources' / name) != digest:
            raise ValueError('Base third-party source mismatch: ' + name)
    if not (stage/'runtime/python/python.exe').is_file() or not (stage/'runtime/ghostscript/bin/gswin64c.exe').is_file():
        raise ValueError('Base runtime is incomplete')


def build(stage: Path, python_archive: Path | None, ghostscript: Path | None,
          source_archives: Path | None, base_bundle: Path | None = None,
          extra_sources: Path | None = None, extra_bundle: Path | None = None,
          backend_sources: Path | None = None, base_sha256: str | None = None,
          extra_bundle_sha256: str | None = None) -> dict:
    stage.mkdir(parents=True, exist_ok=False)
    if base_bundle:
        copy_base_runtime(stage, base_bundle, base_sha256 or BASE_BUNDLE_SHA)
    else:
        if not python_archive or not ghostscript or not source_archives:
            raise ValueError('Provide a pinned base bundle, or all original runtime inputs')
        if sha(python_archive) != PYTHON_SHA:
            raise ValueError('Python archive does not match pinned release digest')
        for name, digest in SOURCE_SHA.items():
            if sha(source_archives / name) != digest:
                raise ValueError('Third-party source archive mismatch: ' + name)
    # Retain the existing source layout; exclude machine evidence, course data,
    # configurations, journals, licence tools and reverse-engineering runtimes.
    for name, source in public_sources(portable=True).items():
        ordinary_copy(source, stage/name)
    runtime = stage / 'runtime'
    runtime.mkdir(exist_ok=True)
    if not base_bundle:
        copy_python_runtime(runtime, python_archive)
    (runtime / 'python/python312._pth').write_text(
        '.\nLib\nDLLs\nLib/site-packages\nimport site\n', encoding='ascii')
    site = Path(sysconfig.get_paths()['purelib'])
    destination = runtime / 'python/Lib/site-packages'
    excluded = ('_virtualenv.py', '_virtualenv.pth', 'mapgis_agent','pefile.py','peutils.py','ordlookup')
    for file in site.rglob('*'):
        if not file.is_file():
            continue
        relative = file.relative_to(site)
        first = relative.parts[0].lower()
        if first in excluded or first.startswith(('mapgis_local_agent-','pefile-')):
            continue
        if '__pycache__' in relative.parts or file.suffix in ('.pyc', '.pyo'):
            continue
        if file.name in ('direct_url.json', 'uv_cache.json', 'uv_cache', '.gitignore'):
            continue
        if file.suffix == '.pth' and file.name != 'pywin32.pth':
            raise ValueError('Unreviewed environment path hook: ' + file.name)
        ordinary_copy(file, destination / relative)
    if not base_bundle:
        for file in ghostscript.rglob('*'):
            if file.is_file() and file.suffix not in ('.lib', '.pyc'):
                ordinary_copy(file, runtime / 'ghostscript' / file.relative_to(ghostscript))
        for name in SOURCE_SHA:
            ordinary_copy(source_archives / name, stage / 'third_party/sources' / name)
    extra = json.loads((ROOT/'packaging/native-sources.json').read_text(encoding='utf8'))
    if extra_bundle:
        if sha(extra_bundle) != (extra_bundle_sha256 or 'a77776d1f8bb66343716548f45f3c919bdf6f618c6bf08679b305756b0644203'):
            raise ValueError('Changed extra-source bundle; the exact expected archive digest is required')
        with zipfile.ZipFile(extra_bundle) as previous:
            for item in extra:
                data=previous.read('MapGIS-Agent/third_party/sources/'+item['file'])
                if hashlib.sha256(data).hexdigest()!=item['sha256']:
                    raise ValueError('Extra source mismatch: '+item['file'])
                target=stage/'third_party/sources'/item['file']
                with target.open('xb') as stream:stream.write(data)
    else:
        if not extra_sources:
            raise ValueError('The current libvips runtime requires extra sources')
        for item in extra:
            source=extra_sources/item['file']
            if sha(source)!=item['sha256']:
                raise ValueError('Extra source mismatch: '+item['file'])
            ordinary_copy(source,stage/'third_party/sources'/item['file'])
    backend = json.loads((ROOT/'packaging/backend-sources.json').read_text(encoding='utf8'))
    if not backend_sources:raise ValueError('Provide --backend-sources with the pinned SciPy/ContourPy/NetworkX sources')
    for item in backend:
        source=backend_sources/item['file']
        if sha(source)!=item['sha256'] or source.stat().st_size!=item['bytes']:
            raise ValueError('Backend source mismatch: '+item['file'])
        ordinary_copy(source,stage/'third_party/sources'/item['file'])
    components = sorted([{'name': d.metadata['Name'], 'version': d.version}
                         for d in metadata.distributions()
                         if d.metadata['Name'].lower() not in ('mapgis-local-agent','pefile')],
                        key=lambda x: x['name'].lower())
    third_party = {'python': {'version': '3.12.16', 'release': 'python-build-standalone 20261026',
        'url': 'https://github.com/astral-sh/python-build-standalone/releases/tag/20261026', 'archive_sha256': PYTHON_SHA},
        'packages': components, 'ghostscript': {'version': '10.08.0', 'source_included': True},
        'geos': {'version': '3.14.1', 'source_included': True},
        'libvips': {'version': '8.18.7', 'source_included': True, 'build_recipes_and_native_dependencies': extra},
        'runtime_scope': 'MapGIS MCP tools (exact names verified on restored startup); isolated exact-build x86 native buffers, WP topology deletion, MSI/MPJ/EPS; CDT native TIN storage, SciPy/ContourPy/NetworkX open algorithms; Pyogrio/GDAL vector exchange and Rasterio/GDAL rasters, libvips and separate GPL-2.0 help indexing; analysis-only pefile excluded',
        'sources': SOURCE_SHA,
        'backend_sources': backend,
        'base_bundle_sha256': sha(base_bundle) if base_bundle else None,
        'licences': 'See docs/THIRD-PARTY.md, dependency dist-info/licenses and included source archives.'}
    (stage / 'THIRD-PARTY.json').write_text(json.dumps(third_party, ensure_ascii=False, indent=2), encoding='utf-8')
    commands = {'01-Setup.cmd': 'portable.py setup --interactive %*',
                '02-SelfTest.cmd': 'self_test.py %*', '03-Diagnostics.cmd': 'portable.py doctor %*',
                'MapGIS-CLI.cmd': 'mapgis_cli.py %*', 'MapGIS-MCP.cmd': 'launch_mcp.py %*'}
    for name, arguments in commands.items():
        pause = name[:2].isdigit()
        lines = ['@echo off', 'setlocal', 'chcp 65001 >nul',
                 'if /I "%MAPGIS_READ_ONLY_PROBE%"=="1" (',
                 '  echo READ_ONLY_LAUNCH_OK', '  echo %~dp0', '  exit /b 0', ')',
                 f'"%~dp0runtime\\python\\python.exe" -s -B -X utf8 "%~dp0scripts\\{arguments.split()[0]}" ' + ' '.join(arguments.split()[1:]),
                 'set "MAPGIS_TASK_EXIT=%ERRORLEVEL%"']
        if pause:
            lines += ['if not defined MAPGIS_NO_PAUSE pause']
        lines += ['exit /b %MAPGIS_TASK_EXIT%']
        (stage / name).write_bytes(('\r\n'.join(lines) + '\r\n').encode('utf-8'))
    manifest = {}
    for file in sorted(stage.rglob('*')):
        if file.is_file():
            manifest[file.relative_to(stage).as_posix()] = {'bytes': file.stat().st_size, 'sha256': sha(file)}
    (stage / 'bundle-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def copy_python_runtime(runtime: Path, python_archive: Path) -> None:
    with tarfile.open(python_archive) as archive:
        selected = []
        for member in archive.getmembers():
            target = (runtime / member.name).resolve()
            if not target.is_relative_to(runtime.resolve()) or not member.name.startswith('python/'):
                raise ValueError('Unsafe Python archive path')
            if not (member.isfile() or member.isdir()):
                raise ValueError('Python archive link is not accepted')
            # The application vendors tested dependencies below; it does not
            # include a second pip installation or developer command launchers.
            if member.name.startswith(('python/Lib/site-packages/', 'python/Scripts/')):
                continue
            if '/__pycache__/' in member.name or member.name.endswith(('.pyc', '.pyo')):
                continue
            selected.append(member)
        archive.extractall(runtime, members=selected, filter='data')



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path, required=True)
    runtime_input = parser.add_mutually_exclusive_group(required=True)
    runtime_input.add_argument('--python-archive', type=Path)
    runtime_input.add_argument('--base-bundle', type=Path)
    parser.add_argument('--base-sha256', help='Exact digest of an accepted current bundle when reusing its runtime')
    parser.add_argument('--ghostscript', type=Path)
    parser.add_argument('--source-archives', type=Path)
    extra_input = parser.add_mutually_exclusive_group(required=True)
    extra_input.add_argument('--extra-sources', type=Path)
    extra_input.add_argument('--extra-sources-bundle', type=Path)
    parser.add_argument('--extra-bundle-sha256', help='Exact digest when using an accepted current bundle for corresponding sources')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend-sources', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    manifest = build(args.stage.resolve(), args.python_archive, args.ghostscript,
                     args.source_archives, args.base_bundle, args.extra_sources, args.extra_sources_bundle,args.backend_sources,
                     args.base_sha256,args.extra_bundle_sha256)
    with zipfile.ZipFile(args.output, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(args.stage.rglob('*')):
            if file.is_file():
                archive.write(file, 'MapGIS-Agent/' + file.relative_to(args.stage).as_posix())
    receipt = {'archive': str(args.output.resolve()), 'archive_sha256': sha(args.output),
        'archive_bytes': args.output.stat().st_size, 'payload_files': len(manifest),
        'payload_bytes': sum(v['bytes'] for v in manifest.values()),
        'version': tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'],
        'status': 'BUILT_REQUIRES_RESTORE_AND_ISOLATED_TEST'}
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
