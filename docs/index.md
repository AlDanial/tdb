# `textual-debugger`

`textual-debugger` (the package) provides `tdb` (the command-line tool and module),
a full-featured terminal-based debugger for Python and other languages
with a Debug Adapter Protocol (DAP) implementation.  In addition to Python,
`tdb` comes with built-in support for
- C, C++, Fortran (via `gdb` or `lldb-dap`)
- Rust (via `gdb` or `lldb-dap`, selected explicitly)
- Perl (via `perl -d`)
- Bash (via bash's own `DEBUG` trap; bash ≥ 4.4)
- Tcsh (via source instrumentation of a stock `tcsh`)
- Ruby (via the [debug gem](https://github.com/ruby/debug)'s `rdbg`; debug ≥ 1.9)
- OCaml (native via `lldb-dap`/`gdb`, bytecode via [ocamlearlybird](https://github.com/hackwaly/ocamlearlybird))
- Go (via [Delve](https://github.com/go-delve/delve)'s `dlv dap`)
- PowerShell 7 (via [PowerShell Editor Services](https://github.com/PowerShell/PowerShellEditorServices); Linux/macOS, Windows experimental)

`tdb` is built with [textual](https://github.com/Textualize/textual) and speaks
DAP to a pluggable debug adapter.
It provides a rich interactive interface for stepping through
code, inspecting variables, managing breakpoints, and evaluating expressions in
complex programs.

- PyPI: https://pypi.org/project/textual-debugger/
- GitHub: https://github.com/AlDanial/tdb

MIT License.  Copyright 2026 by Al Danial.

## Feature Overview

`tdb`:

- debugs multiple languages through the Debug Adapter Protocol: Python (via `debugpy`,
the richest feature set); C/C++ and Fortran (via `gdb -i dap` or `lldb-dap`); Rust (via `gdb` or
`lldb-dap`); Perl (via `perl -d`);
Bash (via bash's own `DEBUG` trap); Tcsh (via source instrumentation of a
stock `tcsh`); Ruby (via the debug gem's `rdbg`); OCaml (native via
`lldb-dap`/`gdb`, bytecode via ocamlearlybird); Go (via Delve's `dlv
dap`); and PowerShell 7 (via PowerShell Editor Services). Most languages are
auto-detected from the target; Rust intentionally requires `--lang rust`
(ref. [Multi-Language Debugging](languages/index.md)).

- supports debugging of synchronous, asynchronous, multi-threaded, and multi-process Python code.
It specifically supports modules
    - `asyncio` (with a built-in async task inspector and task wait graph)
    - `threading` (with a thread inspector)
    - `multiprocessing` / `concurrent.futures` (with automatic child process attachment and a process inspector)

- supports remote attachment to Python, Perl, Ruby, Rust, C/C++, and Go programs

- includes a JSON-RPC server mode, an MCP (v2) mode, and a `SKILL.md` file that enable
programmatic debug control, making it suitable for
automated, headless debugging workflows and AI-assisted debugging

- can spawn the debuggee in an external terminal to enable debugging TUI applications
built with `textual`, `prompt-toolkit`, `urwid`, `curses`, `rich`, and so on

- comes with a post-mortem exception hook that can be installed in Python programs
to have `tdb` pop open automatically at the first uncaught exception, and a live
breakpoint hook (`tdb.breakpoint()` in Python, `Devel::TdbRemote::breakpoint()`
in Perl, `Tdb.breakpoint` in Ruby, `tdb.Breakpoint()` in Go, `tdb_breakpoint()`
in C/C++ and Fortran, `tdb::breakpoint()` in Rust, `Tdb.breakpoint ()` in OCaml) that opens
`tdb` paused at a line of your choosing

- can be entirely keyboard-driven
making it suitable for operation in non-graphical environments (mouse support is
available in graphical environments)

## Gallery
<p align="center">
  <img src="https://raw.githubusercontent.com/AlDanial/tdb/main/gallery/async_breakpoint.png" alt="at breakpoint" width="300">
  <img src="https://raw.githubusercontent.com/AlDanial/tdb/main/gallery/async_task_graph.png" alt="task graph" width="300">
  <img src="https://raw.githubusercontent.com/AlDanial/tdb/main/gallery/multiprocessing_process_3.png" alt="multiple processes" width="300">
  <img src="https://raw.githubusercontent.com/AlDanial/tdb/main/gallery/threading_list.png" alt="thread list" width="300">
</p>

Videos:
- [tdb basics](https://youtu.be/2_qf2WZDHuA) views, keybindings, breakpoints, stepping, variable modification, call stack
- [asyncio tasks](https://youtu.be/vM4tODuqMGg) inspect asyncio tasks and their wait graph; code mod to allow pause
- [threads and processes](https://youtu.be/J8LOARLs2oQ) inspect variables and call stacks in multiple threads and processes
- [external terminal](https://youtu.be/121aihjAQ8g) run the debuggee in a separate terminal--ideal for debugging TUI applications
