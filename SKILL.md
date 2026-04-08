# Skill: Interactive Python Debugging with tdb

Use this skill when you need to understand runtime behavior of Python code -- variable values, control flow, why a condition is or isn't met, what a function actually returns, or why an exception occurs. This is faster and more reliable than inserting print/logging statements.

## When to use this

- A bug depends on runtime state you can't deduce from reading the code
- You need to inspect variables at a specific point in execution
- You want to trace control flow through conditional branches or loops
- An exception traceback doesn't give enough context
- You want to test what an expression evaluates to in a live scope

## Quick start

### 1. Start the debug server

```bash
cd /home/al/projects/tdb/work
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
| `continue` | `[]` | Resume execution until next breakpoint or exit |
| `next` | `[]` | Step over (execute current line, stop at next) |
| `step_in` | `[]` | Step into function call |
| `step_out` | `[]` | Step out of current function |
| `pause` | `[]` | Pause a running program |
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
- **Step/continue actions block** until the program stops (breakpoint, exception, or exit). Timeout is 30 seconds.
- **`--stop-on-entry`** pauses at the first line. Without it, the program runs until a breakpoint or exit.
- **Output capture:** stdout/stderr from the debuggee is buffered. Use `get_output` to retrieve it, or it's included automatically in step/continue responses.
- **After termination:** stepping and evaluation are unavailable. Use `restart` to start over, or `quit` to shut down.
