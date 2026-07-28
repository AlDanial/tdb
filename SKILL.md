# Skill: Interactive Debugging with tdb (Python, C/C++, Perl)

Use this skill when you need to understand runtime behavior of code -- variable values, control flow, why a condition is or isn't met, what a function actually returns, or why an exception occurs. This is faster and more reliable than inserting print/logging statements.

`tdb` debugs Python (via debugpy, full feature set), C/C++ or any native
binary built with `-g` (via `gdb -i dap`, or `lldb-dap` with
`adapter="lldb-dap"`), and Perl (via a bundled adapter driving `perl5db`,
perl ≥ 5.18). The language is auto-detected from the target: `.py` →
Python, ELF/Mach-O/PE executable → C/C++, `.pl`/`.pm`/`.t` → Perl.

## When to use this

- A bug depends on runtime state you can't deduce from reading the code
- You need to inspect variables at a specific point in execution
- You want to trace control flow through conditional branches or loops
- An exception traceback doesn't give enough context
- You want to test what an expression evaluates to in a live scope

## Two ways to drive tdb

1. **MCP tools (preferred when available).** If the `tdb` MCP server is
   registered in this session, use its tools directly (`debug_launch`,
   `control`, `inspect`, ...) — no server process or curl needed. See
   "MCP mode" below.
2. **Headless HTTP JSON-RPC.** Start `tdb --headless` yourself and POST
   to `/rpc` with curl. Works everywhere, no MCP registration required.
   See "Quick start" below.

## MCP mode

Register the server once (any of the three invocations work):

```bash
claude mcp add tdb -- tdb-mcp          # or: tdb --mcp, or: python -m tdb.mcp
```

The server owns the debug session — do not also start `tdb --headless`.
16 tools:

| Cluster | Tools |
|---------|-------|
| Lifecycle | `debug_launch(program, args?, cwd?, stop_on_entry?, just_my_code?, python?, breakpoints?, lang?, adapter?)`, `debug_attach(host, port, breakpoints?, path_mappings?)`, `quit()` |
| Control | `control(action, timeout_s=30)` — `action ∈ {continue, next, step_in, step_out, pause, wait_for_stop}` |
| Inspection | `inspect(expressions)`, `read_source(file_path)`, `stack_trace()`, `status()`, `get_output()` |
| Breakpoints | `set_breakpoint(spec, condition?, hit_condition?)`, `remove_breakpoint(spec)`, `list_breakpoints()` |
| Concurrency | `threads(thread_id?)`, `tasks(task_name?)`, `processes(name_or_pid?)`, `wait_graph()` |

Multi-language notes:
- `debug_launch` auto-detects the language from `program` — pass a compiled
  binary directly (`debug_launch(program="/abs/path/prog")` debugs it via
  GDB's DAP mode); `.pl`/`.pm`/`.t` auto-detects Perl. `lang="cpp"` /
  `lang="perl"` forces it; `adapter="lldb-dap"` selects lldb-dap instead of
  GDB. The `python` param is only valid for Python debuggees (errors
  otherwise).
- `debug_attach` works for Perl debuggees too, not just Python — the Perl
  program must be prepared with `Devel::TdbRemote` (`use Devel::TdbRemote;`
  first line, `listen()` + `wait_for_client()`) in place of
  `debugpy.listen()`/`wait_for_client()`. C/C++ has no attach mode.
- `tasks`, `processes`, and `wait_graph` remain Python-only; for other
  languages they return a structured "not supported" error. `threads`
  works everywhere.
- **GDB (the default C/C++ adapter):** `inspect`/`evaluate` expressions
  go through GDB's CLI — prefix with `print`
  (`inspect(expressions=["print x"])`); bare `x` collides with GDB's
  examine-memory command. lldb-dap evaluates bare expressions directly.
- If breakpoints in a C/C++ file never bind, the binary likely lacks debug
  info — rebuild with `-g -O0`.

Typical flow:

```
debug_launch(program="/abs/path/script.py", breakpoints=["/abs/path/script.py:42"])
control(action="continue", timeout_s=30)
inspect(expressions=["x", "len(items)", "type(data)"])
control(action="next")
quit()
```

Notes:
- `breakpoints` specs are `"file.py:42"` strings; paths must be absolute.
- If `control` returns `still running — call pause or wait again`, the
  program didn't stop within `timeout_s`. Call `control(action="pause")`
  to interrupt it, or `control(action="wait_for_stop")` to keep waiting.
  `pause` bypasses the session lock, so it works even while another
  `control` call is still blocked.
- `wait_graph()` is the fastest way to diagnose an asyncio hang: it shows
  blocked tasks, what each awaits, and any deadlock cycles.
- `inspect` executes arbitrary Python in the debuggee — same caveat as
  the `inspect`/`evaluate` RPC actions.

## Quick start (HTTP JSON-RPC)

### 1. Start the debug server

```bash
.venv/bin/python -m tdb --headless --stop-on-entry /path/to/script.py &
```

The server starts on `http://127.0.0.1:8150/rpc`.  Use `--server-port PORT` to change it.

If the script takes arguments:
```bash
.venv/bin/python -m tdb --headless --stop-on-entry /path/to/script.py arg1 arg2 &
```

If the script needs a specific virtualenv:
```bash
.venv/bin/python -m tdb --headless --stop-on-entry --python /path/to/venv/bin/python /path/to/script.py &
```

For a C/C++ binary (compiled with `-g`), the same headless mode works — the
language is auto-detected; add `--adapter lldb-dap` to use lldb-dap instead
of GDB:
```bash
.venv/bin/python -m tdb --headless /path/to/binary arg1 &
```

### 2. Send commands via JSON-RPC

Every command is a POST to `/rpc` with `{"action": "...", "params": [...]}`.
Responses are `{"timestamp": "...", "success": true/false, "value": "..."}`.

```bash
# Check status
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"status","params":[]}'

# Set a breakpoint
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"set_breakpoint","params":["/absolute/path/to/file.py:42"]}'

# Continue execution (runs until breakpoint or exit)
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"continue","params":[]}'

# Inspect variables
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect","params":["x", "y", "len(items)"]}'

# Step to next line
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"next","params":[]}'
```

### 3. Clean up

```bash
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"quit","params":[]}'
```

## All actions

| Action | Params | Description |
|---|---|---|
| `help` | `[]` | List all actions with expected params |
| `status` | `[]` | Current state: stopped/running/terminated with location |
| `set_breakpoint` | `["file:line"]` or `["file:line", "condition", "hit_condition"]` | Set a breakpoint, optionally conditional |
| `remove_breakpoint` | `["file:line"]` | Remove a breakpoint |
| `list_breakpoints` | `[]` | Show all breakpoints with conditions |
| `continue` | `[]` or `[timeout_s]` | Resume execution until next breakpoint or exit |
| `next` | `[]` or `[timeout_s]` | Step over (execute current line, stop at next) |
| `step_in` | `[]` or `[timeout_s]` | Step into function call |
| `step_out` | `[]` or `[timeout_s]` | Step out of current function |
| `pause` | `[]` | Pause a running program (works even while a step/continue is blocked) |
| `wait_for_stop` | `[]` or `[timeout_s]` | Wait for the next stop without issuing a step |
| `inspect` | `["expr1", "expr2", ...]` | Evaluate multiple expressions, return all results |
| `evaluate` | `["expression"]` | Evaluate a single expression in the current scope |
| `stack_up` | `[]` | Move up the call stack (toward caller) |
| `stack_down` | `[]` | Move down the call stack (toward callee) |
| `get_stack_trace` | `[]` | Show full call stack with current frame marked |
| `get_output` | `[]` | Drain buffered stdout/stderr from the program |
| `get_source` | `["file_path"]` | Read a source file's contents |
| `restart` | `[]` | Restart the debug session (preserves breakpoints) |
| `quit` | `[]` | Stop the debuggee and shut down the server |

## SSE event stream

For real-time event monitoring, connect to the SSE endpoint:

```bash
curl -N http://127.0.0.1:8150/events
```

Events: `initialized`, `stopped`, `continued`, `terminated`, `exited`, `output`.
Each event is JSON with `event`, `data`, and `timestamp` fields.

## Debugging strategies

### Strategy 1: Targeted breakpoint inspection

When you know roughly where the bug is:

```bash
# Start server
.venv/bin/python -m tdb --headless --stop-on-entry /path/to/script.py &
sleep 2

# Set breakpoint at the suspicious line
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"set_breakpoint","params":["/path/to/file.py:87"]}'

# Run to the breakpoint
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"continue","params":[]}'

# Inspect everything relevant
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"inspect","params":["request", "response.status_code", "len(results)", "type(data)"]}'

# Check the call stack for context
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"get_stack_trace","params":[]}'
```

### Strategy 2: Conditional breakpoints

When a bug only occurs for specific input:

```bash
# Break only when the problematic condition is true
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"set_breakpoint","params":["/path/to/file.py:42", "user_id == 12345"]}'

# Or break on the Nth iteration
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"set_breakpoint","params":["/path/to/file.py:42", null, "100"]}'
```

### Strategy 3: Step-through exploration

When you don't know where the bug is:

```bash
# Set breakpoint at function entry
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"set_breakpoint","params":["/path/to/file.py:20"]}'

curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"continue","params":[]}'

# Step line by line, inspecting as you go
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"next","params":[]}'

curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"inspect","params":["result"]}'

# Step into a function call to see what happens inside
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"step_in","params":[]}'
```

### Strategy 4: Evaluate to test fixes

Use `evaluate` to test expressions in the live scope before changing code:

```bash
# What would this expression return?
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","params":["sorted(items, key=lambda x: x.priority)"]}'

# Would this condition catch the edge case?
curl -s -X POST http://127.0.0.1:8150/rpc -H 'Content-Type: application/json' \
  -d '{"action":"evaluate","params":["x is not None and len(x) > 0"]}'
```

## Important notes

- **Breakpoint paths must be absolute.** Use the full path, not relative.
- **`next` vs `step_in`:** `next` stays in the current function; `step_in` enters called functions.
- **`inspect` vs `evaluate`:** `inspect` takes multiple expressions and labels each result; `evaluate` returns a single raw result.
- **Non-Python debuggees:** expressions are evaluated by the language's adapter, not Python — with GDB, prefix expressions with `print` (see MCP notes above).
- **Step/continue actions block** until the program stops (breakpoint, exception, or exit). Default timeout is 600 seconds; pass a shorter per-call timeout as the first param (e.g. `{"action":"continue","params":[30]}`). On timeout the response is a success with value `still running — call pause or wait again` — follow up with `pause` to interrupt or `wait_for_stop` to keep waiting.
- **`--stop-on-entry`** pauses at the first line. Without it, the program runs until a breakpoint or exit.
- **Output capture:** stdout/stderr from the debuggee is buffered. Use `get_output` to retrieve it, or it's included automatically in step/continue responses.
- **After termination:** stepping and evaluation are unavailable. Use `restart` to start over, or `quit` to shut down.
