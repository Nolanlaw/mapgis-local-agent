"""Measure the v3 discovery and tools/list contracts in an isolated root."""
from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import version
import json
from math import ceil
from pathlib import Path
from statistics import median
import sys
from tempfile import TemporaryDirectory
from time import perf_counter_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from mcp.types import ListToolsResult

from mapgis_agent.mcp_surface import SURFACES
from mapgis_agent.operations import find_operations
from mapgis_agent.server import build_server


# Expected targets identify the query's task, including valid alternatives.
QUERIES = (
    ('B过程', ('topology.build_areas',)), ('拓扑线网造区', ('topology.build_areas',)),
    ('polygonize boundary', ('topology.build_areas', 'vector.process')),
    ('图切剖面', ('terrain.tin_profile', 'geology.borehole_chart')), ('TIN profile', ('terrain.tin_profile',)),
    ('section', ('terrain.tin_profile', 'geology.borehole_chart')),
    ('钻孔柱状图', ('geology.borehole_chart',)), ('borehole trajectory', ('geology.borehole_trajectory',)),
    ('drillhole column', ('geology.borehole_chart',)),
    ('标准分幅', ('frame.sheets',)), ('图框坐标', ('projection.frame',)),
    ('sheet scale', ('frame.sheets',)),
    ('化探异常', ('geochemistry.background', 'geochemistry.combination')),
    ('geochemistry background', ('geochemistry.background', 'geochemistry.background-statistics')),
    ('多元素组合异常', ('geochemistry.combination',)),
    ('动态分段', ('network.analysis',)), ('网络最短路径', ('network.analysis',)),
    ('network connectivity', ('network.analysis',)),
    ('CGCS2000', ('projection.point', 'projection.transform')),
    ('投影转换', ('projection.transform', 'projection.point')),
    ('projection CRS', ('projection.point', 'projection.transform')),
    ('影像融合', ('raster.fusion',)), ('pansharpening', ('raster.fusion',)),
    ('遥感监督分类', ('raster.supervised',)),
    ('DBS', ('library.seams', 'native.private_control')), ('分幅图库', ('library.seams', 'native.private_control')),
    ('library seams', ('library.seams',)),
    ('符号库', ('style.library',)), ('色表', ('style.library',)), ('symbol palette', ('style.library',)),
    ('DRC LVS', ('drc.audit',)), ('注记避让', ('labels.place',)),
    ('视觉预览', ('preview.render',)), ('FDR 黑匣子', ('fdr.export',)), ('ABI 安装探测', ('abi.probe',)),
    ('CGCS2000 原生图层 伴生契约', ('crs.modern_create',)),
    ('editable drillhole section', ('geology.section_workflow',)),
    ('验证宏 磁盘回读', ('ui.verified_macro',)),
    ('多图幅 字段字典', ('dbs.multisheet_validate',)),
    ('多图幅 图库索引账本', ('dbs.multisheet_build',)),
    ('图幅接边 中点缝合', ('dbs.multisheet_edge_match',)),
    ('跨图幅窗口 裁剪合并', ('dbs.multisheet_window',)),
    ('L2 probe projection block', ('native.l2_probe',)),
    ('MapSuvL67 survey WT header', ('native.survey_wt_normalize',)),
    ('ArcGIS Pro GPKG 交接', ('interchange.export',)),
    ('Surpac CorelDRAW 跨软件互操作', ('interchange.export',)),
    ('交接包核验 SHA-256', ('interchange.verify',)),
    ('MapGIS Provider 探测', ('provider.inspect',)),
)


async def _measure_surface(root: Path, surface: str) -> dict:
    server = build_server(root, surface=surface)
    tools = await server.list_tools()
    payload = ListToolsResult(tools=tools).model_dump_json(by_alias=True, exclude_none=True).encode('utf-8')
    fields = ('readOnlyHint', 'destructiveHint', 'idempotentHint', 'openWorldHint')
    annotated = sum(tool.annotations is not None and all(
        isinstance(getattr(tool.annotations, field), bool) for field in fields) for tool in tools)
    return {
        'tool_count': len(tools), 'tools_list_bytes': len(payload), 'estimated_tokens': ceil(len(payload) / 3.5),
        'annotated_tools': annotated, 'annotation_coverage': annotated / len(tools),
        'resources': len(await server.list_resources()), 'resource_templates': len(await server.list_resource_templates()),
        'prompts': len(await server.list_prompts()),
    }


def _measure_discovery(root: Path) -> dict:
    results = []
    for query, expected in QUERIES:
        timings = []
        for _ in range(5):
            started = perf_counter_ns()
            listing = find_operations(root, query, limit=5)
            timings.append((perf_counter_ns() - started) / 1_000_000)
        top = [item['id'] for item in listing['operations']]
        results.append({'query': query, 'expected_ids': list(expected), 'top5_ids': top,
                        'hit': bool(set(top).intersection(expected)), 'median_ms': round(median(timings), 4)})
    timings = sorted(item['median_ms'] for item in results)
    hits = sum(item['hit'] for item in results)
    return {'query_count': len(results), 'top5_hits': hits, 'top5_hit_rate': hits / len(results),
            'median_ms': round(median(timings), 4), 'p95_ms': timings[ceil(0.95 * len(timings)) - 1],
            'max_ms': timings[-1], 'queries': results}


def _violations(results: dict) -> list[str]:
    problems = []
    for surface, count in zip(SURFACES, (123, 15, 21), strict=True):
        row = results['surfaces'][surface]
        if row['tool_count'] != count or row['annotated_tools'] != count:
            problems.append(f'{surface} expected {count} fully annotated tools')
        if (row['resources'], row['resource_templates'], row['prompts']) != (2, 2, 5):
            problems.append(f'{surface} expected 2 resources, 2 templates and 5 prompts')
    for surface, bytes_limit, tokens_limit in (('compact', 10_500, 3_000), ('hybrid', 15_500, 4_429)):
        row = results['surfaces'][surface]
        if row['tools_list_bytes'] > bytes_limit or row['estimated_tokens'] > tokens_limit:
            problems.append(f'{surface} exceeded the tools/list budget')
    if hits := [item['query'] for item in results['discovery']['queries'] if not item['hit']]:
        problems.append('discovery misses: ' + ', '.join(hits))
    return problems


async def _benchmark(root: Path) -> dict:
    (root / 'config.local.json').write_text('{}', encoding='utf-8')
    return {
        'mcp_sdk_version': version('mcp'),
        'serialization': 'MCP ListToolsResult via model_dump_json(by_alias=True, exclude_none=True), UTF-8 bytes',
        'token_estimator': 'ceil(UTF-8 bytes / 3.5), deterministic and tokenizer-independent',
        'discovery_timing': '5 calls per query with a local config file, median per query',
        'surfaces': {surface: await _measure_surface(root / surface, surface) for surface in SURFACES},
        'discovery': _measure_discovery(root),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('work/v3-sprint2/surface-benchmark.json'))
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='surface-bench-', dir=output.parent) as folder:
        results = asyncio.run(_benchmark(Path(folder)))
    problems = _violations(results)
    results['violations'] = problems
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for surface, row in results['surfaces'].items():
        print(f"{surface} {row['tool_count']} tools, {row['tools_list_bytes']} bytes, "
              f"~{row['estimated_tokens']} tokens, {row['annotated_tools']}/{row['tool_count']} annotated, "
              f"{row['resources']} resources, {row['resource_templates']} templates, {row['prompts']} prompts")
    search = results['discovery']
    print(f"Top-5 {search['top5_hits']}/{search['query_count']}, "
          f"median {search['median_ms']:.4f} ms, p95 {search['p95_ms']:.4f} ms")
    print(output)
    if problems:
        raise SystemExit('\n'.join(problems))


if __name__ == '__main__':
    main()
