# `textual-debugger`

`textual-debugger` (the package) provides `tdb` (the command-line tool and module),
a full-featured terminal-based debugger for Python and other languages
with a Debug Adapter Protocol (DAP) implementation.  In addition to Python,
`tdb` comes with built-in support for
- C and C++ (via `gdb` or `lldb-dap`)
- Perl (via `perl -d`)
- Bash (via bash's own `DEBUG` trap; bash ≥ 4.4)
- Tcsh (via source instrumentation of a stock `tcsh`)

`tdb` is built with [textual](https://github.com/Textualize/textual) and speaks
DAP to a pluggable debug adapter: [debugpy](https://github.com/microsoft/debugpy)
(the engine behind VS Code's Python debugger) for Python,
[GDB's DAP mode](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Debugger-Adapter-Protocol.html) or
[lldb-dap](https://lldb.llvm.org/resources/lldbdap.html)
for compiled code. It provides a rich interactive interface for stepping through
code, inspecting variables, managing breakpoints, and evaluating expressions in
complex programs.

- PyPI: https://pypi.org/project/textual-debugger/
- GitHub: https://github.com/AlDanial/tdb

MIT License.  Copyright 2026 by Al Danial.

## Feature Overview

`tdb`:

- debugs multiple languages through the Debug Adapter Protocol: Python (via `debugpy`,
the richest feature set), C/C++ (via `gdb -i dap` or `lldb-dap`), Perl (via `perl -d`),
Bash (via bash's own `DEBUG` trap), and Tcsh (via source instrumentation of a
stock `tcsh`), with the language
auto-detected from the target (ref. [Multi-Language Debugging](#multi-language-debugging)).

- supports debugging of synchronous, asynchronous, multi-threaded, and multi-process Python code.
It specifically supports modules
    - `asyncio` (with a built-in async task inspector and task wait graph)
    - `threading` (with a thread inspector)
    - `multiprocessing` / `concurrent.futures` (with automatic child process attachment and a process inspector)

- supports remote attachment to debugpy-enabled Python programs

- includes a JSON-RPC server mode, an MCP mode, and a `SKILL.md` file that enable
programmatic debug control, making it suitable for
automated, headless debugging workflows and AI-assisted debugging

- can spawn the debuggee in an external terminal to enable debugging TUI applications
built with `textual`, `prompt-toolkit`, `urwid`, `curses`, `rich`, and so on

- comes with a post-mortem exception hook that can be installed in Python programs
to have `tdb` pop open automatically at the first uncaught exception

- can be entirely keyboard-driven
making it suitable for operation in non-graphical environments (mouse support is
available in graphical environments)

## Acknowledgments

Thank you:

- Will McGugan for the amazing `textual` module.
`tdb` would be a pale shadow of itself had I used any other TUI framework.
Fantastic work, Will.

- Microsoft for the Debug Adapter Protocol (DAP) and releasing
its implementation in `debugpy` and the Python Debugger extension for Visual Studio Code
as open source.

- Anthropic, for providing access to Claude Code through the
[Claude for Open Source](https://claude.com/contact-sales/claude-for-oss) program.
`tdb` was made almost entirely with Claude Code.

- OpenAI, for providing access to Codex through the
[Codex for Open Source](https://developers.openai.com/community/codex-for-oss) program.

## Gallery
<p align="center">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/async_breakpoint.png" alt="at breakpoint" width="300">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/async_task_graph.png" alt="task graph" width="300">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/multiprocessing_process_3.png" alt="multiple processes" width="300">
  <img src="https://github.com/AlDanial/tdb/blob/main/gallery/threading_list.png" alt="thread list" width="300">
</p>

Videos:
- [tdb basics](https://youtu.be/2_qf2WZDHuA) views, keybindings, breakpoints, stepping, variable modification, call stack
- [asyncio tasks](https://youtu.be/vM4tODuqMGg) inspect asyncio tasks and their wait graph; code mod to allow pause
- [threads and processes](https://youtu.be/J8LOARLs2oQ) inspect variables and call stacks in multiple threads and processes
- [external terminal](https://youtu.be/121aihjAQ8g) run the debuggee in a separate terminal--ideal for debugging TUI applications

## Installation

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


## Quick Start

```bash
# show comprehensive documentation in a terminal-based Markdown viewer
tdb --doc

# debug a script (stops at first line by default)
tdb my_program.py

# debug with arguments
tdb my_program.py arg1 arg2

# debug a C/C++ (or other native) executable built with -g.  The ELF/Mach-O/PE
# binary is auto-detected and debugged through GDB's DAP mode (GDB >= 14)
tdb ./myprog arg1 arg2

# same, but using lldb-dap (LLVM >= 17) instead of gdb
tdb --adapter lldb-dap ./myprog

# force the language when auto-detection can't tell (e.g. an extensionless script)
tdb --lang python ./mytool

# add breakpoints at lines 20 and 35 of `my_program.py` and line 14
# of `module.py` (when -k is given, --no-stop-on-entry is set and the
# program runs to the first breakpoint)
tdb -k 20 -k 35 -k module.py:14 my_program.py arg1 arg2

# run straight to line 20 without saving the breakpoint for future
# sessions (-t is -k minus the persistence)
tdb -t 20 my_program.py

# use a specific virtualenv
tdb --python /path/to/venv/bin/python my_program.py

# step into, or stop at tracebacks in library code
tdb --no-just-my-code --python /path/to/venv/bin/python my_program.py

# run until first breakpoint or exit
tdb --no-stop-on-entry my_program.py

# run the debuggee in an external terminal
tdb --terminal xterm my_program.py

# attach to a remote Python program that has a debugpy server on port 5678
# (source code is automatically downloaded from the remote host)
tdb -r remotehost:5678

# attach to a remote Python program that has a debugpy server on port 5678
# and set a breakpoint where tdb and the remote program have the same
# source code layout
tdb -r remotehost:5678  -k my_program.py:42

# attach to a remote Python program that has a debugpy server on port 5678
# and set a breakpoint where code on the local host is at a different location
# than code on the remote host
tdb -r remotehost:5678 --local-root /my/code/dir --remote-root /app -k my_program.py:42

# separate tdb arguments from debuggee arguments with `--` 
tdb --python /path/to/venv/bin/python -- my_program.py -k 17 --max 23.3
```

Alternatively, use the module entry point:

```bash
python -m tdb my_program.py
```

## Multi-Language Debugging

`tdb` debugs any language that has a Debug Adapter Protocol backend. Five
languages are supported out of the box:

| Language | Adapter(s) | Dependencies           | Feature level |
|----------|------------|------------------------|---------------|
| Python | `debugpy` (default) | Python ≥ 3.11 | everything in this README |
| C / C++ (any native binary) | `gdb` (default), `lldb-dap` (alternate) | `gdb -i dap` requires GDB ≥ 14; `lldb-dap` ships with LLVM ≥ 17 (e.g. `apt install lldb`) | core debugging: breakpoints, stepping, stack, variables, evaluate console |
| Perl | perl-tdb (bundled) | perl ≥ 5.18 on PATH  | core debugging + remote attach |
| Bash | bash-tdb (bundled) | bash ≥ 4.4 on PATH  | core debugging (no remote attach) |
| Tcsh | tcsh-tdb (bundled) | tcsh on PATH | core debugging (no remote attach, no conditional breakpoints, no pause) |

### Language detection and selection

The language is auto-detected from the debug target:

1. File extension: `.py` → Python; `.pl` / `.pm` / `.t` → Perl; `.sh` / `.bash`
   → Bash; `.csh` / `.tcsh` → Tcsh.
2. Native executables (ELF, Mach-O, PE magic bytes) → C/C++.
3. A `#!...python`, `#!...perl`, `#!...bash`, or `#!...csh`/`#!...tcsh`
   shebang → Python / Perl / Bash / Tcsh respectively.
4. C/C++/Rust *source* files (`.c`, `.cpp`, `.rs`, …) produce an error with a
   hint: compile with debug info (`g++ -g -O0`) and debug the binary.
5. Anything else produces an error naming the `--lang` override.

`--lang` forces the language; `--adapter` picks a non-default adapter within
it (`tdb --lang cpp --adapter lldb-dap ./myprog`).

> **Migration note:** extensionless Python scripts without a `python` shebang
> were previously assumed to be Python; they now require `--lang python`.

### Adapters are found on `PATH`

`tdb` does not download or bundle adapters. If the adapter executable isn't
found, the error names the package to install. To use an adapter from a
non-standard location, or change a language's default adapter, add to
`config.json` (see [Configuration](#configuration)):

```json
{
  "adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"},
  "default_adapters": {"cpp": "lldb-dap"}
}
```

### What works for non-Python languages

Core debugging works identically for every language: breakpoints (incl.
conditions and persistence), stepping, continue/pause, run-to-cursor, stack
navigation, variable inspection, the evaluate console, syntax highlighting,
and the JSON-RPC / MCP programmatic modes.

Python-specific features are hidden or return "not supported for this
language" message when debugging other languages: statement-granularity
stepping (non-Python languages always step per line), the async task /
process inspectors and wait graph, the evaluate console's trailing-`?` help,
`--python`/`--pv`, `--no-subprocess`, automatic child-process attachment, and
the post-mortem / `tdb.breakpoint()` hooks (those hooks live inside Python
programs by nature). Remote attach (`-r`) also works for Perl (see
[Perl](#perl), `Devel::TdbRemote` in place of `debugpy.listen()`), but not
for C/C++, Bash, or Tcsh. `--terminal` is
currently ignored for non-Python targets.

**Bash limitations (v1):** the bash adapter uses bash's own `DEBUG` trap and
has a smaller feature envelope than Python/Perl:

- Debuggee code that installs its own `DEBUG` trap clobbers the harness;
  debugging silently degrades to free-running.
- No stopping inside subshells `(...)`, `$(...)`, or pipeline segments; they
  execute normally.
- Child bash processes run uninstrumented.
- Outer-frame locals not inspectable (innermost frame only).
- Pause is deferred while blocked in an external command.
- `.sh` files that aren't bash are only diagnosed at launch, by bash itself
  or the harness version check.
- The `DEBUG` trap never fires on a function-definition line, so a
  breakpoint on a `func() {` line never hits; entry and step-in stops land
  on the first executable line of the function body instead.
- The debug control channel occupies two inherited file descriptors
  (typically high-numbered). A script that execs redirections onto those
  exact fds (e.g. `exec 63>&-` or reusing them for its own I/O) will
  silently break debugging.

See [Bash](#bash) below for launch details.

**Tcsh limitations (v1):** tcsh has no debug hooks at all, so the tcsh
adapter debugs an instrumented temporary *copy* of the script (original
paths and line numbers are preserved in everything tdb displays):

- Conditional breakpoints are not supported; a condition set in the
  Breakpoint View is ignored for tcsh (the breakpoint always stops).
- No asynchronous pause: a free-running script stops only at the next
  breakpoint (or when it exits).
- A breakpoint binds to the nearest safe statement at or after the
  requested line; unplaceable breakpoints are reported unverified.
- Stack frames represent `source`d files, not native call frames, and only
  literal `source` targets resolvable at launch are instrumented (computed
  or `cd`-dependent sources run normally but are atomic to the debugger).
- All frames show the same live shell state (Shell Variables, Environment,
  Aliases, Arguments).  Stock tcsh keeps no per-frame history.
- Multiple commands on one physical line (`a ; b`), command substitutions,
  and external commands are atomic stepping units.
- `$0` inside dynamically generated or evaluated text can expose the
  generated copy's path (ordinary lexical `$0` is rewritten correctly).
- Requires Python ≥ 3.11.

See [Tcsh](#tcsh) below for launch details.

### C/C++ tips

- Compile with `-g` (ideally `-g -O0`). If no breakpoint in a file can be
  bound, `tdb` prints a console warning suggesting the program may lack
  debug info.
- Stack frames pointing into system libraries often have no source on disk;
  the Code View shows a `<Could not read …>` placeholder while the stack,
  variables, and evaluate console remain fully usable.
- GDB (the default adapter) has the most complete libstdc++
  pretty-printing. `lldb-dap` (via `--adapter lldb-dap`) also debugs
  GCC-built binaries fine.  DWARF is compiler-neutral.
- **GDB evaluate-console quirk:** GDB's DAP treats REPL input as CLI
  commands, so evaluate expressions with an explicit `print`, e.g.
  `print x` rather than bare `x` (bare `x` collides with GDB's
  examine-memory command). `lldb-dap` evaluates bare expressions directly.

### Perl

`tdb` bundles its own Perl adapter (`perl-tdb`) so only need
`perl` ≥ 5.18 on `PATH`. It drives stock `perl5db` under the
hood, so it works with any Perl already on the system.

**Launching a script:**

```bash
tdb script.pl
```

**Compile-time code (`BEGIN` blocks):** Perl runs `BEGIN` blocks and the
`use` statements that are themselves `BEGIN` blocks while it is still
*compiling* your program, before stock `perl5db` ever stops. `tdb` arms the
debugger ahead of compilation so that code is debuggable too, which means the
first stop is the first **compile-time** statement of your file (typically
`use strict;` near the top) rather than the first runtime statement. Step from
there and you land inside your `BEGIN` blocks, with the stack and evaluate
views working normally (local variable listing is limited at a compile-time
stop); the `Stack` view shows the frame as `main::BEGIN`. Stepping through a
`use` line takes a few steps (the pragma's
own compile-time work happens in between) but you are never dragged into
another module's internals.

Two consequences worth knowing:

- **Breakpoints are deferred while your program compiles.** During the compile
  phase Perl has only parsed part of your file, so its line table is
  incomplete and a breakpoint can't be verified yet. `tdb` holds such requests
  and, while it single-steps through the rest of compilation, checks each
  compile-time statement it lands on against them. A breakpoint placed
  *inside* a `BEGIN` block fires there directly, on the first run, without
  needing to be stepped into by hand. Conditional breakpoints work the same
  way at compile time; a condition that itself errors behaves exactly like a
  bad condition at runtime in that it does not fire. Two residual caveats: a
  breakpoint on a non-statement line (the `BEGIN {` line itself, or a blank
  line) never fires during the compile phase, since it's never actually
  trapped as a statement; and `hitCondition` (break on the Nth hit) isn't
  honored for a compile-time stop, only a plain `condition`.
- **Startup is slower for large dependency graphs**, because the debugger is
  active throughout compilation. A script with a big `use` tree takes
  noticeably longer to reach its first stop under `tdb`.

**`END` blocks** are entered with **step-in** (`s`). `next` and `continue` run
straight past them to program termination, which is standard `perl5db`
behavior, not a `tdb` limitation.

**When your program dies:** an uncaught Perl error (`die`, or a fatal runtime
error such as division by zero) opens the same error modal Python tracebacks
get: the message, the call stack parsed from Perl's `at FILE line N.` /
`... called at FILE line N` output, and Code View navigated to the failing
line. This works for compile-time failures too, including a `die` inside a
`BEGIN` block that aborts compilation. Press `e` in Code View to re-summon the
last error. `tdb` also reports the debuggee's real exit status rather than
assuming success.

**Remote attach:** useful when the Perl process is already running (a long-
lived service, a process started by something other than `tdb`) or lives on
another host/container. Add three lines to the target program, with the
`use` line first so the debugger is armed before any of your code compiles:

```perl
use Devel::TdbRemote;                 # first line of your program
...
Devel::TdbRemote::listen(5678);       # non-blocking
print "Waiting for tdb to attach on port 5678\n";
Devel::TdbRemote::wait_for_client();  # blocks until tdb connects
```

Then attach from `tdb`, forcing the language since there's no local `program`
argument for `tdb` to detect it from:

```bash
tdb --lang perl -r host:5678
```

**Arming caveat:** only code *compiled after* the debugger is armed can be
stepped into or breakpointed. That's why `use Devel::TdbRemote;` must be the
first line of the program. If you can't edit the first line (e.g. a wrapper
script controls startup), arm it before Perl even parses your file instead:
`perl -d:TdbRemote prog.pl`, or set `PERL5OPT=-d:TdbRemote` in the
environment that launches the debuggee.

**Copying the adapter to a remote host:** `Devel::TdbRemote` and its helper
script are plain files, not a CPAN install.  Copy both onto the remote
machine and point `PERL5LIB` at the directory that contains them, for example:

```bash
# From a checkout or an installed wheel's site-packages/tdb/adapters/perl:
scp -r Devel/TdbRemote.pm helpers.pl remote-host:/opt/tdb-perl/
# On the remote host:
export PERL5LIB=/opt/tdb-perl:$PERL5LIB
```

(`Devel/TdbRemote.pm` locates `helpers.pl` next to itself at runtime, so keep
the two files in the same relative layout shown above; `helpers.pl` is a
sibling of the `Devel/` directory, not inside it.)

**PadWalker (optional but recommended):** inspecting lexical (`my`)
variables in the *current* frame always works. Lexicals in outer/caller
frames need the `PadWalker` module installed on the debuggee's Perl; without
it, tdb falls back to a read-only pad walk that can't reach fully into
enclosing scopes, and outer-frame variable listings degrade accordingly.
Install with `cpanm PadWalker` (or your distro's package) for full fidelity.

**Pause is unavailable in attach mode.** Launch-mode sessions (`tdb
script.pl`) support pausing a running program at any time. Remote-attach
sessions don't. Asynchronous pause (as Python gets via `debugpy`) needs a control channel
`Devel::TdbRemote` doesn't implement yet; `pause` in attach mode returns a
"not available" error instead of hanging.

### Bash

`tdb` bundles its own bash adapter (`bash-tdb`) so no separate adapter install
needed, just a `bash` ≥ 4.4 on `PATH`. It drives stock bash's own `DEBUG`
trap (with `extdebug` for return-value control) under the hood via a small
harness script sourced through `BASH_ENV`, so it works with any bash already
on the system.

**Launching a script:**

```bash
tdb script.sh
```

Core debugging works as described above (breakpoints, stepping,
continue/pause, stack, variables, evaluate console), with the v1 caveats
listed in [What works for non-Python languages](#what-works-for-non-python-languages).
Most notably, a breakpoint can't be set on a `func() {` line itself (the
`DEBUG` trap never fires there), stops never land inside subshells or
pipeline segments, child bash processes run uninstrumented, and only the
innermost frame's locals are inspectable. There is no remote-attach mode for
Bash.

The Variables view shows three scopes for bash: Locals (innermost frame
only), Globals (unexported shell variables), and Environment (exported
variables, both inherited and script-`export`ed).

### Tcsh

`tdb` bundles its own tcsh adapter (`tcsh-tdb`) so no separate adapter
is needed, just a stock `tcsh` on `PATH` (or
`{"adapters": {"tcsh": "/path/to/tcsh"}}` in `config.json`). Stock tcsh has
no debugger hooks, so the adapter instruments a temporary copy of the script
(and of any literal `source`d files), runs it with `tcsh -f`, and
coordinates stops through private FIFOs. tdb always shows the original
source paths and line numbers; the generated copies are private adapter
details and are cleaned up when the session ends.

**Launching a script:**

```bash
tdb script.csh
```

Core debugging works as described above: line breakpoints, stepping
(`next` steps over `source`d files, `stepIn` steps into instrumented ones),
stack navigation across `source` nesting, variable inspection, and the
evaluate console with the v1 caveats listed in
[What works for non-Python languages](#what-works-for-non-python-languages),
most notably: no conditional breakpoints, no pause of a free-running
script, and stepping is per logical source line.

The Variables view shows four scopes for tcsh: Shell Variables (`set`),
Environment (`setenv`), Aliases, and Arguments (`argv`). All of them show
the live state of the single tcsh process. The evaluate console executes
text directly in the paused shell. It can inspect *and mutate* state
(`set name = value` takes effect immediately), and a syntax error in
evaluated text can terminate the debuggee; there is no isolation.

## Layout

```
┌─ Header ──────────────────────────────────────────────┐
├─ Menu Bar (File / Configure / Help)───────────────────┤
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
├─ Footer (keybindings) ────────────────────────────────┤
└───────────────────────────────────────────────────────┘
```

The status bar shows the current execution state (running, paused,
breakpoint hit) and location.
The footer shows the most relevant keybindings for the current mode.

## Features

### Navigation and Keybindings


The Code View shows syntax-highlighted source (lexer chosen per language) with line numbers.
A cursor line (blue) tracks your position; the current execution line is highlighted in gold.

**View focus shortcuts (global):**

| Key | View |
|-----|------|
| `Ctrl+C` | Code View |
| `Ctrl+O` | Console View |
| `Ctrl+E` | Evaluate Console |
| `Ctrl+V` | Variable View |
| `Ctrl+S` | Stack View |
| `Ctrl+B` | Breakpoint View |

**Menu-bar shortcuts (global):**

`Alt+<first-letter>` opens the corresponding tab in the menu bar.

| Key | Menu |
|-----|------|
| `Alt+F` | File (open a different script to debug) |
| `Alt+C` | Configure (Color Theme, Keybindings, Step Mode) |
| `Alt+T` | Threads |
| `Alt+P` | Processes |
| `Alt+A` | Async Tasks |
| `Alt+H` | Help (Documentation, About) |

**Navigation (vim-style by default):**

By default the Code View is in Debug mode.  Hit `Escape` to switch to Navigate mode
In Navigate mode, you can move around the file with the following keys:

| Key | Action |
|-----|--------|
| `j` / `k` | Move cursor down / up |
| `5j`, `10k` | Move N lines down / up with count prefix |
| `G` | Go to end of file (with count: `42G` jumps to line 42)|
| `[` / `]` | Jump to previous / next paragraph boundary |
| `/` | Search forward |
| `?` | Search backward |
| `n` / `N` | Next / previous search result |
| `PageUp` / `PageDown` | Scroll by page |

Switch from Navigate back to Debug mode with `Escape`.

> **Note:** Many terminals send the byte sequence `ESC+f` for `Alt+F`, which Textual's
ANSI parser rewrites to `Ctrl+Right` (the readline "forward-word" convention).
`tdb` binds both so `Alt+F` works as expected.

### Debugging Controls

Keybindings for stepping, continuing, pausing, and stack navigation match
those for gdb/pdb, with some aliases and extras thrown in for convenience.

| Key | Action |
|-----|--------|
| `n` | Step over (next statement) |
| `s` | Step into function call |
| `o` / `f` / `r` | Step out of current function (also aliased as "finish" and "return") |
| `c` | Continue execution |
| `p` | Pause a running program |
| `t` | Run to cursor position |
| `u` / `d` | Navigate stack up (caller) / down (callee) |
| `j` / `k` | Move cursor down / up (with count: `5j`, `10k`) |
| `G` | Go to last line (with count: `42G` jumps to line 42) |
| `e` | Re-display the last error (traceback) |
| `R` | Restart the debug session |
| `q q` | Quit |
| `Ctrl+q` | Quit |

> **Note:** `f` ("finish") and `r` ("return") are both aliases for step-out. DAP's only
"exit-a-function" primitive is `stepOut`, which runs the rest of the current function
normally and stops at the return point. A true gdb-style immediate-return (skipping
remaining code in the function without executing side effects) is not supported by DAP/debugpy.

**Step granularity (statement vs. line):** by default, `n` (step over) and `s` (step into)
treat a multi-line source statement as a single step. For example, stepping over

```python
results = await asyncio.gather(
    fetch(1, 2),
    fetch(2, 1),
    fetch(3, 3),
)
print(results)   # next stop lands here, not on each sub-line above
```

lands on `print(results)`, not on each interior sub-line of the `gather` call. Switch to
**Line** mode (Configure > Step Mode) to get debugpy's native per-line behavior, which
stops on each physical line--useful for inspecting how a complex expression is built up.
The choice is saved to `~/.config/tdb/config.json`.

Statement mode requires a source-language model and is currently Python-only;
other languages always step per line (the Step Mode menu says so if you try).

### Breakpoints

Click the gutter in the Code View to toggle a breakpoint, or press `b` in Debug mode.

**Breakpoint indicators:**
- Red dot: active breakpoint
- Yellow dot: conditional breakpoint
- Blue dot: disabled breakpoint

**Conditional breakpoints:** Double-click a breakpoint to open the condition editor.
Set a Python expression (e.g., `x > 10`) and/or a hit count (pause on the Nth hit).

**Breakpoint View actions:**
- `D` : Disable / enable all breakpoints
- `C` : Clear all breakpoints

Breakpoints persist across session restarts.

### Variable Inspection

The Variable View shows a tree of scopes with all variables in the current frame. The scopes
themselves are language-dependent: Locals, Globals, plus Environment for bash; Lexicals,
Globals, Specials for Perl (see the [Bash](#bash) and [Perl](#perl) sections above for
details). Expand nodes to drill into complex objects.  Children are loaded lazily on demand.
Variable values can be changed in the Evaluate Console.

Double-click a variable, or highlight the variable with the text cursor in
the Variables View and press `Enter`
to display that variable in a modal.  This simplifies inspection of
large or deeply nested data structures.

### Call Stack

The Stack View shows the full call stack. Click a frame to navigate to its source
location and inspect its variables.

### Evaluate Console

A read-evaluate-print loop (REPL) at the bottom left permits
interactive evaluation of expressions in the current scope:

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
(a, *p) : Join two or more pathname components...
```

Variable values set here are reflected in the running code.

### Cut / Paste

Expression for the Evaluate Console are often copied from the Code View.
Doing this in `tdb` differs from traditional terminal behavior, because `textual` applications
capture mouse events for their own use.

Instead, hold the `Shift` key while performing your conventional cut/paste keystrokes or mouse
operation to get the expected behavior.

### Console Output

The Console View captures stdout (normal text) and stderr (red text) from the debuggee
in real time.

If your program prints a lot, or prompts for input, or uses colors or
terminal control codes, run the program in an external terminal
with `--terminal` for a better experience.
The `--terminal` switch requires a graphical environment and a compatible
terminal emulator.


### Crash Detection

When the debuggee raises an unhandled exception, `tdb`:
1. Shows a modal with the full traceback
2. Navigates the Code View to the crash line
3. Populates the Stack View with the exception's call stack
4. Lets you press `R` to restart or `Escape` to dismiss

> Note:  after dismissing the traceback modal, you
> can display it again by hitting `e` when focus is in the Code View.

### Post-Mortem Exception Hook

You can have `tdb` pop open automatically when *any* Python program crashes without the
need to launch through `tdb` up front. Install the hook once at the top of your program:

```python
import sys
import tdb
sys.excepthook = tdb.exception_hook
```

When an uncaught exception reaches the hook, `tdb`:

1. Prints the standard Python traceback to stderr (so your scrollback still has a record)
2. Snapshots every frame in the traceback. This includes locals, plus one level of
recursion into containers (`dict`, `list`, `tuple`, `set`) and objects with `__dict__`
3. Launches the TUI in **post-mortem mode**, inheriting the current terminal

In post-mortem mode you can:

- Navigate the call stack (`u` / `d` or the Stack View) and see each frame's locals
- Expand nested containers and object attributes in the Variables View
- Read the full traceback (including chained `cause`/`context` exceptions) in the Console View
- Jump around the source with the full Code View (syntax highlighting, goto-line, etc.)

Stepping, continue, breakpoints, restart, and the Evaluate View are disabled. The original
interpreter is gone since the view is a frozen snapshot. Press `q` to exit.

The hook is a no-op when stdin/stdout aren't a tty (e.g. when your program is piped or
run from cron), so it is safe to leave installed in production code. Snapshots are
written to a temp file that is deleted as soon as `tdb` exits.

Snapshot depth / breadth is capped (5 levels, 50 children per container) to keep the
capture cheap even for pathological object graphs; cycles are handled via identity
memoization.

### Post-Mortem within a Docker Container

The `textual-debugger` GitHub repository's `examples/` directory has three files
that show how to run a `tdb`-enabled Python program under `tmux` in a Docker
container so that you can attach to the container and inspect the program
in `tdb` post-mortem analysis mode if the program hits an unhandled exception:

- [post_mortem_example.py](https://github.com/AlDanial/tdb/blob/main/examples/post_mortem_example.py)
- [post_mortem_entrypoint.sh](https://github.com/AlDanial/tdb/blob/main/examples/post_mortem_entrypoint.sh)
- [Dockerfile.post_mortem](https://github.com/AlDanial/tdb/blob/main/examples/Dockerfile.post_mortem)

### Live Breakpoint Hook

`tdb` has an improved implemenation of the standard `breakpoint()` function (or equivalently,
`pdb.set_trace()`) used to pause at a specific line to inspect, then
continuing--use `tdb.breakpoint()`:

```python
import tdb

def compute(n):
    total = sum(range(n))
    tdb.breakpoint()  # pause here and drop into tdb
    return total
```

Or hook it into the builtin `breakpoint()` function for the whole program:

```bash
PYTHONBREAKPOINT=tdb.breakpoint python myscript.py
```

When the call is reached, `tdb` starts an in-process `debugpy` server on a loopback port,
spawns `python -m tdb -r <port>` as a subprocess so the TUI takes over the terminal,
and pauses the calling thread at the line that called `tdb.breakpoint()` (the hook
auto-steps out of its own helper so you land in your own frame, not inside
`breakpoint_hook.py`). Stepping (`n`/`s`/`o`), `continue`, and setting/removing breakpoints
all work normally; quitting `tdb` (`Ctrl+q`) detaches without killing the program, and
debugpy auto-resumes any threads still paused.

This differs from `tdb.exception_hook` in one way:
- **Requires `debugpy`** as a runtime dependency for the debuggee (only imported when the hook actually fires).

Unlike the exception hook (which works on a frozen snapshot), the breakpoint hook leaves
the interpreter live: variable inspection reads real objects, and stepping/`continue`
drive the user's program forward.

As with `exception_hook`, the call is a no-op when stdin/stdout aren't a tty, so it's
safe to leave in code paths that sometimes run headless.

Quitting `tdb` while paused in a `tdb.breakpoint()` session detaches the debugger and
lets the program continue running normally.
This behavior matches hitting `c` while in a conventional (that is, the Python
standard library's) `breakpoint()` session.
If you want to kill the program instead, use `Ctrl+c` in the terminal running the debuggee.

### Async Task Inspector

For programs using `asyncio`, the menu bar shows an **Async Tasks (N)** label with the count of
active tasks (updated each time the program stops). Click it to open a full-screen modal:

- **Left pane**: list of all tasks with name, state (pending/done/cancelled), awaiting
  primitive (`Lock.acquire`, `Queue.get`, `asyncio.sleep`, …), and coroutine
- **Right pane**: detail view with full stack trace and an expandable variable tree (same
as the main Variables View) for the selected task
- Press `g` to switch the right pane to the **wait graph** which is a tree showing
  each blocked
  task, the asyncio primitive it's parked on, and the task(s) holding that primitive.
  Cycles (deadlocks) are highlighted in red both in the task table and as a "Deadlock
  cycles" section at the top of the graph. Selecting a node in the tree highlights the
  corresponding task in the table.
- Navigate with arrow keys; press `r` to refresh, `Escape` to close

> Note:  Async task relationships may be directed acyclic graphs (DAGs) rather than trees
> but I don't know of a way to visualize DAGs in textual.

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

# Show wait graph and any deadlock cycles
curl -s -X POST http://127.0.0.1:8150/rpc \
  -H 'Content-Type: application/json' \
  -d '{"action":"wait_graph","params":[]}'
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

For programs using `multiprocessing`, the menu bar shows a **Processes (N)** label when there
are 2 or more child processes. Click it to open a modal with:

- **Left pane**: list of child processes with PID, name, and status (alive/exited)
- **Right pane**: process details, full stack trace, and expandable variable tree for the selected process

`tdb` automatically attaches to child processes spawned via `multiprocessing.Process`, `multiprocessing.Pool`,
or `concurrent.futures.ProcessPoolExecutor`. Breakpoints set in the parent are propagated to all
child processes. When any process hits a breakpoint, all other processes are paused. Pressing `p`
pauses all processes; `c` continues all.

**Stepping in multi-process programs:** step commands (`n`, `s`, `o`, `f`, `r`) apply only to the
process whose stack is currently shown in the Code View (the one that hit the breakpoint).
Other processes remain paused throughout the step. To step in a different process, open
the Processes tab and select it first. The Code View then switches focus to that process,
and subsequent step commands operate on it.

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

Remote attachment is useful in situations where you can't launch the debuggee directly
with `tdb`, for example, if it is launched from another program or runs in an environment
where you can't install `tdb`.  Two requirements must still be met though:
1. the `debugpy` package must be installed in the debuggee's Python environment
2. you need write access to the debuggee's code to add the following code at the point
where you want to attach the debugger:

```python
# In the target program:
import debugpy
debugpy.listen(("0.0.0.0", 5678))
print("Waiting for tdb to attach on port 5678...")
debugpy.wait_for_client()  # optional: pause until debugger connects
print("tdb is attached!")
```

When the debuggee runs and hits the `debugpy.wait_for_client()` line, it starts a
debugpy server listening on port 5678.
Attach `tdb` to it with the `-r` switch, specifying the host and port.
If the debuggee is on the same machine, you can omit the host or use `localhost`.
This example assumes the debuggee runs on 192.168.1.10 and listens on port 5678:

```bash
# Attach from tdb:
tdb -r 5678   # to localhost
tdb -r 192.168.1.10:5678

# With breakpoints:
tdb -r 5678 -k my_program.py:42
```

All debugging features (breakpoints, stepping, variable inspection, threads, processes,
async tasks) work in remote attach mode. The Code View automatically navigates to the
source file when the program stops.

**Mapping remote paths to local copies (`--local-root` / `--remote-root`):** when the
debuggee lives on another machine, or in a container, or simply in a different directory
on the same machine, the source paths it reports (and the paths it expects breakpoints
to refer to) won't match anything on the `tdb` host. To bridge that gap, give `tdb` one
or more `--local-root` / `--remote-root` pairs. Each `--local-root` points at a local
directory containing a copy of the code; each `--remote-root` is the corresponding path
on the debuggee. The two flags must be supplied in equal numbers and are paired in CLI
order via `zip()`, so the first `--local-root` matches the first `--remote-root`, the
second matches the second, and so on. `debugpy` then translates paths bidirectionally:
breakpoints set on a local file land on the matching remote file, and source paths
returned in stopped-events / stack-traces are rewritten back to the local copy so the
Code View loads directly from disk (no DAP `source` round-trip needed).

These flags are required whenever you want to set a `-k` breakpoint against a remote
debuggee whose code lives at a different path than your local copy. For example, if the
remote runs `program.py` at `/path/to/code/program.py` and your local copy is at
`/local/project/code/program.py`, set a breakpoint at line 321 with:

```bash
tdb -r RHOST:15678 \
    --local-root /local/project/code \
    --remote-root /path/to/code \
    -k program.py:321
```

With `--local-root` set, a relative `-k FILE:LINE` is resolved by searching each
`--local-root` directory in CLI order (first match wins); absolute paths still work as
before. Multiple pairs can be supplied to mirror multiple source trees (e.g. an
application directory and a shared library directory) in one invocation.

### External Terminal Support

Some Python programs, notably text user interfaces, use terminal control
codes and require direct access to the terminal to function properly. 
Such programs can be debugged with `tdb` by having it launch the debuggee in
a separate terminal:

```bash
tdb --terminal xterm my_tui_app.py
```

The debuggee runs in a separate window of the specified terminal. Supported choices:
`xterm`, `konsole`, `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`,
`wezterm`, `terminator`. The selected terminal must be on `PATH`. Debugging
proceeds as usual in the terminal where `tdb` was invoked.

This feature only works in graphical environments where external terminals are available.

### Keybinding Schemes

```bash
tdb --keybindings vim my_program.py    # default
tdb --keybindings emacs my_program.py
tdb --keybindings default my_program.py
```

The keybinding choice is saved to `~/.config/tdb/config.json` and remembered for subsequent
runs. View the full keybinding reference from the menu: **Configure > Keybindings**.

## JSON-RPC Server Mode

`tdb` includes a built-in debug server for programmatic control which is useful for scripted
debugging, CI pipelines, or AI-assisted debugging workflows.

### Headless Mode (no TUI)

```bash
python -m tdb --headless my_program.py &
```

The server listens on `http://127.0.0.1:8150/rpc` (change with `--server-port`).

### Dual Mode (TUI + server)

```bash
tdb --server my_program.py
```

Both the interactive TUI and the JSON-RPC server run simultaneously.

### RPC Protocol

Send POST requests with `{"action": "...", "params": [...]}`. Responses return
`{"timestamp": "...", "success": true/false, "value": "..."}`.

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
| `get_source` | `["file_path"]` | Read a source file |
| `list_threads` | `[]` | List all threads |
| `inspect_thread` | `[thread_id]` | Inspect a specific thread |
| `list_processes` | `[]` | List child processes (multiprocessing) |
| `inspect_process` | `["name_or_pid"]` | Inspect a specific child process |
| `list_tasks` | `[]` | List all asyncio tasks |
| `inspect_task` | `["task_name"]` | Inspect a specific asyncio task |
| `wait_graph` | `[]` | Show wait graph + any deadlock cycles |
| `restart` | `[]` | Restart session (preserves breakpoints) |
| `quit` | `[]` | Shut down |

### SSE Event Stream

Subscribe to real-time debug events:

```bash
curl -N http://127.0.0.1:8150/events
```

Events: `initialized`, `stopped`, `continued`, `terminated`, `exited`, `output`.
Each is JSON with `event`, `data`, and `timestamp` fields.

## Recording and replaying sessions

`tdb --record session.jsonl prog.py` runs a normal TUI session and captures
your debugging actions including breakpoints (including `-k`/`-t` and persisted
ones), stepping, continue/pause, Evaluate-console entries, stack-frame
navigation, variable expansion, restart, quit. The session is
written to `session.jsonl` as
JSON-RPC commands. Works with launch mode (any language) and `-r`
remote attach.

Replay it two ways:

- `tdb --replay session.jsonl` launches the recorded
  program headless, feeds every recorded command through the same RPC
  dispatch `tdb --server` uses, and prints a transcript (recorded time,
  command, verbatim result, interleaved program output). Exit code 0 iff
  every command succeeded. Add `--timing` to reproduce the original
  pacing, `--replay-timeout S` to bound each stop-wait (default 30 s).
- Against a live server: start `tdb --server prog.py`, then feed line 2
  onward of the file to `POST /rpc` . Each line is a valid request body:

      tail -n +2 session.jsonl | while read line; do
          curl -s -X POST -H 'Content-Type: application/json' \
               -d "$line" http://127.0.0.1:8150/rpc
      done

  (On Windows, an equivalent loop in Python: read the file, skip the
  first line, `requests.post` each remaining line.)

Not captured: pure viewing (scrolling, search, modals, thread/task
lists), breakpoint enable/disable toggles, variable expansions when the
adapter reports no `evaluateName` (currently the Perl adapter), and
File > Open program switches.

## MCP Integration

tdb ships a Model Context Protocol (MCP) server (`tdb-mcp`) that exposes
the debugger as a curated set of tools an AI agent can call. The MCP
server is a third in-process consumer of the same dispatch handlers the
TUI and the HTTP server use so an agent gets the same lock semantics,
including the pause-during-continue bypass, and the same DAP-backed
inspection surface.

For a worked end-to-end example (prompting an agent to find two
runtime bugs in a sample program) see
[docs/tutorial-mcp-debugging.md](docs/tutorial-mcp-debugging.md) and
its companion `examples/sales_report_buggy.py`.

### Running the MCP server

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

### Tool surface (16 tools, curated)

| Cluster | Tools |
|---------|-------|
| Lifecycle | `debug_launch`, `debug_attach`, `quit` |
| Control | `control(action, timeout_s=30)` where `action ∈ {continue, next, step_in, step_out, pause, wait_for_stop}` |
| Inspection | `inspect(expressions)`, `read_source(file_path)`, `stack_trace()`, `status()`, `get_output()` |
| Breakpoints | `set_breakpoint(spec, condition?, hit_condition?)`, `remove_breakpoint(spec)`, `list_breakpoints()` |
| Differentiators | `threads(thread_id?)`, `tasks(task_name?)`, `processes(name_or_pid?)`, `wait_graph()` |

`control` is intentionally one tool that takes an action enum. The six
underlying RPC actions share a return shape, and agents perform
measurably better with a small surface than with one tool per action.
`threads` / `tasks` / `processes` overload list-vs-inspect via a
single optional argument for the same reason.

`debug_launch` accepts optional `lang` and `adapter` parameters mirroring the
CLI's `--lang`/`--adapter`; when omitted, the language is auto-detected from
`program`, so an agent can hand it a compiled binary directly. The
`tasks`/`processes`/`wait_graph` tools stay registered for every language but
return a structured "not supported when debugging C/C++"-style error for
non-Python debuggees.

### Agent flow for a long-running step

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

### Security caveat

`inspect` calls debugpy's `evaluate`, which is **arbitrary Python
execution in the debuggee process**. This is inherent to a debugger and
not a tdb-specific concern, but MCP clients (and the humans running
them) should apply appropriate permission models: don't auto-approve
`inspect` against untrusted expressions, and don't expose `tdb-mcp` on
a network (stdio transport only by design).

### Deferred / out of scope (v1)

- SSE-style event push: `control` and `wait_for_stop` make polling
  efficient enough; events would also need uneven MCP-client support.
- HTTP / streamable-HTTP transports: would require auth (which the
  HTTP RPC server also currently lacks); stdio inherits the trust of
  the process that spawned it.
- Multi-session: one debug session per MCP process.

## CLI Reference

```
usage: tdb [-h] [-v] [-r [HOST:]PORT] [--cwd CWD] [--no-stop-on-entry]
           [--no-just-my-code] [--no-subprocess] [--python PYTHON] [--pv]
           [--lang LANGUAGE] [--adapter ADAPTER]
           [--keybindings {default,vim,emacs}]
           [--terminal {xterm,konsole,gnome-terminal,ghostty,kitty,iterm2,warp,wezterm,terminator}]
           [--local-root PATH] [--remote-root PATH]
           [--server] [--headless] [-k FILE:LINE|LINE] [--server-port SERVER_PORT] [-d] [--doc-text]
           [program] [args ...]
```

| Flag | Description |
|------|-------------|
| `-r HOST:PORT` | Attach to a remote debugpy server |
| `--local-root PATH` | Local directory containing a copy of remote code (repeat to mirror multiple trees). Pair with `--remote-root`; counts must match. Required when `-k` sets a breakpoint against a remote debuggee whose code lives at a different path. |
| `--remote-root PATH` | Remote directory matched to `--local-root` (same CLI position via `zip()`). |
| `-k`, `--breakpoint FILE:LINE|LINE` | Set a breakpoint (may be repeated). Passing `-k` implies `--no-stop-on-entry` so the program runs straight to the first breakpoint. |
| `-t`, `--to-line FILE:LINE|LINE` | Like `-k`, but the breakpoint is not saved to the breakpoints file, it just takes you to that spot in the code for this session (may be repeated). |
| `--no-stop-on-entry` | Do not pause at the first line (default: stop on entry; automatic when `-k` is given) |
| `--cwd DIR` | Working directory for the debuggee |
| `--python PATH` | Python interpreter for the debuggee (Python targets only) |
| `--pv` | Shorthand for --python .venv/bin/python |
| `--lang LANGUAGE` | Debuggee language (`python`, `cpp`, `perl`); default: auto-detect from the target |
| `--adapter ADAPTER` | Debug adapter within the language (e.g. `--lang cpp --adapter lldb-dap`); default: the language's standard adapter |
| `--no-just-my-code` | Step into stdlib/site-packages code instead of skipping it
  (default: skipped). On uncaught exceptions, the crash modal always shows the full traceback
  including library frames, regardless of this flag. |
| `--no-subprocess` | Disable debugpy's subprocess tracking (use when debugging `tdb` itself) |
| `--terminal TERM` | Run debuggee in the named external terminal: `xterm`, `konsole`,
  `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`, `wezterm`, or `terminator` |
| `--keybindings SCHEME` | `default`, `vim`, or `emacs` (saved to config) |
| `--server` | Enable JSON-RPC server alongside TUI |
| `--headless` | JSON-RPC server only, no TUI |
| `--server-port PORT` | Server port (default: 8150) |

## Configuration

On UNIX-like systems (Linux, macOS, FreeBSD, etc.),
`tdb` stores configuration and breakpoints in `~/.config/tdb/`.
On Windows, it uses `%APPDATA%\tdb\`.

| File | Contents |
|------|----------|
| `config.json` | User preferences (keybinding scheme, color theme, step mode, adapter overrides) |
| `breakpoints.json` | Breakpoints from previous sessions, keyed by project directory |

Adapter-related keys in `config.json`: `adapters` maps an adapter id to an
executable path (`{"adapters": {"lldb-dap": "/opt/llvm/bin/lldb-dap"}}`), and
`default_adapters` picks a language's default adapter
(`{"default_adapters": {"cpp": "lldb-dap"}}`).

**Perl is a special case:** `perl-tdb` is tdb's own bundled adapter (always
found; it's Python code, not an external executable), so
`adapters.perl` doesn't select an adapter binary. Instead it names the
*Perl interpreter* tdb should spawn to run the debuggee:
`{"adapters": {"perl": "/path/to/perl"}}`. Use this when the `perl` on
`PATH` is too old (< 5.18) or you need a specific `perlbrew`/`plenv` version.

Breakpoints are saved on exit and restored when debugging a program in the same
directory. Each project's breakpoints are independent. Breakpoints set with
`-t`/`--to-line` are the exception: they behave like `-k` breakpoints during
the session but are never saved.

**Step mode** (`step_mode` in `config.json`) controls how `n` (step over) and `s` (step
into) handle multi-line source statements:

| Value | Behavior |
|-------|----------|
| `"statement"` (default) | A multi-line statement (e.g. a `gather(...)` call spanning five lines) is one step. The debugger keeps issuing DAP steps until execution leaves the statement, then stops on the next logical line. |
| `"line"` | debugpy's native per-line behavior (stops on every physical line, including each interior sub-line of a multi-line expression)|

Change it from the menu (**Configure > Step Mode**); the choice is saved immediately and
applies to all future sessions. Breakpoint hits, exceptions, and pauses always interrupt
a statement step, so a breakpoint set on a sub-line of a multi-line expression still
fires as expected.

## Tech Stack

- [textual](https://github.com/Textualize/textual) : TUI framework
- [debugpy](https://github.com/microsoft/debugpy) : Debug Adapter Protocol implementation for Python
- [gdb](https://sourceware.org/gdb/) / [lldb-dap](https://lldb.llvm.org/resources/lldbdap.html) : optional, user-installed DAP adapters for C/C++
- [pygments](https://pygments.org/) : Syntax highlighting
- [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) : JSON-RPC server

## License

MIT


## Known Problems

This command
```
tdb --terminal gnome-terminal --python /path/to/venv/matplotlib/bin/python3 examples/double_pendulum.py
```
either ignores breakpoints or crashes after showing the first frame.
The `--python` argument must point to an installation with `matplotlib`.
