# Eval Mode (`-e`/`--eval`)

Eval mode is essentially a code injection mechanism.  Rather than injecting
native code though, only expressions that are valid in the Evaluate View
can be injected (in Python, native expressions are processed correctly
in the Evaluate View).

In this way, Eval mode automates the common debugging action of setting a
breakpoint, running to it, evaluating (and printing the result of) an expression, then
resuming execution, all without opening the TUI. `-e` takes two arguments, a location and an expression:

```bash
tdb --eval examples/digits_of_pi.py:25 "print(f'{nr=}')" examples/digits_of_pi.py
```

sets a breakpoint at line 25, runs the program, and each time that line is
reached evaluates `print(f'{nr=}')` in the stopped frame, then continues.
Program output and evaluation output both stream to the terminal; when the
program exits, `tdb` exits with the same code (and with `1` if the session
ends without one--as with a crash or killed adapter--so a scripted `tdb -e ...
&& next` can trust the exit status). `Ctrl-C` stops the program and exits
`130`. Under `multiprocessing`, an eval point placed in child code is
evaluated in each child process that reaches it.

- The location is `FILE:LINE`, or a bare `LINE` of the program being debugged,
  and is snapped to the start of a logical statement just like `-k`.
- `-e` may be repeated to plant several eval points in one run.
- The expression is evaluated through the debug adapter (DAP `evaluate`,
  context `repl`), just like the Evaluate console: side effects are real
  and persist in the running program, for example
  `-e my_program.py:25 "limit = 10"` changes `limit` from line 25 onward.
- A bare expression's result is printed to stdout; statement-style
  expressions (`print(...)`, assignments) print nothing beyond their own
  output. An expression error is reported on stderr and the run continues --
  a typo isn't fatal, matching the Evaluate console.
- Saved breakpoints are ignored, and any other stop (e.g. an
  uncaught-exception stop) is continued untouched, so the program runs to
  completion unattended.
- For non-Python debuggees the expression must be in the debug adapter's
  own syntax, for example gdb's `print x` / `set var x=3` for C/C++/Fortran, and not
  necessarily the debuggee's language. The adapter may bind the breakpoint
  to the next executable line (gdb moves one off a declaration or brace);
  the eval point matches that bound line, so the expression still runs.

**Incompatible flags:** `--run`, `-r`, `-a`, `-k`/`-t`, `--record`,
`--replay`, `--replay-tui`, `--server`, `--headless`, `--mcp`, `--terminal`.
