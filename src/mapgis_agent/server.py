from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core import Bridge
from .environment import capabilities
from .native_state import live_state


def build_server(root: str | Path) -> FastMCP:
    bridge = Bridge(root)
    server = FastMCP("MapGIS 6.7 本机原生文件工具")

    @server.tool()
    def mapgis_get_capabilities() -> dict:
        """Report local scope, installation, processes and exact-build live-state prerequisites; discovery is not a live acceptance test."""
        return capabilities(bridge.root)

    @server.tool()
    def mapgis_get_live_state(pid: int) -> dict:
        """Read exact-build 6.7 project/layer paths, selection pools and unsaved flags. Read-only. Active MDI identity requires independent Sky observation; never guesses it from view count."""
        return live_state(bridge.root, pid)

    @server.tool()
    def mapgis_inspect_file(file: str, offset: int = 0, limit: int = 10) -> dict:
        """Read native WT/WL/WP geometry, attributes and style. record_id is physical 1-based, not attribute ID; no GUI selection read."""
        return bridge.inspect(file, offset, limit)

    @server.tool()
    def mapgis_make_work_copy(source: str, request_id: str) -> dict:
        """Copy a native vector into a new owned workspace, preserving original bytes. Repeated ID reconciles the same copy."""
        return bridge.copy(source, request_id)

    @server.tool()
    def mapgis_prepare_patch(file: str, expected_sha256: str, record_ids: list[int], request_id: str,
                             attributes: dict | None = None, style: dict | None = None) -> dict:
        """Preview fixed-field edits to explicit records on an owned copy. Preserves geometry, annotation auxiliary sections and every untargeted byte. No source write."""
        return bridge.prepare(file, expected_sha256, record_ids, request_id, attributes, style)

    @server.tool()
    def mapgis_apply_patch(request_id: str) -> dict:
        """Apply a prepared patch to a NEW output branch after source hash recheck. Idempotent; does not overwrite originals or live documents."""
        return bridge.apply(request_id)

    @server.tool()
    def mapgis_get_request(request_id: str) -> dict:
        """Inspect the persisted request and output hash. Reconcile this state after interruption before retrying."""
        return bridge.status(request_id)

    @server.tool()
    def mapgis_cancel_request(request_id: str) -> dict:
        """Cancel an unapplied preview. Refuses to cancel an applied or uncertain output."""
        return bridge.cancel(request_id)

    @server.tool()
    def mapgis_prepare_recovery(applied_request: str, later_file: str, expected_sha256: str, request_id: str) -> dict:
        """Prepare a NEW recovery branch reverting only this request's fields. Uses compatible spans or a guarded mapping of unchanged physical records after native layout changes. Preserves later independent edits; same-field, deleted, renumbered or changed-geometry conflicts fail. Not universal native Undo."""
        return bridge.recover(applied_request, later_file, expected_sha256, request_id)

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    build_server(args.root).run(transport="stdio")


if __name__ == "__main__":
    main()
