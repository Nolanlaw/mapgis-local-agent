from __future__ import annotations

import os
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
import argparse
import asyncio
import json
from pathlib import Path

from .core import Bridge, json_text
from .environment import capabilities
from .native_state import live_state
from .deletion import prepare_deletion


def main():
    parser = argparse.ArgumentParser(description="MapGIS native-file bridge; JSON arguments via a file")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("action", choices=["capabilities", "live", "inspect", "copy", "prepare", "delete", "apply", "status", "cancel", "recover", "tools", "call"])
    parser.add_argument("--arguments", type=Path)
    args = parser.parse_args()
    bridge = Bridge(args.root)
    params = json.loads(args.arguments.read_text(encoding="utf-8")) if args.arguments else {}
    try:
        if args.action in {"tools", "call"}:
            from .server import build_server
            server = build_server(bridge.root)
            if args.action == "tools":
                result = {"tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                                     for tool in asyncio.run(server.list_tools())]}
            else:
                if set(params) != {"tool", "arguments"} or not isinstance(params["tool"], str) or not isinstance(params["arguments"], dict):
                    raise ValueError("call expects {tool: registered MCP tool name, arguments: object}")
                raw = asyncio.run(server.call_tool(params["tool"], params["arguments"]))
                result = raw if isinstance(raw, dict) else json.loads(raw[0].text)
        elif args.action == "capabilities":
            result = capabilities(bridge.root)
        elif args.action == "live":
            result = live_state(bridge.root, **params)
        elif args.action == "delete":
            result = prepare_deletion(bridge, **params)
        else:
            result = getattr(bridge, args.action)(**params)
    except Exception as error:
        print(json_text({"error": type(error).__name__, "message": str(error)}))
        raise SystemExit(1)
    print(json_text(result))


if __name__ == "__main__":
    main()
