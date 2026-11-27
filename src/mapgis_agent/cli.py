from __future__ import annotations

import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
import argparse
import asyncio
import json
from pathlib import Path
import sys

from .sdk import MapGISClient, SURFACES


_LEGACY_ACTIONS = ('capabilities', 'live', 'inspect', 'copy', 'prepare', 'delete',
                   'apply', 'status', 'cancel', 'recover', 'tools', 'call')
_MODERN_ACTIONS = ('discover', 'plan', 'drc', 'interchange', 'doctor', 'providers', 'setup-snippet')


def stdio_command(root: Path, surface: str, *, python: Path | None = None, launcher: Path | None = None) -> dict:
    """Produce one argv vector, suitable for paths with spaces and non-ASCII text."""
    if launcher is None:
        candidate = Path(__file__).resolve().parents[2] / 'scripts/launch_mcp.py'
        launcher = candidate if candidate.is_file() else None
    entry = [str(launcher.resolve())] if launcher is not None else ['-m', 'mapgis_agent.server']
    return {'command': str(Path(python or sys.executable).resolve()),
            'args': ['-s', '-B', '-X', 'utf8', *entry, '--root', str(root.resolve()),
                     '--surface', surface, '--transport', 'stdio']}


def _run_modern(args, params):
    root = args.root or Path.cwd()
    surface = args.surface or 'hybrid'
    if not isinstance(params, dict):
        raise ValueError('Expected a JSON arguments object')
    if args.action == 'setup-snippet':
        if params or args.mode is not None:
            raise ValueError('Use --client, --surface and --root with setup-snippet')
        # Codex uses this object shape in TOML. The CLI always emits JSON.
        key = 'mcp_servers' if args.client == 'codex' else 'mcpServers'
        return {key: {'mapgis_local': stdio_command(root, surface)}}

    client = MapGISClient(root, surface)
    if args.action == 'discover':
        op_id = args.operation_id or params.pop('operation_id', None)
        if op_id is not None:
            if params or any(value is not None for value in (args.mode, args.query, args.module, args.limit)):
                raise ValueError('Use an operation ID by itself to request its description')
            return client.describe(op_id)
        for name, value in (('query', args.query if args.query is not None else args.mode),
                            ('module', args.module), ('limit', args.limit)):
            if value is not None:
                params[name] = value
        return client.discover(**params)
    if args.action == 'plan':
        action = _mode(args.mode, params, 'plan', {'plan', 'diff', 'apply', 'resume', 'import_mpj'})
        value_key = {'diff': 'current_spec', 'resume': 'plan_id', 'import_mpj': 'mpj_path'}.get(action, 'spec')
        if action in {'plan', 'apply'} and 'nodes' in params:
            return client.run_plan(action, params)
        if value_key not in params:
            raise ValueError(f'{action} requires {value_key} in --arguments')
        value = params.pop(value_key)
        return client.run_plan(action, value, **params)
    if args.action == 'interchange':
        action = _mode(args.mode, params, 'export', {'export', 'verify'})
        method = client.export_interchange if action == 'export' else client.verify_interchange
        return method(**params)
    if args.mode is not None:
        raise ValueError(f'{args.action} does not accept a positional mode')
    if args.action == 'drc':
        return client.audit_drc(**params)
    if args.action == 'providers':
        return {'providers': client.detect_providers(**params)}
    from .environment import capabilities

    return {'abi': client.probe_abi(**params), 'providers': client.detect_providers(),
            'capabilities': capabilities(client.root), 'surface': surface}


def _mode(explicit, params, default, allowed):
    value = params.pop('action', None)
    if explicit is not None and value is not None and explicit != value:
        raise ValueError('Positional mode and JSON action disagree')
    action = explicit if explicit is not None else default if value is None else value
    if action not in allowed:
        raise ValueError('Expected one of: ' + ', '.join(sorted(allowed)))
    return action


def main():
    parser = argparse.ArgumentParser(description="MapGIS local file tools, planning and MCP configuration")
    parser.add_argument("--root", type=Path, help='Task root (required by legacy commands)')
    parser.add_argument("--surface", choices=SURFACES)
    parser.add_argument("action", choices=_LEGACY_ACTIONS + _MODERN_ACTIONS)
    parser.add_argument("mode", nargs='?', help='Discovery query, plan action or interchange action')
    parser.add_argument("--arguments", type=Path, help='JSON arguments file')
    parser.add_argument('--query', help='Discovery search query')
    parser.add_argument('--module', help='Discovery module filter')
    parser.add_argument('--limit', type=int, help='Maximum discovery results')
    parser.add_argument('--operation-id', help='Describe a known operation ID')
    parser.add_argument('--client', choices=('auto', 'claude', 'cursor', 'codex', 'antigravity'), default='auto')
    args = parser.parse_intermixed_args()
    if args.action in _LEGACY_ACTIONS and (args.root is None or args.mode is not None):
        parser.error('legacy commands require --root and do not take a positional mode')
    try:
        params = json.loads(args.arguments.read_text(encoding='utf-8-sig')) if args.arguments else {}
        if args.action == 'setup-snippet':
            print(json.dumps(_run_modern(args, params), ensure_ascii=False, sort_keys=True))
            return

        from .core import Bridge, json_text

        if args.action in _MODERN_ACTIONS:
            print(json_text(_run_modern(args, params)))
            return
        bridge = Bridge(args.root)
        if args.action in {"tools", "call"}:
            from mcp.types import CallToolResult
            from .server import build_server
            server = build_server(bridge.root, surface=args.surface)
            if args.action == "tools":
                result = {"tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                                     for tool in asyncio.run(server.list_tools())]}
            else:
                if set(params) != {"tool", "arguments"} or not isinstance(params["tool"], str) or not isinstance(params["arguments"], dict):
                    raise ValueError("call expects {tool: registered MCP tool name, arguments: object}")
                raw = asyncio.run(server.call_tool(params["tool"], params["arguments"]))
                if isinstance(raw, CallToolResult):
                    result = raw.structuredContent if raw.structuredContent is not None else json.loads(raw.content[0].text)
                    if raw.isError:
                        print(json_text(result))
                        raise SystemExit(1)
                elif isinstance(raw, tuple):
                    result = raw[1]
                else:
                    result = raw if isinstance(raw, dict) else json.loads(raw[0].text)
        elif args.action == "capabilities":
            from .environment import capabilities

            result = capabilities(bridge.root)
        elif args.action == "live":
            from .native_state import live_state

            result = live_state(bridge.root, **params)
        elif args.action == "delete":
            from .deletion import prepare_deletion

            result = prepare_deletion(bridge, **params)
        else:
            result = getattr(bridge, args.action)(**params)
    except Exception as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, ensure_ascii=False, sort_keys=True))
        raise SystemExit(1)
    print(json_text(result))


if __name__ == "__main__":
    main()
