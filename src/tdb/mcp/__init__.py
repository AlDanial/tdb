"""Model Context Protocol (MCP) server for tdb.

Third in-process consumer of `RpcHandlers` — alongside the TUI and the
FastAPI/HTTP server. Speaks MCP over stdio so an MCP client (Claude
Desktop, IDE extensions, etc.) can drive a tdb debug session as a set
of tools.

Entry point: `tdb-mcp` (registered in pyproject.toml), `tdb --mcp`, or
`python -m tdb.mcp`.

The `mcp` package is an optional extra (it drags in `cryptography`, which
not every platform can install), so nothing here imports it at module
level: `main()` probes for it first and explains how to install it.
"""

from __future__ import annotations

import sys

INSTALL_HINT = 'pip install "textual-debugger[mcp]"'


def mcp_import_error() -> str | None:
    """None when the MCP SDK imports cleanly, else the ImportError text
    (covers both a missing `mcp` and a missing transitive dependency
    such as `cryptography`)."""
    try:
        import mcp.server.mcpserver  # noqa: F401
    except ImportError as exc:
        return str(exc)
    return None


def main() -> None:
    """`tdb-mcp` / `tdb --mcp` / `python -m tdb.mcp`: run the MCP server,
    or exit with an install hint when the optional extra is absent."""
    error = mcp_import_error()
    if error is not None:
        sys.exit(
            f"tdb: MCP support is not available ({error}).\n"
            f"Install it with:  {INSTALL_HINT}"
        )
    from .server import main as server_main

    server_main()
