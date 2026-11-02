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
        runtime=json.loads((await server.call_tool('mapgis_get_capabilities',{}))[0].text)['loaded_runtime']
        assert runtime['tool_count']==len(tools) and Path(runtime['server_module']).name=='server.py'
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
        assert result['execution_status']=='MCP_TOOL_AVAILABLE'
        assert result['input_schema']==tools['mapgis_prepare_system_library'].inputSchema
        raw = await server.call_tool("mapgis_describe_operation", {"operation_id": "scan.interactive_trace"})
        result = json.loads(raw[0].text)
        assert result["execution_status"] == "HOST_OBSERVATION_REQUIRED"
        assert result["executable_by_mcp"] is False
        assert "input_schema" not in result
        raw=await server.call_tool('mapgis_describe_operation',{'operation_id':'cartography.map_series'})
        result=json.loads(raw[0].text)
        assert result['input_schema']==tools['mapgis_prepare_map_series'].inputSchema
        assert result['availability']=='NOT_CONFIGURED' and result['workflow_knowledge']['id']=='cartography-series'
        raw=await server.call_tool('mapgis_describe_operation',{'operation_id':'geology.borehole_chart'})
        assert json.loads(raw[0].text)['availability']=='NOT_CONFIGURED'
        raw=await server.call_tool('mapgis_describe_operation',{'operation_id':'native.scene.tin.render'})
        result=json.loads(raw[0].text)
        assert result['input_schema']==tools['mapgis_prepare_native_tin_scene'].inputSchema
        assert result['workflow_knowledge']['id']=='native-tin-scene'

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


def test_description_reports_a_catalog_tool_missing_from_the_loaded_runtime(tmp_path,monkeypatch):
    import mapgis_agent.server as module
    original=module.describe_operation
    def changed_catalog(root,operation_id):
        item=original(root,operation_id)
        return item|{'execution':item['execution']|{'tool':'tool_from_a_later_runtime'}}
    server=build_server(tmp_path)
    monkeypatch.setattr(module,'describe_operation',changed_catalog)
    async def check():
        response=await server.call_tool('mapgis_describe_operation',{'operation_id':'file.inspect'})
        result=json.loads(response[0].text)
        assert result['execution_status']=='MCP_TOOL_NOT_LOADED'
        assert result['executable_by_mcp'] is False and 'input_schema' not in result
    asyncio.run(check())
