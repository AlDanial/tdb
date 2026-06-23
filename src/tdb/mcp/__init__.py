"""Model Context Protocol (MCP) server for tdb.

Third in-process consumer of `RpcHandlers` — alongside the TUI and the
FastAPI/HTTP server. Speaks MCP over stdio so an MCP client (Claude
Desktop, IDE extensions, etc.) can drive a tdb debug session as a set
of tools.

Entry point: `tdb-mcp` (registered in pyproject.toml), `tdb --mcp`, or
`python -m tdb.mcp`.
"""
