from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3

import geopandas as gpd
from pymapgis import Writer
import pytest
from shapely.geometry import LineString

from mapgis_agent.artifacts import begin_bundle, finish_bundle
from mapgis_agent.core import Bridge, BridgeError, json_text, sha
from mapgis_agent.object_store import CHUNK_SIZE, ContentAddressedStore, ObjectStoreError


def test_object_deduplicates_and_exports_an_independent_verified_copy(tmp_path):
    source = tmp_path / 'source.bin'
    source.write_bytes(b'abc' * CHUNK_SIZE)
    store = ContentAddressedStore(tmp_path / 'evidence' / 'objects')
    obj = store.put(source)
    expected = sha(source.read_bytes())
    stored = store.root / expected[:2] / expected
    assert (obj.digest, obj.size) == (expected, source.stat().st_size)
    timestamp = stored.stat().st_mtime_ns
    assert store.put(source) == obj
    assert stored.stat().st_mtime_ns == timestamp
    result = tmp_path / 'result.bin'
    assert store.copy_to(expected, result, expected_size=obj.size) == obj
    assert result.read_bytes() == source.read_bytes()
    assert [path.stat().st_nlink for path in (source, stored, result)] == [1, 1, 1]
    with pytest.raises(FileExistsError):
        store.copy_to(expected, result)
    stored.write_bytes(b'corrupted')
    with pytest.raises(ObjectStoreError, match='ARTIFACT_OBJECT_CORRUPT'):
        store.put(source)
    with pytest.raises(ObjectStoreError, match='ARTIFACT_OBJECT_CORRUPT'):
        store.copy_to(expected, tmp_path / 'failed.bin')
    assert stored.read_bytes() == b'corrupted' and not (tmp_path / 'failed.bin').exists()


def test_object_refuses_unsealed_input_and_path_escape(tmp_path):
    source = tmp_path / 'source'
    source.write_bytes(b'data')
    store = ContentAddressedStore(tmp_path / 'objects')
    with pytest.raises(ObjectStoreError, match='ARTIFACT_DIGEST_INVALID'):
        store.path_for('../outside')
    with pytest.raises(ObjectStoreError, match='ARTIFACT_CANDIDATE_CHANGED'):
        store.put(source, expected_digest='a' * 64)
    assert list(store.root.iterdir()) == []


class LimitedBridge(Bridge):
    @contextmanager
    def connect(self):
        with super().connect() as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, CHUNK_SIZE)
            yield db


def _bundle(bridge, source, request_id):
    return begin_bundle(bridge, {'operation': 'test.bundle'}, request_id,
                        [{'file': str(source), 'sha256': sha(source.read_bytes())}], 'result.bin')


def test_new_member_larger_than_sqlite_limit_and_legacy_blob_coexist(tmp_path):
    bridge = LimitedBridge(tmp_path)
    source = tmp_path / 'original'
    source.write_bytes(b'original')
    _, old, _ = _bundle(bridge, source, 'legacy')
    with bridge.connect() as db:
        db.execute('CREATE TABLE artifact_members (request_id TEXT, name TEXT, digest TEXT, data BLOB, '
                   'PRIMARY KEY(request_id, name))')
        db.execute('INSERT INTO artifact_members VALUES (?,?,?,?)',
                   ('legacy', 'result.bin', sha(b'historical'), b'historical'))
        db.execute("UPDATE commands SET state='PREPARED' WHERE id='legacy'")
    assert bridge.status('legacy')['state'] == 'PREPARED'
    stage, _, _ = _bundle(bridge, source, 'cas')
    files = stage / 'files'
    files.mkdir()
    payload = b'x' * (2 * CHUNK_SIZE + 5)
    (files / 'result.bin').write_bytes(payload)
    new = finish_bundle(bridge, 'cas', files, {})
    epoch = bridge.advance_epoch()
    with pytest.raises(BridgeError, match='STALE_PUBLISHER_EPOCH'):
        bridge.apply('cas', expected_epoch=epoch - 1)
    assert not Path(new['output']).exists()
    with bridge.connect() as db:
        assert tuple(db.execute('SELECT digest,size,storage FROM artifact_member_refs').fetchone()) == (
            sha(payload), len(payload), 'cas')
        assert db.execute('SELECT COUNT(*) FROM artifact_members').fetchone()[0] == 1
    (files / 'result.bin').unlink()
    assert bridge.apply('cas', expected_epoch=epoch)['state'] == 'APPLIED'
    assert Path(new['output']).read_bytes() == payload
    assert bridge.apply('legacy')['state'] == 'APPLIED'
    assert Path(old['output']).read_bytes() == b'historical'
    assert bridge.status('cas')['output_matches'] and bridge.status('legacy')['output_matches']
    with bridge.connect() as db:
        assert db.execute('SELECT data FROM artifact_members').fetchone()[0] == b'historical'
    assert source.read_bytes() == b'original'


def _prepare_patch(tmp_path):
    bridge = Bridge(tmp_path / 'isolated')
    source = tmp_path / 'source.wl'
    Writer(gpd.GeoDataFrame({'ID': [1]}, geometry=[LineString([(1, 1), (2, 2)])],
                           crs='EPSG:4326')).to_file(source)
    copy = bridge.copy(str(source), 'copy')
    receipt = bridge.prepare(copy['path'], copy['sha256'], [1], 'edit', style={'color': 2})
    return bridge, source, Path(receipt['output'])


@pytest.mark.parametrize('after_rename', [False, True])
def test_single_file_fault_boundaries_are_reconcilable(tmp_path, monkeypatch, after_rename):
    bridge, source, output = _prepare_patch(tmp_path)
    original = source.read_bytes()
    replace = os.replace

    def interrupted(staging, destination):
        assert Path(destination) == output.parent
        assert (Path(staging) / output.name).is_file() and not output.parent.exists()
        if after_rename:
            replace(staging, destination)
        raise OSError('simulated interruption')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'replace', interrupted)
        with pytest.raises(OSError, match='simulated interruption'):
            bridge.apply('edit')
    status = bridge.status('edit')
    assert status['state'] == 'PREPARED' and status['output_matches'] is after_rename
    assert bridge.apply('edit')['reconciled_existing_output'] is after_rename
    assert source.read_bytes() == original and bridge.status('edit')['output_matches']


def test_epoch_is_monotonic_across_writers_and_stops_an_old_commit(tmp_path):
    bridge, _, output = _prepare_patch(tmp_path)
    assert bridge.current_epoch() == 0
    peers = [Bridge(bridge.root) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        epochs = list(pool.map(Bridge.advance_epoch, peers))
    assert sorted(epochs) == list(range(1, 9))
    assert bridge.current_epoch() == 8
    with pytest.raises(BridgeError, match='STALE_PUBLISHER_EPOCH'):
        bridge.apply('edit', expected_epoch=7)
    assert not output.exists()
    assert bridge.apply('edit', expected_epoch=8)['state'] == 'APPLIED'


def test_status_and_cancel_skip_command_blobs(tmp_path, monkeypatch):
    bridge, _, _ = _prepare_patch(tmp_path)
    original_connect = bridge.connect

    @contextmanager
    def deny_blob_columns():
        with original_connect() as db:
            def authorizer(action, table, column, *_):
                if action == sqlite3.SQLITE_READ and table == 'commands' and column in {'before_blob', 'after_blob'}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorizer)
            yield db

    monkeypatch.setattr(bridge, 'connect', deny_blob_columns)
    assert bridge.status('edit')['state'] == 'PREPARED'
    assert bridge.cancel('edit')['state'] == 'CANCELLED'
