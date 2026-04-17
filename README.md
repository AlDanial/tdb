# `textual-debugger` — A TUI Python Debugger Based on `textual` and `debugpy`

`textual-debugger` (the package) provides `tdb` (the command-line tool), a full-featured
terminal-based Python debugger with an intuitive interface and powerful features, all without leaving your terminal.

`tdb` is built with [textual](https://github.com/Textualize/textual)
and [debugpy](https://github.com/microsoft/debugpy) (the Debug Adapter Protocol engine behind
VS Code's Python debugger). It provides a rich interactive interface for stepping through code,
inspecting variables, managing breakpoints, and evaluating expressions for complex Python programs.
It can debug Python code that uses
- `asyncio` (with a built-in async task inspector)
- `threading` (with a thread inspector)
- `multiprocessing` / `concurrent.futures` (with automatic child process attachment and a process inspector)
and supports remote attachment to any debugpy-enabled Python program.

It also includes a JSON-RPC server mode for programmatic debug control, making it suitable for
automated debugging workflows and AI-assisted debugging.

## AI Assistance

The bulk of the code and documentation for `textual-debugger` was generated with Claude Code.
Thank you, Anthropic, for providing access to Claude Code through the
[Claude for Open Source](https://claude.com/contact-sales/claude-for-oss) program.

## Installation

```bash
pip install textual-debugger
```

## Quick Start

```bash
# Debug a script (stops at first line by default)
tdb my_script.py

# Debug with arguments
tdb my_script.py arg1 arg2

# Use a specific virtualenv
tdb --python /path/to/venv/bin/python my_script.py

# Don't stop on entry — run until first breakpoint or exit
tdb --no-stop-on-entry my_script.py
```

Or use the module entry point:

```bash
python -m tdb my_script.py
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
| `o` / `f` / `r` | Step out of current function (also aliased as "finish" and "return") |
| `c` | Continue execution |
| `p` | Pause a running program |
| `t` | Run to cursor position |
| `u` / `d` | Navigate stack up (caller) / down (callee) |
| `R` | Restart the debug session |
| `Ctrl+Q` | Quit |

> **Note:** `f` ("finish") and `r` ("return") are both aliases for step-out. DAP's only "exit-a-function" primitive is `stepOut`, which runs the rest of the current function normally and stops at the return point. A true gdb-style immediate-return (skipping remaining code in the function without executing side effects) is not supported by DAP/debugpy.

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
- **Right pane**: detail view with full stack trace and an expandable variable tree (same as the main Variables View) for the selected task
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

### Thread Inspector

The menu bar shows a **Threads (N)** label when the program has 2 or more threads. Click it to open a modal with:

- **Left pane**: list of threads with ID and name (current thread shown in bold)
- **Right pane**: full stack trace and expandable variable tree for the selected thread's top frame
- Navigate with arrow keys; press `r` to refresh, `Escape` to close

RPC equivalents:

```bash
# List all threads (* marks current)
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_threads","params":[]}'

# Inspect a specific thread by ID
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_thread","params":[1]}'
```

### Process Inspector

For programs using `multiprocessing`, the menu bar shows a **Processes (N)** label when there are 2 or more child processes. Click it to open a modal with:

- **Left pane**: list of child processes with PID, name, and status (alive/exited)
- **Right pane**: process details, full stack trace, and expandable variable tree for the selected process

tdb automatically attaches to child processes spawned via `multiprocessing.Process`, `multiprocessing.Pool`, or `concurrent.futures.ProcessPoolExecutor`. Breakpoints set in the parent are propagated to all child processes. When any process hits a breakpoint, all other processes are paused. Pressing `p` pauses all processes; `c` continues all.

**Stepping in multi-process programs:** step commands (`n`, `s`, `o`, `f`, `r`) apply only to the process whose stack is currently shown in the Code View (the one that hit the breakpoint). Other processes remain paused throughout the step. To step in a different process, open the Processes tab and select it first — the Code View switches focus to that process, and subsequent step commands operate on it.

RPC equivalents:

```bash
# List all child processes
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"list_processes","params":[]}'

# Inspect a specific process by name or PID
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"inspect_process","params":["ForkPoolWorker-1"]}'
```

### Remote Attach

Attach to a Python program that is already running with debugpy:

```python
# In the target program:
import debugpy
debugpy.listen(("0.0.0.0", 5678))
debugpy.wait_for_client()  # optional: pause until debugger connects
```

```bash
# Attach from tdb:
tdb -r 5678
tdb -r 192.168.1.10:5678

# With breakpoints:
tdb -r 5678 -k my_script.py:42
```

All debugging features (breakpoints, stepping, variable inspection, threads, processes, async tasks) work in remote attach mode. The Code View automatically navigates to the source file when the program stops.

### External Terminal Support

For debugging TUI programs (curses, textual, rich) that need direct terminal access:

```bash
tdb --terminal kitty my_tui_app.py
```

The debuggee runs in a separate window of the named terminal. Supported choices: `xterm`, `konsole`, `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`, `wezterm`, `terminator`. The selected terminal must be on `PATH`. Breakpoints, stepping, and variable inspection still work in tdb.

### Keybinding Schemes

```bash
tdb --keybindings vim my_script.py    # default
tdb --keybindings emacs my_script.py
tdb --keybindings default my_script.py
```

The keybinding choice is saved to `~/.config/tdb/config.json` and remembered for subsequent runs. View the full keybinding reference from the menu: **Configure > Keybindings**.

## JSON-RPC Server Mode

tdb includes a built-in debug server for programmatic control — useful for scripted debugging, CI pipelines, or AI-assisted debugging workflows.

### Headless Mode (no TUI)

```bash
python -m tdb --headless my_script.py &
```

The server listens on `http://127.0.0.1:8150/rpc` (change with `--server-port`).

### Dual Mode (TUI + server)

```bash
tdb --server my_script.py
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
| `list_threads` | `[]` | List all threads |
| `inspect_thread` | `[thread_id]` | Inspect a specific thread |
| `list_processes` | `[]` | List child processes (multiprocessing) |
| `inspect_process` | `["name_or_pid"]` | Inspect a specific child process |
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
usage: tdb [-h] [-r [HOST:]PORT] [-k FILE:LINE] [--cwd CWD]
           [--no-stop-on-entry] [--no-just-my-code] [--no-subprocess]
           [--python PYTHON] [--keybindings {default,vim,emacs}]
           [--terminal {xterm,konsole,gnome-terminal,ghostty,kitty,iterm2,warp,wezterm,terminator}]
           [--server] [--headless] [--server-port PORT]
           [program] [args ...]
```

| Flag | Description |
|------|-------------|
| `-r`, `--remote-attach HOST:PORT` | Attach to a remote debugpy server |
| `-k`, `--breakpoint FILE:LINE` | Set a breakpoint (may be repeated) |
| `--no-stop-on-entry` | Do not pause at the first line (default: stop on entry) |
| `--cwd DIR` | Working directory for the debuggee |
| `--python PATH` | Python interpreter for the debuggee |
| `--no-just-my-code` | Step into stdlib/site-packages code instead of skipping it (default: skipped). On uncaught exceptions, the crash modal always shows the full traceback including library frames, regardless of this flag. |
| `--no-subprocess` | Disable debugpy's subprocess tracking (use when debugging tdb itself) |
| `--terminal TERM` | Run debuggee in the named external terminal: `xterm`, `konsole`, `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`, `wezterm`, or `terminator` |
| `--keybindings SCHEME` | `default`, `vim`, or `emacs` (saved to config) |
| `--server` | Enable JSON-RPC server alongside TUI |
| `--headless` | JSON-RPC server only, no TUI |
| `--server-port PORT` | Server port (default: 8150) |

## Configuration

tdb stores configuration and state in `~/.config/tdb/`:

| File | Contents |
|------|----------|
| `config.json` | User preferences (keybinding scheme) |
| `last_run.json` | Breakpoints from previous sessions, keyed by project directory |

Breakpoints are automatically saved on exit and restored when debugging a program in the same directory. Each project's breakpoints are independent.

## Tech Stack

- [textual](https://github.com/Textualize/textual) — TUI framework
- [debugpy](https://github.com/microsoft/debugpy) — Debug Adapter Protocol implementation
- [pygments](https://pygments.org/) — Syntax highlighting
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) — JSON-RPC server

## License

MIT
