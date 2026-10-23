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

**Documentation: https://textual-debugger.readthedocs.io/**
(also available offline with `tdb --doc`, or as plain text with `tdb --doc-text`)

- PyPI: https://pypi.org/project/textual-debugger/
- GitHub: https://github.com/AlDanial/tdb

MIT License.  Copyright 2026 by Al Danial.

## Highlights

- Debugs synchronous, asynchronous, multi-threaded, and multi-process Python,
  with inspectors for `asyncio` tasks (including a task wait graph), threads,
  and `multiprocessing` / `concurrent.futures` children
- Remote attach to Python, Perl, Ruby, Rust, C/C++, and Go programs
- JSON-RPC server, MCP (v2) server, and a `SKILL.md` for headless and
  AI-assisted debugging
- Runs the debuggee in an external terminal to debug TUI applications
- Post-mortem exception hook and live breakpoint hooks (`tdb.breakpoint()` and
  its counterparts in Perl, Ruby, Go, C/C++/Fortran, Rust, and OCaml)
- `pytest --pdbcls=tdb.pytest_debugger:Debugger` integration
- Run mode (`--run`) to attach the TUI to a running job on Ctrl-C, and eval
  mode (`--eval`) to print expressions at chosen lines without the TUI
- Entirely keyboard-driven, with vim, emacs, and Notepad-style keybindings;
  mouse support in graphical environments

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

## Installation

```bash
uv pip install textual-debugger        # or: pip install textual-debugger
uvx --from textual-debugger tdb my_program.py   # run without installing
pip install "textual-debugger[mcp]"    # add the optional MCP server
```

## Quick Start

```bash
tdb --doc                        # full documentation in a terminal Markdown viewer
tdb --info                       # versions and locations of tdb, its config, and every debugger it would use
tdb my_program.py arg1 arg2      # debug a Python program (stops at the first line)
tdb -k 20 -k module.py:14 my_program.py   # run to breakpoints
tdb ./myprog                     # debug a native executable built with -g
tdb script.pl                    # ... or a Perl, Bash, Tcsh, Ruby, PowerShell script
tdb -r remotehost:5678           # attach to a remote debugpy server
```

See the [Quick Start](https://textual-debugger.readthedocs.io/quickstart/)
page for many more examples, and the
[CLI Reference](https://textual-debugger.readthedocs.io/reference/cli/)
for every option.

## Acknowledgments

`tdb` is built with Will McGugan's [textual](https://github.com/Textualize/textual)
and Microsoft's [debugpy](https://github.com/microsoft/debugpy), and was
inspired by Andreas Klöckner's [pudb](https://pypi.org/project/pudb/).
See [About](https://textual-debugger.readthedocs.io/about/) for the full
acknowledgments.
