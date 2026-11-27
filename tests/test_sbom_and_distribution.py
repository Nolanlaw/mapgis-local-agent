import asyncio
import hashlib
from importlib import metadata, util
import json
import os
from pathlib import Path
import sys
import tomllib
from zipfile import ZipFile

from jsonschema import validators
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from mapgis_agent.sbom import EXCLUDED_DISTRIBUTIONS, generate_sbom, resolve_license, validate_sbom
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]


def _script(name):
    spec = util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_spdx_committed_and_current_distribution_inventories_resolve_every_license():
    document, summary = generate_sbom(ROOT)
    validate_sbom(document)
    expected = {canonicalize_name(item.metadata['Name']) for item in metadata.distributions()} - EXCLUDED_DISTRIBUTIONS
    assert {item['name'] for item in summary['distributions']} == expected
    assert summary['installed_distribution_count'] == len(expected)
    assert summary['unknown_license_count'] == 0
    delivered = _json(ROOT / 'packaging/sbom.spdx.json')
    validate_sbom(delivered)
    names = {record['name'] for record in delivered['packages']}
    assert {'winhlp', 'Ghostscript', 'GEOS', 'libvips', 'CPython (portable build)', 'CDT',
            'GDAL (Pyogrio)', 'GDAL (Rasterio)', 'mapgis2shp (vendored)'} <= names
    assert not any(name.casefold() == 'pefile' for name in names)
    delivered['packages'][0]['licenseConcluded'] = 'UNKNOWN'
    with pytest.raises(ValueError, match='Unresolved SPDX license'):
        validate_sbom(delivered)


def test_license_precedence_and_unknown_version_fails_closed(tmp_path):
    dist_info = tmp_path / 'fixture-1.dist-info'
    dist_info.mkdir()
    meta = dist_info / 'METADATA'
    fields = 'Metadata-Version: 2.4\nName: fixture\nVersion: 1\n'
    meta.write_text(fields + 'License-Expression: MIT OR Apache-2.0\nLicense: GPL-3.0-only\n', encoding='utf-8')
    distribution = metadata.PathDistribution(dist_info)
    assert resolve_license(distribution).expression == 'MIT OR Apache-2.0'
    meta.write_text(fields + 'License: BSD\nClassifier: License :: OSI Approved :: BSD License\n', encoding='utf-8')
    with pytest.raises(ValueError, match='fixture==1'):
        resolve_license(distribution)
    with pytest.raises(ValueError, match='fixture==1'):
        generate_sbom(ROOT, distributions=[distribution])


def test_official_distribution_schemas():
    if not (schema_path := os.environ.get('MAPGIS_DISTRIBUTION_SCHEMA_DIR')):
        pytest.skip('Official schemas are supplied by the distribution-validation job')
    for schema_name, relative in [('registry.json', 'server.json'), ('mcpb.json', 'packaging/mcpb/manifest.json'),
                                 ('spdx.json', 'packaging/sbom.spdx.json')]:
        schema = _json(Path(schema_path) / schema_name)
        validator = validators.validator_for(schema)
        validator.check_schema(schema)
        validator(schema, format_checker=validator.FORMAT_CHECKER).validate(_json(ROOT / relative))


def test_distribution_entry_metadata_and_public_allowlist(tmp_path):
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    registry = _json(ROOT / 'server.json')
    package = registry['packages'][0]
    bundle = _json(ROOT / 'packaging/mcpb/manifest.json')
    assert project['license'] == 'Apache-2.0'
    assert (ROOT / 'LICENSE').read_text(encoding='utf-8').splitlines()[0].strip() == 'Apache License'
    assert registry['version'] == bundle['version'] == package['version'] == project['version']
    assert registry['name'].endswith('/' + project['name']) and bundle['name'] == package['identifier'] == project['name']
    assert package['transport']['type'] == 'stdio'
    assert bundle['server']['mcp_config']['env']['MAPGIS_MCP_SURFACE'] == 'hybrid'
    assert (ROOT / bundle['server']['entry_point']).is_file()
    assert {'hydrology', 'scan-cv', 'cad', 'dev', 'full'} <= set(project['optional-dependencies'])
    builder = _script('package_share')
    for portable in (False, True):
        files = builder.public_sources(portable)
        required = {'LICENSE', 'SECURITY.md', 'server.json', 'benchmarks/mapgis_bench/run_bench.py',
                    'packaging/mcpb/manifest.json', 'packaging/skill/SKILL.md', 'packaging/sbom.spdx.json',
                    'docs/V3-DELIVERY-REPORT.md', 'scripts/generate_sbom.py', 'scripts/benchmark_worker_pool.py'}
        assert required <= set(files)
        assert all(files[key].is_file() for key in required)
        assert not any('.local.json' in name or Path(name).parts[0] in {'work', 'evidence', '.venv'}
                       or Path(name).suffix in {'.i64', '.idb'} for name in files)
    portable = _script('portable')
    result = portable.configure(tmp_path, Path(sys.executable), surface='compact')
    assert result['surface'] == 'compact'
    configuration = _json(tmp_path / 'mcp-config.local.json')
    assert configuration['mcpServers']['mapgis_local']['args'][-4:] == ['--surface', 'compact', '--transport', 'stdio']


def test_core_split_share_cross_binds_source_zip_and_keeps_both_gdal_payloads(tmp_path, monkeypatch):
    builder = _script('package_share')
    root = tmp_path / 'project'
    (root / 'packaging').mkdir(parents=True)
    for name in ('native-sources.json', 'backend-sources.json'):
        (root / 'packaging' / name).write_text('[]', encoding='utf-8')
    (root / 'pyproject.toml').write_text('[project]\nname="mapgis-local-agent"\nversion="3.0.0"\n', encoding='utf-8')
    monkeypatch.setattr(builder, 'ROOT', root)
    monkeypatch.setattr(builder, 'public_sources', lambda portable: {
        'pyproject.toml': root / 'pyproject.toml', 'packaging/fixture.json': root / 'packaging/backend-sources.json'})

    source_payload = b'verified corresponding source'
    runtime_files = {'runtime/python/python.exe': b'python', 'runtime/ghostscript/bin/gswin64c.exe': b'gs',
                     'third_party/sources/source.tar.xz': source_payload}
    digest = lambda content: hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(builder, 'SOURCE_SHA', {'source.tar.xz': digest(source_payload)})
    base = tmp_path / 'base.zip'
    with ZipFile(base, 'x') as zip_file:
        for name, content in runtime_files.items():
            zip_file.writestr('MapGIS-Agent/' + name, content)
        zip_file.writestr('MapGIS-Agent/bundle-manifest.json', json.dumps({
            name: {'bytes': len(content), 'sha256': digest(content)} for name, content in runtime_files.items()}))
    site = tmp_path / 'site-packages'
    site_payload = {'rasterio.libs/gdal-rasterio.dll': b'GDAL 3.10.3', 'pyogrio.libs/gdal-pyogrio.dll': b'GDAL 3.12.4',
                    'cv2/cv2.pyd': b'OpenCV', 'llvmlite/binding/llvmlite.dll': b'LLVMLite', 'numba/init.py': b'',
                    'pyflwdir/init.py': b'', '_editable_impl_mapgis_local_agent.pth': b'local development path',
                    'pefile.py': b'analysis only', 'postinstall/direct_url.json': b'not a distribution input'}
    for name, content in site_payload.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for name, version in [('rasterio', '1.4.4'), ('pyogrio', '0.13.0'), ('numba', '0.68.1')]:
        folder = site / f'{name}-{version}.dist-info'
        folder.mkdir()
        (folder / 'METADATA').write_text(f'Name: {name}\nVersion: {version}\nLicense-Expression: MIT\n', encoding='utf-8')
    monkeypatch.setattr(builder.sysconfig, 'get_paths', lambda: {'purelib': str(site)})
    prefix = 'runtime/python/Lib/site-packages/'
    assert prefix + 'cv2/cv2.pyd' in builder._site_files(site, 'full')
    assert prefix + 'cv2/cv2.pyd' not in builder._site_files(site, 'core')

    companion = tmp_path / builder.COMPANION_NAME
    stage = tmp_path / 'stage'
    files = builder.build(stage, None, None, None, base_bundle=base, base_sha256=builder.sha(base),
                          extra_sources=tmp_path, backend_sources=tmp_path, profile='core', compliance_archive=companion)
    manifest = _json(stage / 'THIRD-PARTY.json')
    companion_meta = manifest['compliance_sources']
    assert companion_meta['sha256'] == builder.sha(companion) and companion_meta['file'] == companion.name
    sbom = _json(stage / 'sbom.spdx.json')
    validate_sbom(sbom)
    assert json.loads(sbom['annotations'][0]['comment']) == companion_meta
    assert not any(name.startswith('third_party/sources/') for name in files)
    assert not any(record['name'] == 'numba' for record in manifest['packages'])
    with ZipFile(companion) as archive:
        assert archive.testzip() is None
        assert archive.read('third_party/sources/source.tar.xz') == source_payload
    main = tmp_path / 'main.zip'
    builder._archive_stage(stage, main)
    with ZipFile(main) as archive:
        for file, expected in site_payload.items():
            member = 'MapGIS-Agent/' + prefix + file
            if '/gdal-' in file:
                assert archive.read(member) == expected
            else:
                assert member not in archive.namelist()


def test_mcpb_unpack_and_launch_declared_stdio_command(tmp_path):
    builder = _script('package_share')
    extension = tmp_path / 'mapgis-local-agent.mcpb'
    assert builder.build_mcpb(extension)['sha256'] == builder.sha(extension)
    bundle = tmp_path / 'extension'
    with ZipFile(extension) as archive:
        assert archive.testzip() is None
        archive.extractall(bundle)
    manifest = _json(bundle / 'manifest.json')
    assert (bundle / manifest['icon']).read_text(encoding='utf-8').startswith('<svg')
    config = manifest['server']['mcp_config']
    replacements = {'${__dirname}': str(bundle), '${user_config.task_root}': str(tmp_path / 'working area')}
    args = [arg.replace('${__dirname}', replacements['${__dirname}']).replace(
        '${user_config.task_root}', replacements['${user_config.task_root}']) for arg in config['args']]

    async def check():
        parameters = StdioServerParameters(command=sys.executable, args=args, env=config['env'])
        async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 21
            result = await session.call_tool('mapgis_environment', {})
            assert not result.isError
            loaded = result.structuredContent['loaded_runtime']
            assert Path(loaded['server_module']).is_relative_to(bundle)

    asyncio.run(check())


def test_core_service_starts_when_optional_distributions_are_absent(tmp_path):
    import geopandas as gpd
    from pymapgis import Writer
    from shapely.geometry import Point

    source = tmp_path / 'source.wt'
    Writer(gpd.GeoDataFrame({'ID': [21]}, geometry=[Point(1, 2)])).to_file(source)
    injection = '''
import importlib.util, runpy, sys
optional = {'cv2', 'llvmlite', 'numba', 'pyflwdir'}
class MissingOptional:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in optional:
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, MissingOptional())
real_find = importlib.util.find_spec
def find_spec(name, *args, **kwargs):
    return None if name in optional else real_find(name, *args, **kwargs)
importlib.util.find_spec = find_spec
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''

    async def check():
        arguments = ['-s', '-B', '-X', 'utf8', '-c', injection, str(ROOT / 'scripts/launch_mcp.py'),
                     '--root', str(tmp_path), '--surface', 'hybrid']
        parameters = StdioServerParameters(command=sys.executable, args=arguments)
        async with stdio_client(parameters) as streams, ClientSession(*streams) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 21
            result = await session.call_tool('mapgis_discover', {'operation_id': 'file.inspect'})
            assert not result.isError
            assert result.structuredContent['execution_status'] == 'MCP_TOOL_AVAILABLE'
            opened = await session.call_tool('mapgis_inspect', {'op_id': 'file.inspect', 'arguments': {'file': str(source)}})
            assert not opened.isError and opened.structuredContent['rows'][0]['attributes']['ID'] == 21
            unavailable = await session.call_tool('mapgis_execute', {'op_id': 'mapgis_prepare_grid_analysis', 'arguments': {
                'file': str(source), 'expected_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'parameters': {}, 'template': {}, 'request_id': 'needs-opencv'}})
            assert unavailable.isError and unavailable.structuredContent['error_code'] == 'PROVIDER_UNAVAILABLE'

    asyncio.run(check())


def test_mapgis_bench_smoke_passes_paired_file_and_transaction_cases(tmp_path):
    from benchmarks.mapgis_bench.run_bench import TASKS, run_benchmark

    tasks = tuple(task for task in TASKS if task.id in {'B01', 'B04'})
    assert len(tasks) == 2
    report = asyncio.run(run_benchmark(tmp_path, config={}, tasks=tasks))
    for entry in report['surfaces'].values():
        assert entry['overall']['statuses'] == {'PASS': 2}
