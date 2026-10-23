# MCP v2 Integration

tdb ships a Model Context Protocol (MCP), version 2,
server (`tdb-mcp`) that exposes
the debugger as a curated set of tools an AI agent can call. The MCP
server is a third in-process consumer of the same dispatch handlers the
TUI and the HTTP server use so an agent gets the same lock semantics,
including the pause-during-continue bypass, and the same DAP-backed
inspection surface.

For a worked end-to-end example (prompting an agent to find two
runtime bugs in a sample program) see
[docs/tutorial-mcp-debugging.md](../tutorial-mcp-debugging.md) and
its companion `examples/sales_report_buggy.py`.

## Running the MCP server

The MCP server needs the optional `mcp` extra:

```bash
pip install "textual-debugger[mcp]"
```

Without it, `tdb-mcp` and `tdb --mcp` exit with that install hint, and
`tdb --info` reports `MCP server : not installed`. Everything else in
tdb works without the extra.

Configure your MCP client (Claude Desktop, an IDE extension, etc.) to
launch `tdb-mcp` over stdio. Example `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tdb": {
      "command": "tdb-mcp"
    }
  }
}
```

Three equivalent invocation forms: `tdb-mcp` (the dedicated entry
point), `tdb --mcp` (the main CLI with the `--mcp` switch), and
`python -m tdb.mcp` (module form). Pick whichever matches how your MCP
client expects to launch servers.

## Tool surface (17 tools, curated)

| Cluster | Tools |
|---------|-------|
| Lifecycle | `debug_launch`, `debug_attach`, `quit` |
| Control | `control(action, timeout_s=30)` where `action ∈ {continue, next, step_in, step_out, pause, wait_for_stop}` |
| Inspection | `inspect(expressions)`, `read_source(file_path)`, `stack_trace()`, `status()`, `get_output()` |
| Breakpoints | `set_breakpoint(spec, condition?, hit_condition?)`, `remove_breakpoint(spec)`, `list_breakpoints()` |
| Differentiators | `threads(thread_id?)`, `tasks(task_name?)`, `processes(name_or_pid?)`, `wait_graph()`, `rust_concurrency()` |

`control` is intentionally one tool that takes an action enum. The six
underlying RPC actions share a return shape, and agents perform
measurably better with a small surface than with one tool per action.
`threads` / `tasks` / `processes` overload list-vs-inspect via a
single optional argument for the same reason.

`debug_launch` accepts optional `lang` and `adapter` parameters mirroring the
CLI's `--lang`/`--adapter`; when omitted, the language is auto-detected from
`program`, so an agent can hand it a compiled binary directly. The
exception is Rust: compiled native binaries require explicit `lang="rust"`.
The `tasks`/`processes`/`wait_graph` tools stay registered for every language but
return a structured "not supported when debugging C/C++"-style error for
non-Python debuggees; `rust_concurrency` does the same for non-Rust debuggees.

## Agent flow for a long-running step

```
agent → control(action="continue", timeout_s=30)
mcp   → "still running, call pause or wait again"
agent → control(action="pause")        # OR: control(action="wait_for_stop", timeout_s=30)
mcp   → "<file>:<line>"
agent → inspect(["x", "len(buf)"])
mcp   → "x = 7\nlen(buf) = 1024"
```

`pause` bypasses the dispatch lock so it can interrupt a `continue`
that's still mid-flight (HTTP and MCP share the same `NO_LOCK_ACTIONS`
policy; see `tdb/server/app.py`).

## Security caveat

`inspect` calls debugpy's `evaluate`, which is **arbitrary Python
execution in the debuggee process**. This is inherent to a debugger and
not a tdb-specific concern, but MCP clients (and the humans running
them) should apply appropriate permission models: don't auto-approve
`inspect` against untrusted expressions, and don't expose `tdb-mcp` on
a network (stdio transport only by design).

## Deferred / out of scope (v1)

- SSE-style event push: `control` and `wait_for_stop` make polling
  efficient enough; events would also need uneven MCP-client support.
- HTTP / streamable-HTTP transports: would require auth (which the
  HTTP RPC server also currently lacks); stdio inherits the trust of
  the process that spawned it.
- Multi-session: one debug session per MCP process.
