from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import Bridge, json_text
from .environment import capabilities
from .native_state import live_state


def main():
    parser = argparse.ArgumentParser(description="MapGIS native-file bridge; JSON arguments via a file")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("action", choices=["capabilities", "live", "inspect", "copy", "prepare", "apply", "status", "cancel", "recover"])
    parser.add_argument("--arguments", type=Path)
    args = parser.parse_args()
    bridge = Bridge(args.root)
    params = json.loads(args.arguments.read_text(encoding="utf-8")) if args.arguments else {}
    try:
        if args.action == "capabilities":
            result = capabilities(bridge.root)
        elif args.action == "live":
            result = live_state(bridge.root, **params)
        else:
            result = getattr(bridge, args.action)(**params)
    except Exception as error:
        print(json_text({"error": type(error).__name__, "message": str(error)}))
        raise SystemExit(1)
    print(json_text(result))


if __name__ == "__main__":
    main()
