"""Versioned operation discovery. A native menu entry is not an executable API."""
from __future__ import annotations

from copy import deepcopy
from functools import cache
import json
from pathlib import Path
import re


# Freeze descriptions with the Python revision that registered the tools.
# The legacy base remains byte-identical for already-running older servers;
# Only a runtime importing this revision discovers the additive family catalogs.
_CATALOG = [item for name in ('operations.json','volume-operations.json','raster-operations.json','exchange-operations.json','geochemistry-operations.json','database-operations.json','multimedia-operations.json','sdk-operations.json','private-control-operations.json')
            for item in json.loads(Path(__file__).with_name(name).read_text(encoding='utf8'))]
_SPRINT2 = {
    'drc.audit': ('mapgis_audit_check', 'cartography', '地质制图质检 / DRC LVS',
                  'Check topology, map/legend agreement, CRS and spatial accounting.'),
    'labels.place': ('mapgis_place_labels', 'cartography', 'WT 注记避让 / annotation placement',
                     'Propose editable native point.move updates and measure overlap.'),
    'preview.render': ('mapgis_render_preview', 'cartography', 'PNG 视觉预览 / visual preview',
                       'Render ordered saved layers, DRC crops, or a pixel comparison.'),
    'fdr.export': ('mapgis_export_fdr', 'artifact', 'FDR 黑匣子 / redacted evidence',
                   'Package checkpoint or recomputation evidence with structured redaction.'),
    'abi.probe': ('mapgis_probe_abi', 'native', 'ABI 安装探测 / native runtime profile',
                  'Compare installed x86 module digests with known compatibility profiles.'),
}
_CATALOG.extend({'id': identity, 'name': title, 'module': module, 'parameters': {},
                 'validation': scope, 'validation_scope': scope, 'op_version': '1.0',
                 'execution': {'kind': 'mcp', 'tool': tool, 'backend': 'sprint2'}}
                for identity, (tool, module, title, scope) in _SPRINT2.items())
_SPRINT3 = {
    'crs.modern_create': ('mapgis_prepare_modern_crs', 'projection', 'CGCS2000 原生图层及 CRS 伴生契约',
                          'Stage native WT/WL/WP and a SHA-256-bound WKT2 declaration as one artifact.', 'sidecar'),
    'section.drillhole_workflow': ('mapgis_prepare_drillhole_workflow', 'geology', '钻孔剖面与多道柱状图',
                                  'Prepare independent editable native MPJs from hash-bound drilling ledgers.', 'mapgis_native'),
    'ui.macro_run': ('mapgis_run_ui_macro', 'native', '私有桌面状态验证宏',
                     'Verify each action against a fresh observation and optionally reopen owned outputs.', 'mapgis_owned_private_ui'),
    'geology.section_workflow': ('mapgis_prepare_drillhole_workflow', 'geology', '钻孔剖面工作流与多道柱状图',
                                 'Build editable section and column MPJs with a hash-bound drillhole exchange ledger.', 'mapgis_native'),
    'ui.verified_macro': ('mapgis_run_ui_macro', 'native', '验证式 UI 宏及磁盘回读',
                          'Verify private UI preconditions, postconditions, hashes and native readback.', 'mapgis_owned_private_ui'),
    'dbs.multisheet_validate': ('mapgis_dbs_multisheet_validate', 'cartography', '多图幅字段字典及 CRS 校验',
                                'Check ordered native attribute dictionaries, false easting and standard sheet adjacency.', 'dbs'),
    'dbs.multisheet_edge_match': ('mapgis_dbs_multisheet_edge_match', 'cartography', '图幅接边报告及中点缝合',
                                  'Measure WL/WP seam gaps and attribute conflicts. Optionally write a midpoint branch.', 'dbs'),
    'dbs.multisheet_build': ('mapgis_dbs_multisheet_build', 'cartography', '多图幅图库索引账本',
                             'Persist standard sheet IDs, bounds, CRS contracts, input hashes and seam evidence.', 'dbs'),
    'dbs.multisheet_window': ('mapgis_dbs_multisheet_window', 'cartography', '多图幅窗口裁剪合并',
                              'Clip a window in a DBS ledger frame and export editable WT/WL/WP with source row lineage.', 'dbs'),
    'native.l2_probe': ('mapgis_probe_native_l2', 'native', 'L2 命名 DLL 投影参数只读探针',
                        'Decode a bounded MapGIS 4.x or 5.x projection block in an isolated exact-build x86 worker.', 'mapgis_native'),
    'native.survey_wt_normalize': ('mapgis_normalize_survey_wt', 'native', '数字测量 WT 文件头规范化',
                                   'Repair evidenced WT header offsets and scale on a fresh copy and reopen with BAS60.', 'mapgis_native'),
}
_CATALOG.extend({'id': identity, 'name': title, 'module': module, 'parameters': {},
                 'validation': scope, 'validation_scope': scope, 'op_version': '1.0',
                 'execution': {'kind': 'mcp', 'tool': tool, 'backend': backend}}
                for identity, (tool, module, title, scope, backend) in _SPRINT3.items())
_SPRINT4 = {
    'interchange.export': ('mapgis_interchange_export', 'exchange', '跨软件互操作交接包',
                           'Export hash-bound WT/WL/WP as ArcGIS Pro or QGIS GeoPackage (GPKG), Surpac STR and drillhole CSV, CorelDRAW DXF/SVG, or GeoSciML-Lite GeoJSON.'),
    'interchange.verify': ('mapgis_interchange_verify', 'exchange', '交接包哈希与文件回读核验',
                           'Independently reopen every interchange format and check output SHA-256, feature counts and XY bounds.'),
    'provider.inspect': ('mapgis_provider_inspect', 'environment', 'MapGIS 多代际 Provider 探测',
                         'Inspect MapGIS 6.7 ABI, COM and private desktop readiness, open headless dependencies, and local MapGIS 10.x installation evidence.'),
}
_CATALOG.extend({'id': identity, 'name': title, 'module': module, 'parameters': {},
                 'validation': scope, 'validation_scope': scope, 'op_version': '1.0',
                 'execution': {'kind': 'mcp', 'tool': tool, 'backend': 'interchange' if module == 'exchange' else 'provider'}}
                for identity, (tool, module, title, scope) in _SPRINT4.items())
_BY_ID = {item['id']: item for item in _CATALOG}

# Synonyms are bound to actual catalog entries. The ordered IDs favour the
# most direct operation within each concept while lexical matches still rank.
_CONCEPTS = (
    (('b过程', '拓扑造区', '造区', 'polygonize', 'polygonise'),
     ('topology.build_areas', 'vector.process', 'topology.node', 'topology.trim_dangles')),
    (('图切剖面', '剖面', 'section', 'profile'),
     ('terrain.tin_profile', 'section.drillhole_workflow', 'geology.borehole_chart', 'geology.apparent_dip', 'terrain.line_of_sight')),
    (('柱状图', '钻孔', 'borehole', 'drillhole', 'column'),
     ('geology.borehole_chart', 'geology.borehole_trajectory', 'geology.borehole_workbook')),
    (('标准分幅', '分幅', '图幅', '图框', 'sheet', 'frame'),
     ('frame.sheets', 'projection.frame', 'cartography.map_series')),
    (('化探', '异常', '地球化学', 'geochemistry', 'anomaly'),
     ('geochemistry.background', 'geochemistry.background-statistics', 'geochemistry.combination',
      'geochemistry.soil-grid', 'geochemistry.assay-qa', 'geochemistry.sediment-numbering')),
    (('动态分段', '网络', 'network', 'segmentation', 'connectivity'),
     ('network.analysis', 'native.private_control')),
    (('cgcs2000', '投影', '坐标系', 'projection', 'crs', 'epsg:4528', 'epsg:4549'),
     ('projection.transform', 'crs.modern_create', 'projection.point', 'projection.frame', 'scan.georeference')),
    (('影像融合', '遥感', 'raster', 'pansharpening', 'fusion'),
     ('raster.fusion', 'raster.processing', 'raster.supervised', 'raster.pca')),
    (('图库', 'dbs', 'library'), ('library.seams', 'native.private_control', 'style.library')),
    (('多图幅', '接边', '缝合', 'edge match', 'multisheet'),
     ('dbs.multisheet_edge_match', 'dbs.multisheet_build', 'dbs.multisheet_window', 'dbs.multisheet_validate')),
    (('窗口裁剪', '跨图幅窗口', 'window export'), ('dbs.multisheet_window',)),
    (('钻孔剖面', 'editable drillhole section'), ('geology.section_workflow', 'section.drillhole_workflow')),
    (('验证宏', 'ui macro', 'verified macro'), ('ui.verified_macro', 'ui.macro_run')),
    (('命名dll', 'l2 probe', 'projection block'), ('native.l2_probe',)),
    (('wt文件头', 'mapsuvl67', 'survey wt header'), ('native.survey_wt_normalize',)),
    (('符号库', '子图库', '色表', 'symbol', 'palette'), ('style.library', 'native.legend', 'style.annotation')),
    (('地质质检', '制图质检', 'drc', 'lvs'), ('drc.audit',)),
    (('注记避让', 'label', 'annotation'), ('labels.place',)),
    (('视觉预览', 'preview', '热图'), ('preview.render',)),
    (('飞行记录仪', '黑匣子', 'fdr'), ('fdr.export',)),
    (('安装探测', '进程兼容', 'abi'), ('abi.probe',)),
    (('跨软件', '互操作', 'arcgis pro', 'surpac', 'coreldraw', 'qgis gpkg', 'geosciml-lite'),
     ('interchange.export',)),
    (('交接包核验', 'interchange verify', 'verify interchange'), ('interchange.verify',)),
    (('provider', 'provider 探测', '多代际', 'mapgis 10.x'), ('provider.inspect',)),
)
_ALIAS_POSTINGS = {alias: {identity: max(5, 23 - 3 * rank) for rank, identity in enumerate(ids)}
                   for aliases, ids in _CONCEPTS for alias in aliases}
_DOCUMENTS = {
    item['id']: (item['id'].casefold(), item['name'].casefold(), item['module'].casefold(),
                 item['execution'].get('tool', '').casefold(), json.dumps(item, ensure_ascii=False).casefold())
    for item in _CATALOG
}

_EXAMPLE_HASH = 'a' * 64
_LINE_PATH = 'C:/mapgis-project/work/lines.wl'
_PROJECT_PATH = 'C:/mapgis-project/work/map.mpj'
_GEOGRAPHIC = {'definition': 'EPSG:4490', 'axis_order': 'xy'}
_PROJECTED = {'definition': 'EPSG:4549', 'axis_order': 'xy'}
_EXAMPLES = {
    'file.inspect': {'file': _LINE_PATH},
    'file.features': {'file': _LINE_PATH, 'expected_sha256': _EXAMPLE_HASH},
    'file.patch': {'file': _LINE_PATH, 'expected_sha256': _EXAMPLE_HASH, 'record_ids': [1],
                   'request_id': 'patch-01', 'style': {'color': 2}},
    'file.delete': {'file': _LINE_PATH, 'expected_sha256': _EXAMPLE_HASH, 'record_ids': [1],
                    'request_id': 'delete-01'},
    'file.compare': {'before': _LINE_PATH, 'after': 'C:/mapgis-project/work/revised.wl'},
    'topology.build_areas': {'operation_id': 'topology.polygonize', 'file': _LINE_PATH,
                             'expected_sha256': _EXAMPLE_HASH, 'request_id': 'areas-01',
                             'template': {'name': 'areas.wp', 'crs': _PROJECTED, 'map_scale': 1000},
                             'parameters': {'role_by_record': {'1': 'boundary'}}},
    'project.new': {'layers': [{'file': _LINE_PATH, 'expected_sha256': _EXAMPLE_HASH}],
                    'parameters': {'coordinate_frame': 'native'}, 'request_id': 'mpj-01'},
    'output.eps_tiff': {'file': _PROJECT_PATH, 'expected_sha256': _EXAMPLE_HASH, 'request_id': 'plot-01',
                        'view': {'native_bounds': [0, 0, 100, 100], 'paper_mm': [210, 297],
                                 'coordinate_frame': 'native'}},
    'projection.point': {'points': [[120, 30]], 'source_crs': _GEOGRAPHIC, 'target_crs': _PROJECTED},
    'frame.sheets': {'action': 'index', 'parameters': {'points': [[120, 30]], 'scale': 50000},
                     'geographic_crs': _GEOGRAPHIC},
    'native.private_control': {'action': 'policy'},
    'artifact.reconcile': {'request_id': 'artifact-01'},
    'native.reconcile': {'request_id': 'native-01'},
}


def _catalog() -> list[dict]:
    return list(_CATALOG)


@cache
def _workflow(relative: str) -> dict:
    name = Path(relative).name
    # Wheels carry the same canonical JSON via Hatch's force-include rule.
    folder = Path(__file__).with_name("workflows")
    if not folder.is_dir():
        folder = Path(__file__).resolve().parents[2] / "workflows"
    return json.loads((folder / name).read_text(encoding="utf-8"))


def _environment(root: str | Path) -> tuple[dict, Path | None, dict[str, str]]:
    config_path = Path(root) / "config.local.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    program = Path(config["mapgis_installation"]) / "program" if config.get("mapgis_installation") else None
    private_modules = {}
    if config.get('native_general_private_ui') is True:
        from .private_control import _profile
        private_modules = {value['executable'].casefold(): key for key, value in _profile()['modules'].items()}
    return config, program, private_modules


def _execution(item: dict, config: dict, program: Path | None, private_modules: dict[str, str]) -> dict:
    native = item["execution"]["kind"] == "host_sky"
    exe = program / item["executable"] if native and program else None
    installed = bool(exe and exe.is_file())
    availability = ("INSTALLED_LICENCE_UNCHECKED" if installed else
                    "NOT_INSTALLED" if program else "NOT_CONFIGURED") if native else "AVAILABLE"
    if item['id'].startswith("native.") or item["execution"].get("backend") == "mapgis_native":
        availability = ("NOT_CONFIGURED" if not program else
                        "LEGACY_HOST_DISABLED" if config.get("native_backend_legacy_dep_compatibility") is not True else
                        "EXACT_BUILD_AND_LICENCE_CHECKED_PER_CALL")
    if item['id'] in {'native.private_control', 'ui.macro_run', 'ui.verified_macro'}:
        availability=('PRIVATE_UI_AUTHORIZATION_REQUIRED' if config.get('native_general_private_ui') is not True else
                      'NOT_CONFIGURED' if not program else 'EXACT_BUILD_CHECKED_PER_OPEN')
    result = {'availability': availability, 'executable_path': str(exe) if installed else None,
              'executable_by_mcp': not native,
              'licence': 'Native module access must be checked in the actual session' if native else None}
    if native:
        match = private_modules.get(item['executable'].casefold())
        if match:
            result.update(reference_execution=item['execution'],
                execution={'kind':'mcp','tool':'mapgis_private_control','module':match,
                           'backend':'mapgis_owned_private_ui','requires_observation':True},
                executable_by_mcp=True,availability='EXACT_BUILD_CHECKED_PER_OPEN' if program else 'NOT_CONFIGURED')
    return result


def example_from_schema(schema: dict) -> dict:
    """Build a required-field skeleton from a registered tool schema."""
    definitions = schema.get('$defs', {})

    def value(spec: dict, key: str = '', seen: frozenset[str] = frozenset()):
        if reference := spec.get('$ref'):
            name = reference.rsplit('/', 1)[-1]
            return value(definitions[name], key, seen | {name}) if name in definitions and name not in seen else {}
        if 'const' in spec:
            return spec['const']
        if spec.get('enum'):
            return spec['enum'][0]
        if spec.get('examples'):
            return deepcopy(spec['examples'][0])
        variants = spec.get('anyOf', spec.get('oneOf', []))
        if variants:
            return value(next((variant for variant in variants if variant.get('type') != 'null'), variants[0]), key, seen)
        kind = spec.get('type', 'object')
        if kind == 'object':
            return {field: value(spec.get('properties', {}).get(field, {}), field, seen)
                    for field in spec.get('required', [])}
        if kind == 'array':
            return [value(spec.get('items', {}), key, seen) for _ in range(min(spec.get('minItems', 0), 10))]
        if kind in {'integer', 'number'}:
            return spec.get('minimum', spec.get('exclusiveMinimum', -1) + 1)
        if kind == 'string':
            if 'sha256' in key:
                return _EXAMPLE_HASH
            if key in {'file', 'source'}:
                return _LINE_PATH
            return 'example'.ljust(spec.get('minLength', 0), '0')
        return False if kind == 'boolean' else None

    return value(schema)


def add_operation_schema(detail: dict, schema: dict) -> dict:
    detail['input_schema'] = schema
    if detail['id'] not in _EXAMPLES:
        detail['example_arguments'] = example_from_schema(schema) | operation_defaults(detail)
    return detail


def operation_defaults(item: dict) -> dict:
    defaults = {}
    for key in ('operation_id', 'operation', 'part', 'action'):
        value = item['execution'].get(key, item['parameters'].get(key))
        if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*', value):
            defaults[key] = value
    module = item['execution'].get('module')
    if module:
        defaults['module'] = module
    return defaults


def describe_operation(root: str | Path, operation_id: str) -> dict:
    item = _BY_ID.get(operation_id)
    if item is None:
        raise ValueError(f"UNKNOWN_OPERATION: {operation_id}")
    result = deepcopy(item)
    result.update(_execution(item, *_environment(root)))
    result.update(op_version=item.get('op_version', '1.0'),
                  coverage_scope=item.get('validation_scope', item.get('scope', 'See output_checks and parameters')),
                  version='6.7; live editor requires exact supported build')
    result['preconditions'] = result.get('preconditions') or [
        'Bind the declared inputs and check the operation-specific data and coordinate requirements.'
    ]
    if operation_id in _EXAMPLES:
        result['example_arguments'] = deepcopy(_EXAMPLES[operation_id])
    elif result['executable_by_mcp']:
        name = result['execution']['tool']
        if operation_id in _SPRINT2:
            from .mcp_surface import _sprint2_tools
            schema = _sprint2_tools(Path(root))[name].parameters
        elif operation_id in _SPRINT3:
            from .mcp_surface import _sprint3_tools
            schema = _sprint3_tools(Path(root))[name].parameters
        elif operation_id in _SPRINT4:
            from .mcp_surface import _sprint4_tools
            schema = _sprint4_tools(Path(root))[name].parameters
        else:
            from .server import registered_input_schemas
            schema = registered_input_schemas().get(name)
        result['example_arguments'] = (example_from_schema(schema) | operation_defaults(result)) if schema else {}
    else:
        result['example_arguments'] = {
            key: value if isinstance(value, (dict, list, int, float, bool)) else '<' + key + '>'
            for key, value in result['parameters'].items()
        }
    if item.get("workflow"):
        result["workflow_knowledge"] = deepcopy(_workflow(item["workflow"]))
    return result


def _concept_postings(term: str) -> dict[str, int]:
    if term in _ALIAS_POSTINGS:
        return _ALIAS_POSTINGS[term]
    postings = {}
    if re.search(r'[\u3400-\u9fff]', term):
        for alias, matches in _ALIAS_POSTINGS.items():
            if alias in term and re.search(r'[\u3400-\u9fff]', alias):
                for identity, score in matches.items():
                    postings[identity] = max(score, postings.get(identity, 0))
    return postings


def _rank(item: dict, terms: list[tuple[str, dict[str, int]]], phrase: str) -> int | None:
    identity, title, module, tool, body = _DOCUMENTS[item['id']]
    score = 0
    for term, synonyms in terms:
        lexical = next((weight for text, weight in ((title, 14), (identity, 12), (module, 8), (tool, 6), (body, 2))
                        if term in text), 0)
        semantic = synonyms.get(identity, 0)
        if not (lexical or semantic):
            return None
        score += lexical + semantic
    if phrase:
        score += 100 if phrase == identity else 10 if phrase in title else 0
    return score


def find_operations(root: str | Path, query: str = "", module: str | None = None, limit: int | None = None) -> dict:
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError('INVALID_LIMIT: limit must be a nonnegative integer')
    phrase = query.casefold().strip()
    terms = [(term, _concept_postings(term)) for term in phrase.split()]
    matches = []
    for item in _CATALOG:
        if not module or item['module'] == module:
            score = _rank(item, terms, phrase)
            if score is not None:
                matches.append((score, item))
    matches.sort(key=lambda pair: pair[0], reverse=True)
    config, program, private_modules = _environment(root)
    results = []
    for _, item in matches[:limit]:
        execution = _execution(item, config, program, private_modules)
        results.append({key: item[key] for key in ('id', 'name', 'module', 'validation')} |
                       {key: execution[key] for key in ('availability', 'executable_by_mcp')})
    result = {'operations': results, 'count': len(results),
              'status_note': 'DISCOVERED describes a task and its native help; it does not claim execution or licence availability.'}
    if limit is not None:
        result['total'] = len(matches)
    return result
