# tdb — A TUI-Based Python Debugger

tdb is a terminal-based Python debugger built on [textual](https://github.com/Textualize/textual) and [debugpy](https://github.com/microsoft/debugpy) (the Debug Adapter Protocol engine behind VS Code's Python debugger). It provides a rich interactive interface for stepping through code, inspecting variables, managing breakpoints, and evaluating expressions — all from your terminal.

It also includes a JSON-RPC server mode for programmatic debug control, making it suitable for automated debugging workflows and AI-assisted debugging.

## Installation

```bash
cd work
uv pip install -e .
```

## Quick Start

```bash
# Debug a script (stops at first line by default)
./tdb.py my_script.py

# Debug with arguments
./tdb.py my_script.py arg1 arg2

# Use a specific virtualenv
./tdb.py --python /path/to/venv/bin/python my_script.py

# Don't stop on entry — run until first breakpoint or exit
./tdb.py --no-stop-on-entry my_script.py
```

Or use the installed entry point:

```bash
python -m tdb --stop-on-entry my_script.py
```

## Layout

```
┌─ Header ──────────────────────────────────────────────┐
├─ Menu Bar (File | Configure | Help) ──────────────────┤
│                           │                           │
│   Code View               │  Console View (stdout)    │
│   (source + breakpoints)  ├───────────────────────────┤
│                           │  Variable View (tree)     │
│                           ├───────────────────────────┤
│                           │  Stack View (call stack)  │
├─ Status Bar ──────────────────────────────────────────┤
│                           │                           │
│  Evaluate Console (REPL)  │  Breakpoint View (table)  │
│                           │                           │
├─ Footer (keybindings) ────────────────────────────────┘
```

## Features

### Source Code Navigation

The Code View shows syntax-highlighted Python source with line numbers. A cursor line (blue) tracks your position; the current execution line is highlighted in gold.

**Navigation (vim-style by default):**

| Key | Action |
|-----|--------|
| `j` / `k` | Move cursor down / up |
| `5j`, `10k` | Move N lines with count prefix |
| `g` | Go to line (with count: `42g` jumps to line 42) |
| `G` | Go to end of file |
| `[` / `]` | Jump to previous / next paragraph boundary |
| `/` | Search forward |
| `?` | Search backward |
| `n` / `N` | Next / previous search result |
| `PageUp` / `PageDown` | Scroll by page |

Switch between Navigation and Debug modes with `Escape`.

**View focus shortcuts (global):**

| Key | View |
|-----|------|
| `Ctrl+C` | Code View |
| `Ctrl+O` | Console View |
| `Ctrl+E` | Evaluate Console |
| `Ctrl+V` | Variable View |
| `Ctrl+S` | Stack View |
| `Ctrl+B` | Breakpoint View |

### Debugging Controls

In Debug mode, single keys control execution:

| Key | Action |
|-----|--------|
| `n` | Step over (next line) |
| `s` | Step into function call |
| `o` | Step out of current function |
| `c` | Continue execution |
| `p` | Pause a running program |
| `t` | Run to cursor position |
| `u` / `d` | Navigate stack up (caller) / down (callee) |
| `R` | Restart the debug session |
| `Ctrl+Q` | Quit |

### Breakpoints

Click the gutter in the Code View to toggle a breakpoint, or press `b` in Debug mode.

**Breakpoint indicators:**
- Red dot: active breakpoint
- Yellow dot: conditional breakpoint
- Blue dot: disabled breakpoint

**Conditional breakpoints:** Double-click a breakpoint to open the condition editor. Set a Python expression (e.g., `x > 10`) and/or a hit count (pause on the Nth hit).

**Breakpoint View actions:**
- `D` — Disable / enable all breakpoints
- `C` — Clear all breakpoints

Breakpoints persist across session restarts.

### Variable Inspection

The Variable View shows a tree of scopes (Locals, Globals) with all variables in the current frame. Expand nodes to drill into complex objects — children are loaded lazily on demand.

Format: `name (type) = value`

### Call Stack

The Stack View shows the full call stack. Click a frame to navigate to its source location and inspect its variables.

### Evaluate Console

A REPL at the bottom-left evaluates expressions in the current scope:

```
>>> len(items)
42
>>> sorted(data, key=lambda x: x.priority)[:3]
[Item(priority=1), Item(priority=2), Item(priority=3)]
```

- **Up/Down arrows** cycle through expression history
- **Tab** triggers DAP-based completion
- **Trailing `?`** shows help (signature + docstring):

```
>>> os.path.join?
(a, *p) — Join two or more pathname components...
```

### Console Output

The Console View captures stdout (normal text) and stderr (red text) from the debuggee in real time.

### Crash Detection

When the debuggee raises an unhandled exception, tdb:
1. Shows a modal with the full traceback
2. Navigates the Code View to the crash line
3. Populates the Stack View with the exception's call stack
4. Lets you press `R` to restart or `Escape` to dismiss

### Async Task Inspector

For programs using `asyncio`, the menu bar shows an **Async Tasks (N)** label with the count of active tasks (updated each time the program stops). Click it to open a full-screen modal:

- **Left pane**: list of all tasks with name, state (pending/done/cancelled), and coroutine
- **Right pane**: detail view with full stack trace for the selected task
- Navigate with arrow keys; press `r` to refresh, `Escape` to close

RPC equivalents:

```bash
# List all tasks
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_tasks","params":[]}'

# Inspect a specific task by name
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_task","params":["Task-1"]}'
```

### External Terminal Support

For debugging TUI programs (curses, textual, rich) that need direct terminal access:

```bash
./tdb.py --external-terminal my_tui_app.py
```

The debuggee runs in a separate terminal window (auto-detects xterm, gnome-terminal, konsole, kitty, alacritty, foot, xfce4-terminal). Breakpoints, stepping, and variable inspection still work in tdb.

### Keybinding Schemes

```bash
./tdb.py --keybindings vim my_script.py    # default
./tdb.py --keybindings emacs my_script.py
./tdb.py --keybindings default my_script.py
```

View the full keybinding reference from the menu: **Configure > Keybindings**.

## JSON-RPC Server Mode

tdb includes a built-in debug server for programmatic control — useful for scripted debugging, CI pipelines, or AI-assisted debugging workflows.

### Headless Mode (no TUI)

```bash
python -m tdb --headless --stop-on-entry my_script.py &
```

The server listens on `http://127.0.0.1:8150/rpc` (change with `--server-port`).

### Dual Mode (TUI + server)

```bash
./tdb.py --server my_script.py
```

Both the interactive TUI and the JSON-RPC server run simultaneously.

### RPC Protocol

Send POST requests with `{"action": "...", "params": [...]}`. Responses return `{"timestamp": "...", "success": true/false, "value": "..."}`.

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

### All RPC Actions

| Action | Params | Description |
|--------|--------|-------------|
| `help` | `[]` | List all actions |
| `status` | `[]` | Current state with location |
| `set_breakpoint` | `["file:line"]` or `["file:line", "condition", "hit_condition"]` | Set a breakpoint |
| `remove_breakpoint` | `["file:line"]` | Remove a breakpoint |
| `list_breakpoints` | `[]` | Show all breakpoints |
| `continue` | `[]` | Resume execution |
| `next` | `[]` | Step over |
| `step_in` | `[]` | Step into |
| `step_out` | `[]` | Step out |
| `pause` | `[]` | Pause execution |
| `inspect` | `["expr1", "expr2", ...]` | Evaluate multiple expressions |
| `evaluate` | `["expression"]` | Evaluate a single expression |
| `stack_up` | `[]` | Move up the call stack |
| `stack_down` | `[]` | Move down the call stack |
| `get_stack_trace` | `[]` | Full call stack |
| `get_output` | `[]` | Drain buffered stdout/stderr |
| `get_source` | `["file_path"]` | Read a source file |
| `list_tasks` | `[]` | List all asyncio tasks |
| `inspect_task` | `["task_name"]` | Inspect a specific asyncio task |
| `restart` | `[]` | Restart session (preserves breakpoints) |
| `quit` | `[]` | Shut down |

### SSE Event Stream

Subscribe to real-time debug events:

```bash
curl -N http://127.0.0.1:8150/events
```

Events: `initialized`, `stopped`, `continued`, `terminated`, `exited`, `output`. Each is JSON with `event`, `data`, and `timestamp` fields.

## CLI Reference

```
usage: tdb [-h] [--cwd CWD] [--stop-on-entry] [--no-just-my-code]
           [--python PYTHON] [--external-terminal] [--keybindings {default,vim,emacs}]
           [--server] [--headless] [--server-port PORT]
           program [args ...]
```

| Flag | Description |
|------|-------------|
| `--stop-on-entry` | Pause at the first line (default in `tdb.py` wrapper) |
| `--cwd DIR` | Working directory for the debuggee |
| `--python PATH` | Python interpreter for the debuggee |
| `--no-just-my-code` | Also step through library code |
| `--external-terminal` | Run debuggee in a separate terminal window |
| `--keybindings SCHEME` | `default`, `vim`, or `emacs` |
| `--server` | Enable JSON-RPC server alongside TUI |
| `--headless` | JSON-RPC server only, no TUI |
| `--server-port PORT` | Server port (default: 8150) |

## Tech Stack

- [textual](https://github.com/Textualize/textual) — TUI framework
- [debugpy](https://github.com/microsoft/debugpy) — Debug Adapter Protocol implementation
- [pygments](https://pygments.org/) — Syntax highlighting
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) — JSON-RPC server

## License

MIT
