"""Release input reuse must preserve bytes and reject changed/escaping archives."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/package_share.py'
spec = importlib.util.spec_from_file_location('share_builder', SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def fixture_bundle(tmp_path, monkeypatch, extra=None):
    files = {'runtime/python/python.exe': b'python',
             'runtime/python/Lib/encodings/utf_8.py': b'stdlib',
             'runtime/python/Lib/site-packages/obsolete.py': b'old dependency',
             'runtime/ghostscript/bin/gswin64c.exe': b'ghostscript',
             'third_party/sources/gs.tar.xz': b'source'}
    files.update(extra or {})
    manifest = {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                for name, data in files.items()}
    archive = tmp_path / 'base.zip'
    with zipfile.ZipFile(archive, 'x') as zf:
        for name, data in files.items():
            zf.writestr('MapGIS-Agent/' + name, data)
        zf.writestr('MapGIS-Agent/bundle-manifest.json', json.dumps(manifest))
    monkeypatch.setattr(builder, 'BASE_BUNDLE_SHA', builder.sha(archive))
    monkeypatch.setattr(builder, 'SOURCE_SHA', {'gs.tar.xz': hashlib.sha256(b'source').hexdigest()})
    stage = tmp_path / 'stage'
    stage.mkdir()
    return archive, stage


def test_reuse_copies_verified_runtime_but_replaces_old_dependencies(tmp_path, monkeypatch):
    archive, stage = fixture_bundle(tmp_path, monkeypatch)
    builder.copy_base_runtime(stage, archive)
    assert (stage / 'runtime/python/Lib/encodings/utf_8.py').read_bytes() == b'stdlib'
    assert (stage / 'runtime/ghostscript/bin/gswin64c.exe').read_bytes() == b'ghostscript'
    assert not (stage / 'runtime/python/Lib/site-packages').exists()


def test_reuse_rejects_changed_bundle(tmp_path, monkeypatch):
    archive, stage = fixture_bundle(tmp_path, monkeypatch)
    archive.write_bytes(archive.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='base bundle'):
        builder.copy_base_runtime(stage, archive)


def test_current_verified_release_can_replace_the_old_base(tmp_path, monkeypatch):
    archive,stage=fixture_bundle(tmp_path,monkeypatch)
    expected=builder.sha(archive)
    monkeypatch.setattr(builder,'BASE_BUNDLE_SHA','0'*64)
    builder.copy_base_runtime(stage,archive,expected)
    assert(stage/'runtime/python/python.exe').read_bytes()==b'python'


def test_reuse_rejects_escape_inside_allowed_prefix(tmp_path, monkeypatch):
    archive, stage = fixture_bundle(tmp_path, monkeypatch,
        {'runtime/python/../../../escape.txt': b'escape'})
    with pytest.raises(ValueError, match='path'):
        builder.copy_base_runtime(stage, archive)
    assert not (tmp_path / 'escape.txt').exists()


def test_cdt_build_sources_and_licenses_travel_together_in_both_source_sets():
    for portable in [False,True]:
        sources=builder.public_sources(portable)
        required={'vendor/cdt/SOURCE.json','vendor/cdt/LICENSE','vendor/cdt/CDT/LICENSE',
            'vendor/cdt/CDT/LICENSE-predicates.txt','vendor/cdt/CDT/LICENSE-LLVM.txt',
            'vendor/cdt/CDT/include/CDT.h','native/NativeTin.h'}
        assert required<=set(sources)
        receipt=json.loads(sources['vendor/cdt/SOURCE.json'].read_text(encoding='utf8'))
        assert receipt['commit']=='53eff288a4e77ad1c0afe1a10f60bd2b57f877b8'
        for record in receipt['files']:
            original=sources['vendor/cdt/'+record['path']]
            assert hashlib.sha256(original.read_bytes()).hexdigest()==record['sha256']


def test_adapted_sheet_kernel_mit_license_and_modification_notice_travel_in_both_source_sets():
    for portable in [False,True]:
        sources=builder.public_sources(portable)
        required={'src/mapgis_agent/_vendor/ovobj_grids.py','src/mapgis_agent/_vendor/ovobj-grids.LICENSE','src/mapgis_agent/_vendor/ovobj-grids.PATCH.md'}
        assert required<=set(sources)
        license=sources['src/mapgis_agent/_vendor/ovobj-grids.LICENSE'].read_text(encoding='utf8')
        assert 'MIT License' in license and 'Copyright (c) 2026 good9527' in license
        note=sources['src/mapgis_agent/_vendor/ovobj-grids.PATCH.md'].read_text(encoding='utf8')
        assert 'ef5a338f7c45d22c48c841bff8ae059cf75a1cd9' in note and '576x576' in note


def test_replaceable_wellpathpy_sources_and_both_license_texts_travel_together():
    prefix='src/mapgis_agent/_vendor/wellpathpy/'
    for portable in [False,True]:
        sources=builder.public_sources(portable)
        receipt=json.loads(sources[prefix+'SOURCE.json'].read_text(encoding='utf8'))
        assert receipt['commit']=='ccbb7cd803485a74d2de83ef4dd5912bb7f9c2bd'
        for record in receipt['files']:
            original=sources[prefix+record['name']]
            assert hashlib.sha256(original.read_bytes()).hexdigest()==record['sha256']
            assert original.stat().st_size==record['bytes']
        assert {prefix+name for name in ['__init__.py','LICENSE.LGPL3','COPYING.GPL3']}<=set(sources)
