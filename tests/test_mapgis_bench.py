"""The same cases exercise all listed MCP surfaces via real protocol sessions."""
import asyncio
from collections import Counter

from benchmarks.mapgis_bench.run_bench import run_benchmark, summary_markdown
from benchmarks.mapgis_bench.tasks import TASKS


def test_benchmark_contains_thirty_distinct_paired_tasks():
    assert len(TASKS) == len({task.id for task in TASKS}) == 30
    assert Counter(task.level for task in TASKS) == {'basic': 10, 'intermediate': 10, 'expert': 10}
    assert sum(task.safety_checks for task in TASKS) == 4
    assert {task.native_route for task in TASKS if task.native_route} == {'file', 'com', 'l2'}


def test_paired_mcp_benchmark_checks_execution_discovery_rejections_and_compression(tmp_path):
    chosen = {'B03', 'I02', 'I09', 'E02', 'E08', 'E09'}
    tasks = [task for task in TASKS if task.id in chosen]
    report = asyncio.run(run_benchmark(tmp_path, tasks=tasks))
    surfaces = report['surfaces']
    assert 'ClientSession' in report['protocol']
    assert [row['tools_count'] for row in surfaces.values()] == [123, 15, 21]
    assert 0 < surfaces['hybrid']['compression_rate'] < surfaces['compact']['compression_rate'] < 1

    for surface, entry in surfaces.items():
        rows = {task['id']: task for task in entry['tasks']}
        assert rows.keys() == chosen
        assert rows['B03']['status'] == rows['E09']['status'] == 'PASS'
        assert rows['B03']['safety_intercepts'] == ['AUTHORIZATION_BOUNDARY']
        assert rows['E09']['safety_intercepts'] == ['STALE_PUBLISHER_EPOCH']
        assert rows['E08']['status'] == 'NATIVE_UNAVAILABLE'
        assert all(task['mcp_calls'] and task['mcp_calls'][0]['tool'] in {'mapgis_discover', 'mapgis_find_operations'}
                   for task in rows.values())
        assert entry['overall']['latency_mean_ms'] > 0 and entry['overall']['latency_p95_ms'] > 0
        if surface == 'legacy':
            assert entry['overall']['statuses'] == {'PASS': 2, 'UNSUPPORTED_ON_SURFACE': 3, 'NATIVE_UNAVAILABLE': 1}
            assert entry['overall']['safety_intercepts'] == 2
            assert rows['I09']['status'] == 'UNSUPPORTED_ON_SURFACE'
        else:
            assert entry['overall']['statuses'] == {'PASS': 5, 'NATIVE_UNAVAILABLE': 1}
            assert rows['I02']['safety_intercepts'] == ['AUDIT_FALSE_EASTING']
            assert rows['I09']['discovery']['hit']
            assert any(call.get('op_id') == 'interchange.export' for call in rows['I09']['mcp_calls'])
            assert entry['overall']['safety_intercept_rate'] == 1.
    summary = summary_markdown(report)
    assert '## Paired outcomes' in summary
    assert '| NATIVE_UNAVAILABLE | NATIVE_UNAVAILABLE | NATIVE_UNAVAILABLE |' in summary
