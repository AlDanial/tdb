# JSON-RPC Server Mode

`tdb` includes a built-in debug server for programmatic control which is useful for scripted
debugging, CI pipelines, or AI-assisted debugging workflows.

## Headless Mode (no TUI)

```bash
python -m tdb --headless my_program.py &
```

The server listens on `http://127.0.0.1:8150/rpc` (change with `--server-port`).

## Dual Mode (TUI + server)

```bash
tdb --server my_program.py
```

Both the interactive TUI and the JSON-RPC server run simultaneously.

## RPC Protocol

Send POST requests with `{"action": "...", "params": [...]}`. Responses return
`{"timestamp": "...", "success": true/false, "value": "..."}`. Actions with a
structured result (today only `rust_concurrency`) additionally carry a `data`
object holding the full JSON payload; `value` then holds a one-line summary.

```bash
# Check status
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"status","params":[]}'

# Set a breakpoint
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"set_breakpoint","params":["/abs/path/to/file.py:42"]}'

# Continue execution
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"continue","params":[]}'

# Inspect variables
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect","params":["x", "len(items)", "type(result)"]}'

# Shut down
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"quit","params":[]}'
```

## All RPC Actions

| Action | Params | Description |
|--------|--------|-------------|
| `help` | `[]` | List all actions |
| `status` | `[]` | Current state with location |
| `set_breakpoint` | `["file:line"]` or `["file:line", "condition", "hit_condition"]` | Set a breakpoint |
| `remove_breakpoint` | `["file:line"]` | Remove a breakpoint |
| `list_breakpoints` | `[]` | Show all breakpoints |
| `continue` | `[]` or `[timeout_s]` | Resume execution; on timeout returns `"still running--call pause or wait again"` (success) |
| `next` | `[]` or `[timeout_s]` | Step over |
| `step_in` | `[]` or `[timeout_s]` | Step into |
| `step_out` | `[]` or `[timeout_s]` | Step out |
| `pause` | `[]` | Pause execution; bypasses the dispatch lock so it can interrupt an in-flight blocking action |
| `wait_for_stop` | `[]` or `[timeout_s]` | Wait for the next stop without issuing a step (use after `continue` returns `"still running"` to keep waiting) |
| `inspect` | `["expr1", "expr2", ...]` | Evaluate multiple expressions |
| `evaluate` | `["expression"]` | Evaluate a single expression |
| `stack_up` | `[]` | Move up the call stack |
| `stack_down` | `[]` | Move down the call stack |
| `get_stack_trace` | `[]` | Full call stack |
| `get_output` | `[]` | Drain buffered stdout/stderr |
| `stdin` | `["line"]` | Send a line (newline appended) to the program's stdin; errors when the session has no stdin (compiled languages, `--terminal`, remote attach) |
| `stdin_eof` | `[]` | Close the program's stdin (Ctrl+D) |
| `get_source` | `["file_path"]` | Read a source file |
| `list_threads` | `[]` | List all threads |
| `inspect_thread` | `[thread_id]` | Inspect a specific thread |
| `list_processes` | `[]` | List child processes (multiprocessing) |
| `inspect_process` | `["name_or_pid"]` | Inspect a specific child process |
| `list_tasks` | `[]` | List all asyncio tasks |
| `inspect_task` | `["task_name"]` | Inspect a specific asyncio task |
| `wait_graph` | `[]` | Show wait graph + any deadlock cycles |
| `rust_concurrency` | `[]` | Structured Rust thread/wait/deadlock snapshot in `data` (Rust sessions, stopped) |
| `restart` | `[]` | Restart session (preserves breakpoints) |
| `quit` | `[]` | Shut down |

## SSE Event Stream

Subscribe to real-time debug events:

```bash
curl -N http://127.0.0.1:8150/events
```

Events: `initialized`, `stopped`, `continued`, `terminated`, `exited`, `output`.
Each is JSON with `event`, `data`, and `timestamp` fields.
