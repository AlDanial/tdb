# Installation

```bash
pip install textual-debugger
```

or (better):

```bash
uv pip install textual-debugger
```

or run it without installing:

```
uvx --from textual-debugger tdb  my_program.py
```

The MCP server (`tdb-mcp`, `tdb --mcp`) is an optional extra because its
`mcp` dependency requires the `cryptography` package, which cannot be
installed on every platform. Add it with:

```bash
pip install "textual-debugger[mcp]"
```
