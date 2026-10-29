import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mapgis_agent.server import build_server


def test_operation_description_reuses_real_mcp_input_schema(tmp_path):
    server = build_server(tmp_path)

    async def check():
        tools = {tool.name: tool for tool in await server.list_tools()}
        raw = await server.call_tool("mapgis_describe_operation", {"operation_id": "file.inspect"})
        result = json.loads(raw[0].text)
        assert result["input_schema"] == tools["mapgis_inspect_file"].inputSchema
        assert result["execution_status"] == "MCP_TOOL_AVAILABLE"
        raw = await server.call_tool("mapgis_describe_operation", {"operation_id": "edit.points"})
        result = json.loads(raw[0].text)
        assert result["execution_status"] == "MCP_TOOL_AVAILABLE"
        assert result["input_schema"] == tools['mapgis_prepare_native_operation'].inputSchema
        raw = await server.call_tool("mapgis_describe_operation", {"operation_id": "style.library"})
        result = json.loads(raw[0].text)
        assert result["execution_status"] == "HOST_OBSERVATION_REQUIRED"
        assert result["executable_by_mcp"] is False
        assert "input_schema" not in result

    asyncio.run(check())


def test_cli_can_list_and_call_the_same_tools_and_report_errors(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/mapgis_cli.py"
    base = [sys.executable, "-s", "-B", "-X", "utf8", str(script), "--root", str(tmp_path)]
    listed = subprocess.run([*base, "tools"], capture_output=True, text=True, encoding="utf8")
    assert listed.returncode == 0, listed.stderr
    tools = json.loads(listed.stdout)["tools"]
    assert {"mapgis_get_request", "mapgis_read_features", "mapgis_prepare_file_operation"} <= {t["name"] for t in tools}
    arguments = tmp_path / "arguments.json"
    arguments.write_text(json.dumps({"tool": "mapgis_find_operations", "arguments": {"query": "读取"}}), encoding="utf8")
    called = subprocess.run([*base, "call", "--arguments", str(arguments)], capture_output=True, text=True, encoding="utf8")
    assert called.returncode == 0, called.stderr
    assert json.loads(called.stdout)["count"] > 0
    arguments.write_text(json.dumps({"tool": "arbitrary_script", "arguments": {}}), encoding="utf8")
    failed = subprocess.run([*base, "call", "--arguments", str(arguments)], capture_output=True, text=True, encoding="utf8")
    assert failed.returncode == 1
    assert "Unknown tool" in json.loads(failed.stdout)["message"]
