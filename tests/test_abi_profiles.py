import json
from pathlib import Path
import struct

import pytest
from shapely.geometry import Point

from mapgis_agent.abi_profiles import CORE_MODULES, default_profile, probe_installation
from mapgis_agent.native_state import PROFILE
from mapgis_agent.object_store import file_sha256
from mapgis_agent.worker_pool import NativeWorkerPool
from test_drc_lvs import write_layer
from test_native_raster import ROOT, native_settings


def _pe(path):
    binary = bytearray(160)
    binary[:2] = b'MZ'
    struct.pack_into('<I', binary, 60, 64)
    binary[64:68] = b'PE\0\0'
    struct.pack_into('<HH', binary, 68, 0x14c, 1)
    struct.pack_into('<HH', binary, 86, 0x2002 if path.suffix == '.dll' else 2, 0x10b)
    path.write_bytes(binary)


def test_tier_matrix_requires_digests_and_observed_golden_fixtures(tmp_path):
    assert probe_installation(tmp_path)['tier'] == 'D'
    program = tmp_path / 'installation' / 'program'
    program.mkdir(parents=True)
    for name in CORE_MODULES:
        _pe(program / name)
    assert probe_installation(program)['route'] == 'COM_UI_SAFE_MODE'
    golden = {'fixtures': [{'expected_count': 2, 'opened_count': 2, 'resaved_count': 2, 'reopened_count': 2,
                            'opened': True, 'resaved': True, 'reopened': True}]}
    candidate = probe_installation(program.parent, golden_probe=lambda *_: golden)
    assert candidate['tier'] == 'B' and candidate['capabilities']['native_saved_file']
    assert not candidate['capabilities']['raw_memory_offsets']
    golden['fixtures'][0]['reopened_count'] = 1
    assert probe_installation(program, golden_probe=lambda *_: golden)['tier'] == 'C'
    profiles = tmp_path / 'registered'
    profiles.mkdir()
    external = default_profile() | {'profile_id': 'registered-test',
                                    'modules': {name: file_sha256(program / name) for name in CORE_MODULES}}
    (profiles / 'test.json').write_text(json.dumps(external), encoding='utf8')
    result = probe_installation(program, profiles)
    assert result['tier'] == 'A' and result['profile_id'] == 'registered-test'
    assert result['capabilities']['raw_memory_offsets']


def test_installed_mapgis_matches_the_external_abi_profile(tmp_path):
    _, bas = native_settings(tmp_path)
    result = probe_installation(bas.parent)
    assert result['tier'] == 'A' and result['route'] == 'EXACT_CERTIFIED'
    assert result['modules'] == PROFILE == default_profile()['modules']
    assert default_profile()['layout']['view']['project_pointer'] == 0x645


def test_three_real_worker_strategies_and_a_b_a(tmp_path):
    native_settings(tmp_path)
    from scripts.benchmark_worker_pool import run_benchmark
    report = run_benchmark(ROOT, tmp_path / 'benchmark', rounds=1)
    assert report['violations'] == []
    assert all(all(result.values()) for result in report['isolation_checks'].values())
    native = report['strategies']['warm_pool']
    first, edited, repeated = native['a_b_a']
    assert first['sha256'] == repeated['sha256']
    assert first['pid'] != repeated['pid']
    assert edited['record_ids'] == [1, 3]
    assert all(row['exit_code'] == 0 for row in native['retirements'])


@pytest.mark.parametrize(('limits', 'reason'), [
    ({'max_tasks_per_worker': 1}, 'task_limit'),
    ({'max_rss_mb': .01}, 'rss_limit'),
])
def test_warm_worker_recycles_owned_process_at_limits(tmp_path, limits, reason):
    native_settings(tmp_path)
    file = write_layer(tmp_path, 'points.wt', [Point(11, 12), Point(14, 15)])
    with NativeWorkerPool(tmp_path, tmp_path / 'pool', **limits) as pool:
        first, second = pool.run(file), pool.run(file)
        assert first['pid'] != second['pid'] and first['sha256'] == second['sha256']
        assert any(row['reason'] == reason for row in pool.retirements)
    assert all(row['exit_code'] == 0 for row in pool.retirements)
