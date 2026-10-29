"""Discovery must never turn an unimplemented native command into success."""
import asyncio

import pytest

from mapgis_agent.server import build_server


def test_discovery_is_available_through_the_real_mcp_server(tmp_path):
    server = build_server(tmp_path)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert {"mapgis_find_operations", "mapgis_describe_operation"} <= names


def test_catalog_status_does_not_imply_execution(tmp_path):
    from mapgis_agent.operations import find_operations, describe_operation

    found = find_operations(tmp_path, "矢量化")
    assert found["operations"]
    trace = describe_operation(tmp_path, "scan.interactive_trace")
    assert trace["validation"] == "DISCOVERED"
    assert trace["execution"]["kind"] == "host_sky"
    assert trace["execution"]["requires_observation"] is True
    assert trace["executable_by_mcp"] is False
    assert trace["help"]["file"] == "Mpedit60.hlp"
    assert trace["parameters"] and trace["output_checks"]


def test_unsupported_module_is_explicit(tmp_path):
    from mapgis_agent.operations import find_operations, describe_operation

    assert not find_operations(tmp_path, "", module="absent")["operations"]
    item = describe_operation(tmp_path, "style.library")
    assert item["availability"] == "NOT_CONFIGURED"
    assert item["validation"] == "DISCOVERED"
    assert item["executable_by_mcp"] is False
    with pytest.raises(ValueError, match="UNKNOWN_OPERATION"):
        describe_operation(tmp_path, "run_arbitrary_script")


def test_backend_description_keeps_legacy_workflow_and_distinguishes_the_backend(tmp_path):
    from mapgis_agent.operations import describe_operation

    item = describe_operation(tmp_path, "projection.point")
    assert item["executable_by_mcp"] is True
    assert item['execution']['tool']=='mapgis_projection_points'
    assert 'PROJ' in item['coverage_scope']
    assert item["workflow_knowledge"]["id"] == "projection"
    point = next(step for step in item["workflow_knowledge"]["steps"] if step["id"] == "point")
    assert any("500000" in check for check in point["checks"])
    terrain = describe_operation(tmp_path, "terrain.contours")["workflow_knowledge"]
    assert "Other models" in terrain["validation_scope"]
    assert any("快速" in step.get("limit", "") for step in terrain["steps"])


def test_catalog_backend_routes_are_real_and_exact_build_availability_is_explicit(tmp_path):
    from mapgis_agent.operations import _catalog,describe_operation
    tools={tool.name for tool in asyncio.run(build_server(tmp_path).list_tools())}
    for item in _catalog():
        if item['execution']['kind']=='mcp':assert item['execution']['tool'] in tools
    item=describe_operation(tmp_path,'native.workspace')
    assert item['availability']=='NOT_CONFIGURED'
    assert 'external GUI' in item['coverage_scope']
    wp=describe_operation(tmp_path,'native.feature.delete')
    assert 'WP' in wp['parameters']['file'] and 'unsupported' not in wp['scope']
