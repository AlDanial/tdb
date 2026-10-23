# Live Breakpoint Hook

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

Perl, Ruby, Go, C/C++/Fortran, Rust, and OCaml programs get the same hook as
`Devel::TdbRemote::breakpoint()`, `Tdb.breakpoint`, `tdb.Breakpoint()`,
`tdb_breakpoint()`, `tdb::breakpoint()`, and `Tdb.breakpoint ()`; see
[Perl](../languages/perl.md), [Ruby](../languages/ruby.md), [Go](../languages/go.md), [C/C++/Fortran](../languages/c-cpp-fortran.md),
[Rust](../languages/rust.md), and [OCaml](../languages/ocaml.md).
