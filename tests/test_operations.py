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
    item = describe_operation(tmp_path, "terrain.dtm")
    assert item["availability"] == "NOT_CONFIGURED"
    assert item["validation"] == "PARTIAL"
    assert "Fast TIN crashed" in item["validation_scope"]
    assert item["executable_by_mcp"] is False
    with pytest.raises(ValueError, match="UNKNOWN_OPERATION"):
        describe_operation(tmp_path, "run_arbitrary_script")


def test_native_description_includes_the_workflow_without_claiming_mcp_execution(tmp_path):
    from mapgis_agent.operations import describe_operation

    item = describe_operation(tmp_path, "projection.point")
    assert item["executable_by_mcp"] is False
    assert item["workflow_knowledge"]["id"] == "projection"
    point = next(step for step in item["workflow_knowledge"]["steps"] if step["id"] == "point")
    assert any("500000" in check for check in point["checks"])
    terrain = describe_operation(tmp_path, "terrain.contours")["workflow_knowledge"]
    assert "Other models" in terrain["validation_scope"]
    assert any("快速" in step.get("limit", "") for step in terrain["steps"])
