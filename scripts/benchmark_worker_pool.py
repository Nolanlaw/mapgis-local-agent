"""Measure the real packaged BAS60 worker and verify A→B→A isolation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from statistics import median, quantiles
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import psutil

from mapgis_agent.abi_profiles import probe_installation
from mapgis_agent.core import Bridge
from mapgis_agent.creation import prepare_creation
from mapgis_agent.native_backend import backend_configuration, native_file_info
from mapgis_agent.object_store import file_sha256
from mapgis_agent.worker_pool import NativeWorkerPool, STRATEGIES


def _fixtures(root):
    bridge = Bridge(root)
    crs = {'definition': 'EPSG:4326', 'axis_order': 'xy'}
    layers = []
    for name, points in (('a', [(120., 30.), (120.3, 30.4)]),
                         ('b', [(110., 25.), (110.2, 25.4), (110.6, 25.8)])):
        collection = {'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'id': str(index), 'properties': {'ID': index, 'sample': name},
             'geometry': {'type': 'Point', 'coordinates': [x, y]}}
            for index, (x, y) in enumerate(points, 1)]}
        prepare_creation(bridge, collection, {'name': f'{name}.wt', 'crs': crs, 'map_scale': 50_000}, name)
        layers.append(Path(bridge.apply(name)['output']))
    return layers


def run_benchmark(config_root, work_root, *, rounds=3):
    config_root, work_root = Path(config_root).resolve(), Path(work_root).resolve()
    executable, dll = backend_configuration(config_root)
    abi = probe_installation(dll.parent)
    if abi['route'] != 'EXACT_CERTIFIED':
        raise RuntimeError('Spike E requires the certified local four-module installation')
    work_root.mkdir(parents=True, exist_ok=True)
    config = json.loads((config_root / 'config.local.json').read_text(encoding='utf8'))
    (work_root / 'config.local.json').write_text(json.dumps({k: config[k] for k in
        ('mapgis_installation', 'native_backend_legacy_dep_compatibility')}, ensure_ascii=False), encoding='utf8')
    a, b = _fixtures(work_root)
    samples = {'A': a, 'B': b}
    source_hashes = {key: file_sha256(path) for key, path in samples.items()}
    results = {}
    for strategy in STRATEGIES:
        target = work_root / strategy
        with NativeWorkerPool(work_root, target, strategy=strategy, max_workers=2) as pool:
            prewarm = pool.prewarm([a, b]) if strategy == 'warm_pool' else []
            timing = []
            for sample in ['A', 'A', 'B', 'A'] * rounds:
                row = pool.run(samples[sample], expected_sha256=source_hashes[sample])
                timing.append({'sample': sample, 'pid': row['pid'], 'elapsed_ms': row['elapsed_ms'],
                               'reused_session': row['reused_session'], 'rss_mb': row['rss_mb']})
            # Each edit makes a real native save. A1 and A2 use separately owned
            # tasks and start from A's same immutable bytes.
            trace = []
            operations = [('A1', a, 'point.move', {'updates': [{'record_id': 1, 'x': 120.1, 'y': 30.1}]}),
                          ('B', b, 'feature.delete', {'record_ids': [2]}),
                          ('A2', a, 'point.move', {'updates': [{'record_id': 1, 'x': 120.1, 'y': 30.1}]})]
            for name, source, op, parameters in operations:
                digest = source_hashes[name[0]]
                row = pool.run(source, op, parameters, expected_sha256=digest)
                output = Path(row['output'])
                proven = native_file_info(work_root, str(output), row['sha256'])
                row['inspect_readback'] = {k: proven[k] for k in ('pid', 'native_bounds', 'active', 'private_desktop')}
                row['sample'] = name
                # Use stable, inspectable sample paths distinct from scratch logs.
                published = work_root / f'{strategy}-{name}.wt'
                shutil.copyfile(output, published)
                assert file_sha256(published) == row['sha256']
                row['output'] = str(published)
                trace.append(row)
        elapsed = [row['elapsed_ms'] for row in timing]
        children_closed = all(not psutil.pid_exists(row['pid']) for row in pool.started_processes)
        results[strategy] = {'prewarm': prewarm, 'inspect_requests': timing,
                             'median_inspect_ms': median(elapsed),
                             'p95_inspect_ms': quantiles(elapsed, n=20, method='inclusive')[-1],
                             'prewarm_total_ms': sum(row['startup_ms'] for row in prewarm),
                             'retirements': pool.retirements, 'buffer_processes_closed': children_closed,
                             'a_b_a': trace}

    cold_a = results['oneshot']['a_b_a'][0]
    invariants = ('sha256', 'bounds', 'active', 'record_ids', 'crs')
    checks, failures = {}, []
    for strategy, result in results.items():
        first, changed, last = result['a_b_a']
        equality = {field: first[field] == last[field] == cold_a[field] for field in invariants}
        equality['native_bounds'] = (first['inspect_readback']['native_bounds'] ==
                                     last['inspect_readback']['native_bounds'] ==
                                     cold_a['inspect_readback']['native_bounds'])
        equality['same_saved_bytes'] = Path(last['output']).read_bytes() == Path(cold_a['output']).read_bytes()
        equality['different_B_with_deletion'] = (changed['record_ids'] == [1, 3] and
                                               changed['active'] == 2 and changed['bounds'] != last['bounds'])
        equality['native_crs'] = cold_a['crs'] is not None and 'WGS 84' in cold_a['crs']
        equality['native_process_guard'] = all(event['private_desktop'] and event['job_guarded']
            and event['dep_flags'] == 0 for row in (first, changed, last) for event in row['native'])
        equality['buffer_processes_closed'] = result['buffer_processes_closed']
        equality['owned_switch'] = first['pid'] != changed['pid'] != last['pid']
        checks[strategy] = equality
        failures.extend(f'{strategy}.{name}' for name, passed in equality.items() if not passed)
    for strategy in ('sticky', 'warm_pool'):
        timings = results[strategy]['inspect_requests']
        if not (timings[1]['reused_session'] and timings[1]['pid'] == timings[0]['pid']):
            failures.append(f'{strategy}.same_file_pid_reuse')
    warm = results['warm_pool']
    if (len(warm['prewarm']) != 2 or not all(row['reused_session'] for row in warm['inspect_requests'])
            or len({row['pid'] for row in warm['inspect_requests']}) != 2):
        failures.append('warm_pool.prewarmed_file_bound_pid_reuse')
    if any(file_sha256(path) != source_hashes[key] for key, path in samples.items()):
        failures.append('source_changed')
    return {'schema_version': 'mapgis-worker-benchmark/v1', 'abi': abi,
            'runtime': {'worker_sha256': file_sha256(executable), 'bas60_sha256': file_sha256(dll),
                        'workspace_cross_file_rebind': False,
                        'pool_policy': 'Prewarmed file-bound children. Edits always retire the child, unchanged status may reuse it.',
                        'timing_scope': 'Sequential per-request wall time, excluding prewarm and independent post-edit readback.'},
            'samples': source_hashes, 'strategies': results, 'isolation_checks': checks, 'violations': failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('work/v3-sprint2/worker-benchmark.json'))
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='worker-benchmark-', dir=output.parent))
    report = run_benchmark(Path(__file__).resolve().parents[1], stage)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    for strategy, result in report['strategies'].items():
        print(f"{strategy}: {len(result['inspect_requests'])} reads, median {result['median_inspect_ms']:.3f} ms, "
              f"p95 {result['p95_inspect_ms']:.3f} ms, prewarm {result['prewarm_total_ms']:.3f} ms")
    print(f"A→B→A checks: {sum(sum(item.values()) for item in report['isolation_checks'].values())}/"
          f"{sum(map(len, report['isolation_checks'].values()))}")
    print(output)
    if report['violations']:
        raise SystemExit('\n'.join(report['violations']))


if __name__ == '__main__':
    main()
