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


def test_reuse_rejects_escape_inside_allowed_prefix(tmp_path, monkeypatch):
    archive, stage = fixture_bundle(tmp_path, monkeypatch,
        {'runtime/python/../../../escape.txt': b'escape'})
    with pytest.raises(ValueError, match='path'):
        builder.copy_base_runtime(stage, archive)
    assert not (tmp_path / 'escape.txt').exists()
