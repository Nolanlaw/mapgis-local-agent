"""Package relocation and local-configuration preservation."""
import importlib.util
import json
from pathlib import Path
import tomllib

import pytest

spec = importlib.util.spec_from_file_location('portable', Path(__file__).parents[1] / 'scripts/portable.py')
portable = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portable)


def test_imported_version_matches_project_version():
    import mapgis_agent

    project_file = Path(__file__).parents[1] / 'pyproject.toml'
    version = tomllib.loads(project_file.read_text(encoding='utf-8'))['project']['version']
    assert mapgis_agent.__version__ == version


def test_legacy_native_host_needs_explicit_package_local_opt_in(tmp_path):
    root=tmp_path/'package';root.mkdir()
    native=tmp_path/'mapgis';(native/'program').mkdir(parents=True)
    (native/'program/mdiedit6x.exe').write_bytes(b'fixture')
    portable.configure(root,root/'python.exe',str(native))
    assert 'native_backend_legacy_dep_compatibility' not in portable.load(root/'config.local.json')
    portable.configure(root,root/'python.exe',native_legacy=True)
    assert portable.load(root/'config.local.json')['native_backend_legacy_dep_compatibility'] is True
    portable.configure(root,root/'python.exe')
    assert portable.load(root/'config.local.json')['native_backend_legacy_dep_compatibility'] is True


def test_private_ui_opt_in_survives_package_move_and_requires_installation(tmp_path):
    root=tmp_path/'package';root.mkdir()
    with pytest.raises(ValueError,match='licensed MapGIS'):
        portable.configure(root,root/'python.exe',private_ui=True)
    assert not(root/'config.local.json').exists()
    native=tmp_path/'mapgis';(native/'program').mkdir(parents=True)
    (native/'program/mdiedit6x.exe').write_bytes(b'fixture')
    portable.configure(root,root/'python.exe',str(native))
    assert portable.load(root/'config.local.json').get('native_general_private_ui')is not True
    portable.configure(root,root/'python.exe',private_ui=True)
    moved=tmp_path/'moved';root.rename(moved)
    result=portable.configure(moved,moved/'python.exe')
    config=portable.load(moved/'config.local.json')
    assert config['native_general_private_ui']is True and config['native_volume_private_ui']is True
    assert result['private_ui_enabled']is True


def test_setup_rebinds_its_own_config_after_folder_moves_and_preserves_external_edits(tmp_path):
    first = tmp_path / 'first'
    first.mkdir()
    runtime = first / 'runtime/python/python.exe'
    portable.configure(first, runtime)
    moved = tmp_path / '中文 moved folder'
    first.rename(moved)
    portable.configure(moved, moved / 'runtime/python/python.exe')
    config = portable.load(moved / 'mcp-config.local.json')
    assert config['mcpServers']['mapgis_local']['command'] == str((moved / 'runtime/python/python.exe').resolve())
    assert str(moved) in config['mcpServers']['mapgis_local']['args']
    assert not (moved / 'config.local.json').exists()
    (moved / 'mcp-config.local.json').write_text('{"user":"keep"}', encoding='utf-8')
    with pytest.raises(ValueError, match='independently edited'):
        portable.configure(moved, moved / 'runtime/python/python.exe')
    assert (moved / 'mcp-config.local.json').read_text() == '{"user":"keep"}'


def test_invalid_or_changed_mapgis_binding_does_not_overwrite_config(tmp_path):
    root = tmp_path / 'package'
    root.mkdir()
    with pytest.raises(ValueError, match='must contain'):
        portable.configure(root, root / 'python.exe', str(tmp_path / 'missing'))
    assert not (root / 'mcp-config.local.json').exists()
    native = tmp_path / 'mapgis'
    (native / 'program').mkdir(parents=True)
    (native / 'program/mdiedit6x.exe').write_bytes(b'not a supported binary')
    portable.configure(root, root / 'python.exe', str(native / 'program/mdiedit6x.exe'))
    assert portable.profile(root)['status'] == 'UNSUPPORTED_BUILD'
    saved = (root / 'config.local.json').read_bytes()
    other = tmp_path / 'other'
    (other / 'program').mkdir(parents=True)
    (other / 'program/mdiedit6x.exe').write_bytes(b'other')
    with pytest.raises(ValueError, match='elsewhere'):
        portable.configure(root, root / 'python.exe', str(other))
    assert (root / 'config.local.json').read_bytes() == saved
