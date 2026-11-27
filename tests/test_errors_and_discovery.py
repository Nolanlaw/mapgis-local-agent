import json
from pathlib import Path
import sqlite3

import pytest

from mapgis_agent.core import BridgeError
from mapgis_agent.errors import ErrorCategory, classify_error
from mapgis_agent.operations import describe_operation, example_from_schema, find_operations


@pytest.mark.parametrize(('exc', 'code', 'category'), [
    (BridgeError('STALE_SOURCE: changed'), 'STALE_SOURCE', ErrorCategory.STALE_CONFLICT),
    (BridgeError('STALE_PUBLISHER_EPOCH: active epoch is 3'), 'STALE_PUBLISHER_EPOCH', ErrorCategory.PUBLISHER_FENCING),
    (BridgeError('WARP_SOURCE_CRS_REQUIRED'), 'GEODETIC_CONTRACT', ErrorCategory.GEODETIC_CONTRACT),
    (BridgeError('EXPLICIT_CRS: invalid caller input'), 'EXPLICIT_CRS', ErrorCategory.GEODETIC_CONTRACT),
    (BridgeError('GEOREFERENCE_NOT_ACCEPTED'), 'GEOREFERENCE_NOT_ACCEPTED', ErrorCategory.DOMAIN_PRECONDITION),
    (BridgeError('Writes require a project-owned work copy'), 'AUTHORIZATION_BOUNDARY', ErrorCategory.AUTHORIZATION_BOUNDARY),
    (BridgeError('ARTIFACT_INCOMPLETE: no replay'), 'ARTIFACT_INCOMPLETE', ErrorCategory.ARTIFACT_STATE),
    (BridgeError('PLAN_CYCLE: dependency graph'), 'PLAN_CYCLE', ErrorCategory.VALIDATION_ERROR),
    (BridgeError('PLAN_CHECKPOINT_CORRUPT'), 'PLAN_CHECKPOINT_CORRUPT', ErrorCategory.ARTIFACT_STATE),
    (BridgeError('FDR_LEAK: unmapped value'), 'FDR_LEAK', ErrorCategory.AUTHORIZATION_BOUNDARY),
    (BridgeError('DBS_FALSE_EASTING: offset differs'), 'DBS_FALSE_EASTING', ErrorCategory.GEODETIC_CONTRACT),
    (BridgeError('DBS_ADJACENCY: disconnected'), 'DBS_ADJACENCY', ErrorCategory.DOMAIN_PRECONDITION),
    (BridgeError('L2_PARAMETER_BLOCK: wrong size'), 'L2_PARAMETER_BLOCK', ErrorCategory.VALIDATION_ERROR),
    (BridgeError('NATIVE_L2_READBACK: native mismatch'), 'NATIVE_L2_READBACK', ErrorCategory.NATIVE_RUNTIME),
    (BridgeError('SURVEY_WT_LAYOUT: invalid section table'), 'SURVEY_WT_LAYOUT', ErrorCategory.DOMAIN_PRECONDITION),
    (BridgeError('PRIVATE_UI_REQUEST_PENDING: protected'), 'NATIVE_RUNTIME', ErrorCategory.NATIVE_RUNTIME),
    (BridgeError('INTERCHANGE_INTEGRITY: a package member changed'), 'INTERCHANGE_INTEGRITY',
     ErrorCategory.ARTIFACT_STATE),
    (BridgeError('INTERCHANGE_WRITE_ROOT: unsafe location'), 'INTERCHANGE_WRITE_ROOT',
     ErrorCategory.AUTHORIZATION_BOUNDARY),
    (BridgeError('Unknown request_id'), 'UNKNOWN_REQUEST', ErrorCategory.NOT_FOUND),
    (ValueError('invalid'), 'VALIDATION_ERROR', ErrorCategory.VALIDATION_ERROR),
    (RuntimeError('private: do not expose'), 'INTERNAL_ERROR', ErrorCategory.INTERNAL_ERROR),
])
def test_error_families_and_legacy_identity(exc, code, category):
    result = classify_error(exc)
    assert result['error_code'] == code and result['category'] == category
    assert isinstance(result['retryable'], bool)
    assert set(result['recovery']) == {'suggested_tool', 'suggested_operation', 'hint'}
    assert issubclass(BridgeError, ValueError)


def test_error_envelope_does_not_return_detail_or_cause():
    try:
        try:
            raise PermissionError(r'C:\private\example-token')
        except PermissionError as cause:
            raise BridgeError('STALE_SOURCE: secret-example') from cause
    except BridgeError as exc:
        raw = json.dumps(classify_error(exc))
    assert 'STALE_SOURCE' in raw
    assert all(secret not in raw for secret in ('Traceback', 'secret-example', 'example-token'))
    assert classify_error(sqlite3.OperationalError('database is locked'))['retryable']


def test_list_reads_config_once_and_never_expands_workflows(tmp_path, monkeypatch):
    config = tmp_path / 'config.local.json'
    config.write_text('{}', encoding='utf-8')
    read_text = Path.read_text
    reads = []

    def record(path, *args, **kwargs):
        reads.append(path)
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', record)
    listing = find_operations(tmp_path)
    assert listing['count'] > 150 and reads == [config]
    assert all('workflow_knowledge' not in item for item in listing['operations'])
    assert next(item for item in listing['operations'] if item['id'] == 'native.workspace')[
        'availability'] == 'NOT_CONFIGURED'


@pytest.mark.parametrize(('query', 'expected'), [
    ('B过程', 'topology.build_areas'), ('topology polygonize', 'topology.build_areas'),
    ('图切剖面', 'terrain.tin_profile'), ('borehole', 'geology.borehole_chart'),
    ('标准分幅', 'frame.sheets'), ('geochemistry', 'geochemistry.background'),
    ('动态分段', 'network.analysis'), ('network connectivity', 'network.analysis'),
    ('CGCS2000 projection', 'projection.point'),
    ('影像融合', 'raster.fusion'), ('DBS', 'library.seams'), ('symbol', 'style.library'),
    ('图幅接边 中点缝合', 'dbs.multisheet_edge_match'), ('跨图幅窗口', 'dbs.multisheet_window'),
    ('editable drillhole section', 'geology.section_workflow'), ('验证宏', 'ui.verified_macro'),
    ('L2 probe projection block', 'native.l2_probe'), ('MapSuvL67 survey WT header', 'native.survey_wt_normalize'),
    ('ArcGIS Pro GPKG 交接', 'interchange.export'), ('交接包核验 SHA-256', 'interchange.verify'),
    ('MapGIS Provider 探测', 'provider.inspect'),
])
def test_ranked_bilingual_search(query, expected, tmp_path):
    found = find_operations(tmp_path, query, limit=5)
    assert expected in {item['id'] for item in found['operations']}
    assert found['count'] == min(found['total'], 5)


def test_limit_module_and_describe_examples(tmp_path):
    limited = find_operations(tmp_path, 'profile', module='terrain', limit=1)
    assert [item['id'] for item in limited['operations']] == ['terrain.tin_profile']
    assert limited['total'] >= limited['count'] == 1
    assert find_operations(tmp_path, 'profile', limit=0)['operations'] == []
    with pytest.raises(ValueError, match='INVALID_LIMIT'):
        find_operations(tmp_path, limit=-1)
    description = describe_operation(tmp_path, 'projection.point')
    from mapgis_agent.cartography import projection_points
    result = projection_points(**description['example_arguments'])
    assert result['points'][0][0] == pytest.approx(500000)
    assert description['op_version'] == '1.0' and description['preconditions']
    assert description['workflow_knowledge']['id'] == 'projection'


def test_example_skeleton_resolves_required_typed_fields():
    schema = {'type': 'object', 'required': ['mode', 'input'], 'properties': {
        'mode': {'enum': ['read', 'write']}, 'input': {'$ref': '#/$defs/File'}},
        '$defs': {'File': {'type': 'object', 'required': ['expected_sha256', 'offset'], 'properties': {
            'expected_sha256': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 1}}}}}
    assert example_from_schema(schema) == {'mode': 'read', 'input': {'expected_sha256': 'a' * 64, 'offset': 1}}
