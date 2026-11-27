"""Build a relocatable Windows package from explicit source and runtime inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import sys
import sysconfig
import tarfile
from tempfile import TemporaryDirectory
import tomllib
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mapgis_agent.sbom import CORE_EXCLUDES, generate_sbom

PYTHON_SHA = '1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149'
BASE_BUNDLE_SHA = 'dd1766249ed6498a6bd127f34f053f2c4c2784e52e1068d06841e1eb532c7982'
SOURCE_SHA = {'ghostscript-10.08.0.tar.xz': 'c20492bc8ebb96c87fa2e52a0926e1cda8cde95d66145e018ac713fed5da38cf',
              'geos-3.14.1.tar.bz2': '3c20919cda9a505db07b5216baa980bacdaa0702da715b43f176fb07eff7e716'}
COMPANION_NAME = 'OpenSource-Compliance-Sources.zip'
_MCPB_ICON = ('<svg xmlns="http://www.w3.org/2000/svg" height="24" viewBox="0 0 24 24" width="24">'
              '<path d="M0 0h24v24H0z" fill="none"/>'
              '<path d="M20.5 3l-.16.03L15 5.1 9 3 3.36 4.9c-.21.07-.36.25-.36.48V20.5c0 .28.22.5.5.5l.16-.03L9 18.9l6 2.1 5.64-1.9c.21-.07.36-.25.36-.48V3.5c0-.28-.22-.5-.5-.5zM15 19l-6-2.11V5l6 2.11V19z"/>'
              '</svg>\n')
_EXCLUDED_DIRS = {'__pycache__', '.git', '.pytest_cache', 'archive', 'work', 'evidence', '.venv', 'deliverables',
                  'ida', 'idalib', 'course', 'course-data'}
_EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.i64', '.idb', '.id0', '.id1', '.id2', '.nam', '.til', '.pdb', '.obj'}


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
                ('AGENTS.md', 'LICENSE', 'NOTICE.md', 'SECURITY.md', 'server.json', 'pyproject.toml',
                 'requirements.lock.txt', '.gitignore', '.gitattributes', 'README.zh-CN.md', '.github/workflows/ci.yml')}
    selected['README.md'] = ROOT/('docs/SHARE.md' if portable else 'README.GITHUB.md')
    selected['docs/NATIVE.md'] = ROOT/'docs/NATIVE-PORTABLE.md'
    showcase = ROOT/'docs/assets/v3-showcase.png'
    if showcase.is_file():
        selected['docs/assets/v3-showcase.png'] = showcase
    for name in ('SHARE.md', 'THIRD-PARTY.md', 'CAPABILITIES.md', 'SOURCES.md', 'VALIDATION.md', 'AUTOMATION.md',
                 'LOCAL67-CALLING-CONTRACT.md', 'LOCAL67-ROUTES.md', 'PRIVATE-CALLS-PROPOSAL.md',
                 'SDK-CALLS.md', 'MULTIMEDIA.md', 'FULL-COVERAGE.md', 'DATABASES.md', 'VOLUME.md', 'RELEASE-0.7.0.md',
                 'V3-DELIVERY-REPORT.md'):
        selected['docs/'+name] = ROOT/'docs'/name
    for folder in ('src', 'native', 'tests', 'vendor/mapgis2shp', 'vendor/geoscan',
                   'vendor/stratigraphic-amenity', 'vendor/cdt', 'third_party/winhlp', 'third_party/gdal_los',
                   'third_party/hydrology', 'workflows', 'packaging', 'benchmarks/mapgis_bench'):
        for file in (ROOT/folder).rglob('*'):
            relative = file.relative_to(ROOT)
            parts = {part.casefold() for part in relative.parts}
            if parts & _EXCLUDED_DIRS or not file.is_file():
                continue
            if (file.suffix.casefold() not in _EXCLUDED_SUFFIXES and not file.name.casefold().endswith('.local.json')
                    and not file.name.casefold().startswith(('pefile', 'idalib', '.env'))):
                if file.is_symlink() or not file.resolve().is_relative_to(ROOT.resolve()):
                    raise ValueError(f'Public source must be an ordinary in-tree file: {relative.as_posix()}')
                selected[relative.as_posix()] = file
    for name in ('launch_mcp.py', 'mapgis_cli.py', 'native_support.py', 'native_session.mjs',
                 'native_workflows.mjs', 'palette_probe.py', 'package.py', 'package_share.py',
                 'index_native_help.py', 'Prepare-NativeOutput.ps1', 'Setup.ps1', 'portable.py', 'self_test.py', 'self_test_native.py',
                 'self_test_workspace.py', 'self_test_modules.py', 'generate_sbom.py', 'benchmark_surfaces.py',
                 'benchmark_worker_pool.py', 'verify_com6.py', 'run_flagship_geology_slice.py'):
        selected['scripts/'+name] = ROOT/'scripts'/name
    return selected


def _site_files(site: Path, profile: str) -> dict[str, Path]:
    if profile not in {'full', 'core'}:
        raise ValueError('profile must be full or core')
    excluded = {'_virtualenv.py', '_virtualenv.pth', '_editable_impl_mapgis_local_agent.pth', 'mapgis_agent',
                'pefile.py', 'peutils.py', 'ordlookup'}
    prefixes = ['mapgis_local_agent-', 'pefile-']
    if profile == 'core':
        stems = {name.replace('-', '_') for name in CORE_EXCLUDES}
        excluded |= stems | {'cv2'} | {f'{name}.libs' for name in stems}
        prefixes += [f'{name}-' for name in stems]
    selected = {}
    for file in site.rglob('*'):
        relative = file.relative_to(site)
        first = relative.parts[0].casefold()
        if first in excluded or first.startswith(tuple(prefixes)) or not file.is_file():
            continue
        if set(relative.parts) & _EXCLUDED_DIRS or file.suffix in _EXCLUDED_SUFFIXES:
            continue
        if file.name in {'direct_url.json', 'uv_cache.json', 'uv_cache', '.gitignore'} or file.name.endswith('.local.json'):
            continue
        if file.suffix == '.pth' and file.name != 'pywin32.pth':
            raise ValueError('Unreviewed environment path hook: ' + file.name)
        # Preserve relative paths and byte identity, including each wheel's GDAL.
        selected['runtime/python/Lib/site-packages/' + relative.as_posix()] = file
    return selected


def _ghostscript_files(ghostscript: Path) -> dict[str, Path]:
    return {'runtime/ghostscript/' + file.relative_to(ghostscript).as_posix(): file
            for file in ghostscript.rglob('*') if file.is_file() and file.suffix not in {'.lib', '.pyc'}}


def _sources_manifest(name: str) -> list[dict]:
    return json.loads((ROOT / 'packaging' / name).read_text(encoding='utf-8'))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def split_source_archive(source_dir: Path, target: Path) -> dict:
    """Move the verified source payload to one companion ZIP, preserving member paths."""
    sources = sorted(source_dir.iterdir())
    if not sources or any(path.is_symlink() or not path.is_file() for path in sources):
        raise ValueError('Expected ordinary, pinned corresponding-source files')
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sources:
            archive.write(file, 'third_party/sources/' + file.name)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError('Companion source ZIP failed CRC verification')
    result = {'file': target.name, 'sha256': sha(target), 'bytes': target.stat().st_size,
              'source_files': {path.name: sha(path) for path in sources},
              'offline_co_delivery': ('Deliver this exact companion ZIP together with the main ZIP and retain all notices. '
                  'It carries the pinned corresponding-source payload for the LGPL libraries and the AGPL Ghostscript CLI '
                  'listed in source_files. Preserve the applicable licences and permit replacement of the LGPL libraries.'),
              'coverage': ('The companion relocates the pinned third_party/sources inputs. Review other embedded wheel '
                           'components and their source obligations before redistribution.')}
    for file in sources:
        file.unlink()
    source_dir.rmdir()
    return result


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
          extra_bundle_sha256: str | None = None, *, profile: str = 'full',
          compliance_archive: Path | None = None) -> dict:
    site = Path(sysconfig.get_paths()['purelib'])
    selected_site = _site_files(site, profile)
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
    for name, file in selected_site.items():
        ordinary_copy(file, stage / name)
    if not base_bundle:
        for name, file in _ghostscript_files(ghostscript).items():
            ordinary_copy(file, stage / name)
        for name in SOURCE_SHA:
            ordinary_copy(source_archives / name, stage / 'third_party/sources' / name)
    extra = _sources_manifest('native-sources.json')
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
    backend = _sources_manifest('backend-sources.json')
    if not backend_sources:raise ValueError('Provide --backend-sources with the pinned SciPy/ContourPy/NetworkX sources')
    for item in backend:
        source=backend_sources/item['file']
        if sha(source)!=item['sha256'] or source.stat().st_size!=item['bytes']:
            raise ValueError('Backend source mismatch: '+item['file'])
        ordinary_copy(source,stage/'third_party/sources'/item['file'])
    companion = split_source_archive(stage / 'third_party/sources', compliance_archive) if compliance_archive else None
    document, summary = generate_sbom(ROOT, distributions=metadata.distributions(path=[str(site)]),
                                      profile=profile, companion=companion)
    components = [{'name': package['name'], 'version': package['versionInfo']}
                  for package in document['packages'] if package['SPDXID'].startswith('SPDXRef-PyPI-')]
    _write_json(stage / 'packaging/sbom.spdx.json', document)
    _write_json(stage / 'sbom.spdx.json', document)
    third_party = {'python': {'version': '3.12.16', 'release': 'python-build-standalone 20261026',
        'url': 'https://github.com/astral-sh/python-build-standalone/releases/tag/20261026', 'archive_sha256': PYTHON_SHA},
        'packages': components, 'profile': profile, 'spdx_unknown_license_count': summary['unknown_license_count'],
        'ghostscript': {'version': '10.08.0', 'source_included': companion is None},
        'geos': {'version': '3.14.1', 'source_included': companion is None},
        'libvips': {'version': '8.18.7', 'source_included': companion is None, 'build_recipes_and_native_dependencies': extra},
        'runtime_scope': 'MapGIS MCP tools (exact names verified on restored startup); isolated exact-build x86 native buffers, WP topology deletion, MSI/MPJ/EPS; CDT native TIN storage, SciPy/ContourPy/NetworkX open algorithms; Pyogrio/GDAL vector exchange and Rasterio/GDAL rasters, libvips and separate GPL-2.0 help indexing; analysis-only pefile excluded',
        'sources': SOURCE_SHA,
        'backend_sources': backend,
        'base_bundle_sha256': sha(base_bundle) if base_bundle else None,
        'licences': 'See docs/THIRD-PARTY.md, dependency dist-info/licenses and included source archives.'}
    if companion:
        third_party['compliance_sources'] = companion
        for component in ('ghostscript', 'geos', 'libvips'):
            third_party[component]['source_archive'] = companion['file']
    _write_json(stage / 'THIRD-PARTY.json', third_party)
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


def _measure(files: dict[str, Path], compression_sizes: dict[Path, tuple[int, int]], prefix: str = '') -> dict:
    uncompressed_bytes = 0
    zip_bytes = 22  # ZIP end-of-central-directory record, with no comment.
    for name, file in files.items():
        if file.is_symlink():
            raise ValueError(f'Budget input is a link: {name}')
        if file not in compression_sizes:
            compressor = zlib.compressobj(level=6, wbits=-15)
            plain = compressed = 0
            with file.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    plain += len(chunk)
                    compressed += len(compressor.compress(chunk))
            compression_sizes[file] = plain, compressed + len(compressor.flush())
        plain, compressed = compression_sizes[file]
        uncompressed_bytes += plain
        zip_bytes += compressed + 76 + 2 * len((prefix + name).encode('utf-8'))
    return {'files': len(files), 'uncompressed_bytes': uncompressed_bytes, 'zip_estimated_bytes': zip_bytes}


def packaging_budget(site: Path) -> dict:
    """Measure current bytes and level-six ZIP costs using the same payload filters as build."""
    sources = _sources_manifest('native-sources.json') + _sources_manifest('backend-sources.json')
    expected = {name: {'sha256': digest} for name, digest in SOURCE_SHA.items()}
    expected.update({item['file']: item for item in sources})
    available = {}
    missing = []
    for name, record in sorted(expected.items()):
        relative = 'third_party/sources/' + name
        file = ROOT / relative
        if file.is_file():
            if file.is_symlink() or sha(file) != record['sha256']:
                raise ValueError(f'Corresponding source hash mismatch: {name}')
            available[relative] = file
        else:
            missing.append({'file': name, 'bytes_from_pinned_manifest': record.get('bytes')})

    public = public_sources(portable=True)
    ghostscript = _ghostscript_files(ROOT / 'runtime/ghostscript')
    full = _site_files(site, 'full')
    core = _site_files(site, 'core')
    measured = {}
    companion = _measure(available, measured)
    profiles = {}
    for name, dependencies, split in (('full', full, False), ('split-sources', full, True), ('core-split', core, True)):
        content = public | dependencies | ghostscript
        if not split:
            content |= available
        main = _measure(content, measured, 'MapGIS-Agent/')
        profiles[name] = {'main': main, 'companion': companion if split else None,
                          'combined_uncompressed_bytes': main['uncompressed_bytes'] + (companion['uncompressed_bytes'] if split else 0),
                          'combined_zip_estimated_bytes': main['zip_estimated_bytes'] + (companion['zip_estimated_bytes'] if split else 0)}
    return {'schema_version': 'mapgis-packaging-budget/v1', 'measurement_scope': 'Current selected source tree, installed site-packages, local Ghostscript runtime and hash-matched local source archives',
            'zip_method': 'Actual raw DEFLATE level 6 per file, plus ZIP local and central headers; standard non-ZIP64 estimate',
            'profiles': profiles, 'core_excluded_sites': sorted({Path(name).parts[4] for name in full.keys() - core.keys()}),
            'unmeasured_inputs': {'portable_cpython_archive': 'Supply the pinned Python archive or verified base bundle for a release build',
                                  'corresponding_sources': missing,
                                  'generated_members': ['THIRD-PARTY.json', 'sbom.spdx.json', 'bundle-manifest.json', 'CMD launchers']},
            'known_unmeasured_source_bytes': sum(item['bytes_from_pinned_manifest'] or 0 for item in missing),
            'measured_gdal_dlls': {name: measured[path][0] for name, path in full.items()
                                   if ('/rasterio.libs/' in name or '/pyogrio.libs/' in name)
                                   and path.name.casefold().startswith('gdal') and path.suffix.casefold() == '.dll'}}


def _archive_stage(stage: Path, target: Path) -> None:
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(stage.rglob('*')):
            if file.is_file():
                archive.write(file, 'MapGIS-Agent/' + file.relative_to(stage).as_posix())


def build_mcpb(output: Path) -> dict:
    """Package the launcher and project code for an existing local Python runtime."""
    files = public_sources()
    include = {'LICENSE', 'NOTICE.md', 'pyproject.toml', 'scripts/launch_mcp.py',
               'docs/THIRD-PARTY.md', 'docs/V3-DELIVERY-REPORT.md'}
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(files['packaging/mcpb/manifest.json'], 'manifest.json')
        archive.writestr('icon.svg', _MCPB_ICON)
        for name, file in sorted(files.items()):
            if name in include or name.startswith(('src/', 'workflows/', 'vendor/', 'third_party/')):
                if file.is_symlink() or not file.resolve().is_relative_to(ROOT.resolve()):
                    raise ValueError(f'MCPB input is outside the public source tree: {name}')
                archive.write(file, name)
    return {'output': str(output), 'sha256': sha(output), 'bytes': output.stat().st_size}


def _publish_archive(source: Path, dest: Path) -> None:
    with source.open('rb') as original, dest.open('xb') as received:
        try:
            shutil.copyfileobj(original, received, length=1024 * 1024)
        except BaseException:
            received.close()
            dest.unlink()
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', type=Path)
    runtime_input = parser.add_mutually_exclusive_group()
    runtime_input.add_argument('--python-archive', type=Path)
    runtime_input.add_argument('--base-bundle', type=Path)
    parser.add_argument('--base-sha256', help='Exact digest of an accepted current bundle when reusing its runtime')
    parser.add_argument('--ghostscript', type=Path)
    parser.add_argument('--source-archives', type=Path)
    extra_input = parser.add_mutually_exclusive_group()
    extra_input.add_argument('--extra-sources', type=Path)
    extra_input.add_argument('--extra-sources-bundle', type=Path)
    parser.add_argument('--extra-bundle-sha256', help='Exact digest when using an accepted current bundle for corresponding sources')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--backend-sources', type=Path)
    parser.add_argument('--profile', choices=('full', 'core'), default='full')
    parser.add_argument('--split-sources', action='store_true')
    special = parser.add_mutually_exclusive_group()
    special.add_argument('--dry-run-budget', type=Path, metavar='OUTPUT_JSON')
    special.add_argument('--mcpb-output', type=Path, metavar='OUTPUT_MCPB')
    args = parser.parse_args()
    if args.mcpb_output is not None:
        if args.mcpb_output.suffix.casefold() != '.mcpb':
            parser.error('--mcpb-output requires a .mcpb file name')
        print(json.dumps(build_mcpb(args.mcpb_output), ensure_ascii=False))
        return
    if args.dry_run_budget is not None:
        receipt = packaging_budget(Path(sysconfig.get_paths()['purelib']))
        args.dry_run_budget.parent.mkdir(parents=True, exist_ok=True)
        _write_json(args.dry_run_budget, receipt)
        print(json.dumps({'status': 'MEASURED_CURRENT_INPUTS', 'output': str(args.dry_run_budget),
                          'profiles': receipt['profiles']}, ensure_ascii=False))
        return
    if not (args.stage and args.output and args.backend_sources and (args.python_archive or args.base_bundle)
            and (args.extra_sources or args.extra_sources_bundle)):
        parser.error('A build requires --stage, --output, --backend-sources, one runtime input and one extra-source input')
    args.output = args.output.resolve()
    if args.output.exists():
        raise FileExistsError(args.output)
    if args.output.is_relative_to(args.stage.resolve()):
        parser.error('--output must be outside --stage')
    companion = args.output.with_name(COMPANION_NAME) if args.split_sources else None
    if companion is not None and (companion == args.output or companion.exists()):
        raise FileExistsError(companion)
    with TemporaryDirectory(prefix='mapgis-share-', dir=args.output.parent.resolve()) as temporary:
        transaction = Path(temporary)
        manifest = build(args.stage.resolve(), args.python_archive, args.ghostscript,
                         args.source_archives, args.base_bundle, args.extra_sources, args.extra_sources_bundle,
                         args.backend_sources, args.base_sha256, args.extra_bundle_sha256, profile=args.profile,
                         compliance_archive=transaction / COMPANION_NAME if companion is not None else None)
        main_zip = transaction / args.output.name
        _archive_stage(args.stage.resolve(), main_zip)
        if companion is not None:
            _publish_archive(transaction / COMPANION_NAME, companion)
        try:
            _publish_archive(main_zip, args.output)
        except BaseException:
            if companion is not None:
                companion.unlink()
            raise
    receipt = {'archive': str(args.output.resolve()), 'archive_sha256': sha(args.output),
        'archive_bytes': args.output.stat().st_size, 'payload_files': len(manifest),
        'payload_bytes': sum(v['bytes'] for v in manifest.values()),
        'version': tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'],
        'profile': args.profile,
        'status': 'BUILT_REQUIRES_RESTORE_AND_ISOLATED_TEST'}
    if companion is not None:
        receipt['compliance_sources'] = {'archive': str(companion), 'archive_sha256': sha(companion),
                                         'archive_bytes': companion.stat().st_size}
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
