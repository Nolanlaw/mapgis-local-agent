"""Run each MapGIS-Bench task against three actual MCP SDK protocol endpoints."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import timedelta
from importlib.metadata import version
import json
from math import ceil
from pathlib import Path
import re
from statistics import mean
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns
import traceback

from mcp.shared.memory import create_connected_server_and_client_session

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'src'))
sys.path.insert(0, str(REPO))

from benchmarks.mapgis_bench.tasks import SEED, TASKS
from mapgis_agent.mcp_surface import SURFACES
from mapgis_agent.native_l2 import _LEGACY_PROFILES
from mapgis_agent.object_store import file_sha256
from mapgis_agent.providers import MapGIS67Provider
from mapgis_agent.server import build_server


LEVELS = ('basic', 'intermediate', 'expert')
NATIVE_CONFIG_KEYS = ('mapgis_installation', 'native_backend_legacy_dep_compatibility', 'native_general_private_ui')
_LEGACY_ERRORS = {'AUTHORIZATION_BOUNDARY': 'Writes require a project-owned work copy'}


class RouteUnavailable(Exception):
    """The requested operation has no supported top-level path on this surface."""


class CheckFailed(Exception):
    """An MCP response failed the task's protocol or error expectation."""


def _elapsed(started):
    return round((perf_counter_ns() - started) / 1_000_000, 4)


def _code(response, payload):
    if isinstance(payload, dict) and isinstance(payload.get('error_code'), str):
        return payload['error_code']
    content = ' '.join(item.text for item in response.content if item.type == 'text')
    for code, phrase in _LEGACY_ERRORS.items():
        if phrase in content:
            return code
    # Legacy FastMCP reports a text error wrapping the underlying exception.
    match = re.search(r'\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b', content)
    return match[0] if match else 'MCP_TOOL_ERROR'


class Case:
    def __init__(self, root, surface, session, tool_names, native):
        self.root = root
        self.surface = surface
        self.session = session
        self.tool_names = tool_names
        self.native = native
        self.calls = []
        self.blocks = []

    async def call(self, name, arguments, *, expected_error=None):
        if name not in self.tool_names:
            raise RouteUnavailable(name)
        started = perf_counter_ns()
        response = await self.session.call_tool(name, arguments)
        event = {'tool': name, 'ms': _elapsed(started)}
        if name in ('mapgis_execute', 'mapgis_inspect'):
            event['op_id'] = arguments['op_id']
        self.calls.append(event)
        payload = response.structuredContent
        if payload is None and response.content and response.content[0].type == 'text':
            try:
                payload = json.loads(response.content[0].text)
            except ValueError:
                pass
        if response.isError:
            code = _code(response, payload)
            event['error'] = code
            if code != expected_error:
                raise CheckFailed(f'{name} returned {code}')
            self.blocks.append(code)
            return payload
        if expected_error:
            raise CheckFailed(f'{name} accepted input expected to return {expected_error}')
        if not isinstance(payload, dict):
            raise CheckFailed(f'{name} returned no object payload')
        return payload

    async def route(self, direct_name, arguments, *, op=None, read=False, expected_error=None):
        if direct_name in self.tool_names or self.surface == 'legacy':
            return await self.call(direct_name, arguments, expected_error=expected_error)
        return await self.call('mapgis_inspect' if read else 'mapgis_execute',
                               {'op_id': op or direct_name, 'arguments': arguments}, expected_error=expected_error)

    async def describe(self, op):
        if self.surface == 'legacy':
            return await self.call('mapgis_describe_operation', {'operation_id': op})
        return await self.call('mapgis_discover', {'operation_id': op})

    async def discover(self, task):
        if self.surface == 'legacy':
            response = await self.call('mapgis_find_operations', {'query': task.query})
        else:
            response = await self.call('mapgis_discover', {'query': task.query, 'limit': 5})
        ids = [item['id'] for item in response['operations'][:5]]
        candidates = set(task.direct_tools) | ({task.required_tool} if task.required_tool else set())
        direct = sorted(self.tool_names.intersection(candidates))
        return {'hit': bool(direct or set(ids).intersection(task.expected_ops)),
                'top5_ids': ids, 'matching_listed_tools': direct, 'ms': self.calls[-1]['ms']}


def _configure(root, config):
    root.mkdir(parents=True, exist_ok=False)
    (root / 'config.local.json').write_text(json.dumps(config), encoding='utf8')


def _detect_native(root, config):
    observation = MapGIS67Provider().probe(root)
    saved_file = observation['capabilities']['native_saved_file']['available']
    l2_ready = False
    if saved_file:
        profile = _LEGACY_PROFILES['4x']
        library = Path(config['mapgis_installation']) / 'program' / profile['library']
        l2_ready = library.is_file() and file_sha256(library) == profile['sha256']
    return {'tier': observation['tier'], 'status': observation['status'], 'file': saved_file,
            'com': observation['capabilities']['com']['available'], 'l2': l2_ready}


def _failure_label(exc):
    if isinstance(exc, CheckFailed):
        return str(exc)
    frame = traceback.extract_tb(exc.__traceback__)[-1]
    return f'{type(exc).__name__} at {Path(frame.filename).name}:{frame.lineno} in {frame.name}'


async def _case(root, surface, task, config, tool_names, native):
    _configure(root, config)
    server = build_server(root, surface)
    async with create_connected_server_and_client_session(server, read_timeout_seconds=timedelta(seconds=150)) as session:
        case = Case(root, surface, session, tool_names, native)
        discovery = {'hit': False, 'top5_ids': [], 'matching_listed_tools': [], 'ms': None}
        wall_start = perf_counter_ns()
        detail = None
        discovered = False
        try:
            discovery = await case.discover(task)
            discovered = True
            start = perf_counter_ns()
            if task.required_tool and task.required_tool not in tool_names:
                raise RouteUnavailable(task.required_tool)
            if task.native_route and not native[task.native_route]:
                status = 'NATIVE_UNAVAILABLE'
                detail = f'Installed {task.native_route} route unavailable'
            else:
                await task.execute(case)
                if len(case.blocks) != task.safety_checks:
                    raise CheckFailed('Expected rejection count differs from observed rejections')
                status = 'PASS'
        except RouteUnavailable as exc:
            status, detail = 'UNSUPPORTED_ON_SURFACE', str(exc)
        except Exception as exc:
            status, detail = 'FAIL', _failure_label(exc)
        return {
            'id': task.id, 'level': task.level, 'title': task.title, 'query': task.query,
            'expected_ops': list(task.expected_ops), 'discovery': discovery,
            'status': status, 'detail': detail, 'native_route': task.native_route,
            'latency_ms': round(sum(call['ms'] for call in case.calls[1 if discovered else 0:]), 4),
            'execution_wall_ms': _elapsed(start) if discovered else None, 'wall_ms': _elapsed(wall_start),
            'mcp_calls': case.calls, 'safety_total': task.safety_checks, 'safety_intercepts': case.blocks,
        }


def _rate(count, total):
    return count / total if total else None


def _summary(rows):
    statuses = Counter(row['status'] for row in rows)
    total = len(rows)
    hits = sum(row['discovery']['hit'] for row in rows)
    tested = statuses['PASS'] + statuses['FAIL']
    latencies = sorted(row['latency_ms'] for row in rows if row['status'] in ('PASS', 'FAIL'))
    expected = sum(row['safety_total'] for row in rows)
    available = sum(row['safety_total'] for row in rows if row['status'] in ('PASS', 'FAIL'))
    blocked = sum(len(row['safety_intercepts']) for row in rows)
    return {'task_count': total, 'statuses': dict(statuses), 'discovery_hits': hits,
            'discovery_hit_rate': _rate(hits, total), 'pass_count': statuses['PASS'],
            'pass_rate': _rate(statuses['PASS'], total), 'executable_pass_rate': _rate(statuses['PASS'], tested),
            'latency_mean_ms': round(mean(latencies), 4) if latencies else None,
            'latency_p95_ms': latencies[ceil(.95 * len(latencies)) - 1] if latencies else None,
            'safety_intercepts': blocked, 'safety_total': expected,
            'safety_intercept_rate': _rate(blocked, expected),
            'available_safety_total': available, 'available_safety_intercept_rate': _rate(blocked, available)}


async def run_benchmark(root, *, config=None, tasks=TASKS, surfaces=SURFACES):
    """Run isolated task cases using a real ClientSession against each FastMCP server."""
    root = Path(root).resolve()
    config = {key: value for key, value in (config or {}).items() if key in NATIVE_CONFIG_KEYS}
    settings_root = root / 'provider-probe'
    _configure(settings_root, config)
    native = _detect_native(settings_root, config)
    results = {}
    for surface in surfaces:
        fixture = root / surface
        _configure(fixture, config)
        async with create_connected_server_and_client_session(build_server(fixture, surface)) as session:
            listing = await session.list_tools()
        if listing.nextCursor is not None:
            raise CheckFailed('Expected complete unpaginated tools/list')
        tools = {tool.name for tool in listing.tools}
        size = len(listing.model_dump_json(by_alias=True, exclude_none=True).encode('utf8'))
        cases = [await _case(fixture / task.id, surface, task, config, tools, native) for task in tasks]
        results[surface] = {
            'tools_count': len(tools), 'tools_list_bytes': size, 'estimated_tokens': ceil(size / 3.5),
            'overall': _summary(cases),
            'levels': {name: _summary([row for row in cases if row['level'] == name]) for name in LEVELS},
            'tasks': cases,
        }
    if 'legacy' in results:
        base = results['legacy']['tools_list_bytes']
        for surface in results.values():
            surface['compression_rate'] = 1 - surface['tools_list_bytes'] / base
    return {'schema': 'mapgis-bench/v1', 'mcp_sdk_version': version('mcp'), 'seed': SEED,
            'protocol': 'MCP ClientSession over connected in-memory server/client streams',
            'latency': 'Sum of actual execution call_tool round trips per task, excluding discovery and local fixture checks',
            'token_estimator': 'ceil(UTF-8 bytes / 3.5)',
            'native_routes': native, 'surfaces': results}


def _percent(value):
    return '—' if value is None else f'{value:.1%}'


def _ms(value):
    return '—' if value is None else f'{value:.2f}'


def summary_markdown(report):
    surfaces = report['surfaces']
    text = [
        '# MapGIS-Bench', '',
        'Every case uses fresh task-owned files. Calls go through MCP ClientSession using in-memory protocol streams.',
        'Discovery uses top-five catalog hits or a direct endpoint present in tools/list.',
        'Execution rate includes all tasks. Unsupported routes and unavailable native prerequisites have separate statuses.',
        'Timing sums execution RPCs per case in milliseconds.', '',
        '| Surface | Tools | tools/list bytes | Est. tokens | Reduction | Discovery | Execution | Mean ms | P95 ms | Safety checks |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |',
    ]
    for name, entry in surfaces.items():
        overall = entry['overall']
        text.append(f"| {name} | {entry['tools_count']} | {entry['tools_list_bytes']:,} | {entry['estimated_tokens']:,} | "
                    f"{_percent(entry.get('compression_rate'))} | {_percent(overall['discovery_hit_rate'])} | "
                    f"{overall['pass_count']}/{overall['task_count']} | {_ms(overall['latency_mean_ms'])} | "
                    f"{_ms(overall['latency_p95_ms'])} | {overall['safety_intercepts']}/{overall['safety_total']} |")
    text += ['', '## Results by difficulty', '',
             '| Surface | Level | Passed | Unsupported | Native unavailable | Failed |',
             '| --- | --- | ---: | ---: | ---: | ---: |']
    for name, entry in surfaces.items():
        for level, row in entry['levels'].items():
            status = row['statuses']
            text.append(f"| {name} | {level} | {row['pass_count']}/{row['task_count']} | "
                        f"{status.get('UNSUPPORTED_ON_SURFACE', 0)} | {status.get('NATIVE_UNAVAILABLE', 0)} | "
                        f"{status.get('FAIL', 0)} |")
    text += ['', '## Paired outcomes', '',
             '| ID | Task | ' + ' | '.join(surfaces) + ' |',
             '| --- | --- | ' + ' | '.join('---' for _ in surfaces) + ' |']
    for rows in zip(*(entry['tasks'] for entry in surfaces.values()), strict=True):
        task = rows[0]
        text.append(f"| {task['id']} | {task['title']} | " + ' | '.join(row['status'] for row in rows) + ' |')
    return '\n'.join(text) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('work/v3-sprint4/mapgis-bench-report.json'))
    parser.add_argument('--summary', type=Path, default=Path('work/v3-sprint4/mapgis-bench-summary.md'))
    args = parser.parse_args()
    base = REPO / 'work' / 'v3-sprint4'
    for path in (args.output.resolve(), args.summary.resolve()):
        if not path.is_relative_to(base.resolve()):
            parser.error('reports must be under work/v3-sprint4')
        path.parent.mkdir(parents=True, exist_ok=True)
    settings = REPO / 'config.local.json'
    config = json.loads(settings.read_text(encoding='utf-8-sig')) if settings.is_file() else {}
    with TemporaryDirectory(prefix='mapgis-bench-', dir=base) as folder:
        report = asyncio.run(run_benchmark(folder, config=config))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    summary = summary_markdown(report)
    args.summary.write_text(summary, encoding='utf8')
    for surface, row in report['surfaces'].items():
        print(f"{surface} {row['overall']['statuses']} {row['tools_count']} tools, {row['tools_list_bytes']} bytes")
    failures = [f"{surface}/{row['id']}: {row['detail']}" for surface, entry in report['surfaces'].items()
                for row in entry['tasks'] if row['status'] == 'FAIL' or (
                    row['status'] != 'PASS' and surface != 'legacy' and
                    not (row['status'] == 'NATIVE_UNAVAILABLE' and row['native_route'] in {'com', 'l2'}))]
    if failures:
        raise SystemExit('\n'.join(failures))


if __name__ == '__main__':
    main()
