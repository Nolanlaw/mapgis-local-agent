"""MapGIS 6.7 native-file bridge. Desktop selection is a separate capability."""

__version__ = "3.0.0"

__all__ = ["MapGISClient", "Workspace", "MapPlan", "MapGISConfig", "__version__"]


def __getattr__(name):
    if name in __all__ and name != "__version__":
        from . import sdk

        return getattr(sdk, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _mcp_main(*, default_root=None):
    """Protect the protocol descriptor before loading the server and native libraries."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    options, _ = parser.parse_known_args()
    if options.transport == "streamable-http" or any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        from .server import main

        main(default_root=default_root)
        return

    original_stdout = sys.stdout
    original_stdout.flush()
    with os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n") as protocol_output:
        os.dup2(sys.stderr.fileno(), 1)
        sys.stdout = sys.stderr
        try:
            from .server import main

            main(default_root=default_root, protocol_output=protocol_output)
        finally:
            original_stdout.flush()
            os.dup2(protocol_output.fileno(), 1)
            sys.stdout = original_stdout

