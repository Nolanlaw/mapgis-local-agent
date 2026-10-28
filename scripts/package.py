"""Explicit source bundle, actual restore/hash check and fresh stdio startup."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tomllib
import uuid
import zipfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from package_share import public_sources

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


async def main():
    payloads = {}
    for name, source in sorted(public_sources().items()):
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError(f"Not a project-owned ordinary source file: {source}")
        payloads[name] = source.read_bytes()
    manifest = {name: {"bytes": len(data), "sha256": digest(data)} for name, data in payloads.items()}
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    archive = ROOT / f"deliverables/mapgis-local-agent-{version}.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in payloads.items():
            zf.writestr(name, data)
        zf.writestr("bundle-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    restore = ROOT / "work" / ("package-restore-" + uuid.uuid4().hex[:8])
    restore.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        if zf.testzip() is not None:
            raise RuntimeError("Archive CRC check failed")
        for info in zf.infolist():
            target = (restore / info.filename).resolve()
            if not target.is_relative_to(restore.resolve()):
                raise RuntimeError("Archive path escapes restore directory")
            zf.extract(info, restore)
    for name, expected in manifest.items():
        actual = (restore / name).read_bytes()
        if len(actual) != expected["bytes"] or digest(actual) != expected["sha256"]:
            raise RuntimeError(f"Restored source mismatch: {name}")
    params = StdioServerParameters(command=sys.executable,
        args=["-s", "-B", "-X", "utf8", str(restore / "scripts/launch_mcp.py"), "--root", str(restore)])
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = [tool.name for tool in (await session.list_tools()).tools]
            expected_tools = {"mapgis_get_capabilities", "mapgis_get_live_state", "mapgis_inspect_file",
                              "mapgis_make_work_copy", "mapgis_prepare_patch", "mapgis_apply_patch",
                              "mapgis_get_request", "mapgis_cancel_request", "mapgis_prepare_recovery",
                              "mapgis_find_operations", "mapgis_describe_operation", "mapgis_search_native_help",
                              "mapgis_register_scan", "mapgis_scan_window", "mapgis_prepare_georeference",
                              "mapgis_write_georeferenced_window", "mapgis_extract_candidates",
                              "mapgis_merge_candidate_tiles", "mapgis_prepare_creation", "mapgis_prepare_vector_operation",
                              "mapgis_prepare_delete", "mapgis_read_features", "mapgis_prepare_file_operation",
                              "mapgis_compare_files", "mapgis_verify_request_output",
                              "mapgis_native_file_info", "mapgis_prepare_native_operation", "mapgis_prepare_native_plot", "mapgis_reconcile_native_request"}
            if len(tools) != len(expected_tools) or set(tools) != expected_tools:
                raise RuntimeError("Restored server tool inventory mismatch")
            response = await session.call_tool("mapgis_get_capabilities", {})
            if response.isError:
                raise RuntimeError(f"Restored server call failed: {response.content}")
    # Keep the verified restore until the owner performs explicitly scoped,
    # recoverable lifecycle cleanup. Packaging never permanently deletes it.
    receipt = {"archive": str(archive), "archive_sha256": digest(archive.read_bytes()),
               "archive_bytes": archive.stat().st_size, "source_files": len(manifest),
               "source_logical_bytes": sum(x["bytes"] for x in manifest.values()),
               "actual_restore_hashes": True, "fresh_restored_stdio_tools": tools,
               "fresh_restored_call": "mapgis_get_capabilities", "verified_restore": str(restore),
               "temporary_restore_removed": False,
               "new_machine_dependency_installation": "NOT_VERIFIED",
               "excluded": ["MapGIS", "licence helper", "local configuration", "course data", "private evidence", ".venv", ".git", "upstream historical archive"]}
    receipt_file = ROOT / "evidence/package-verification.json"
    receipt_file.parent.mkdir(parents=True, exist_ok=True)
    receipt_file.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
