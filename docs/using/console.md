# Console Output and Input

The Console View captures stdout (normal text) and stderr (red text) from the debuggee
in real time.

Programs that read from stdin (`input()`, `<STDIN>`, `read`, `gets`, …) get
their input from the same view: while the program runs, an input line sits
under the output log. `Ctrl+O` focuses it, `Enter` sends the line (plus a
newline) to the program and echoes it in cyan next to the program's prompt,
and `Ctrl+D` sends end-of-file. A bare `Enter` sends an empty line, which is
a real answer to `input()`. The line disappears when the program exits or
when the session has no stdin to feed.

Stdin forwarding works for the languages whose program `tdb` launches on a
pipe it owns: Python, Perl, Bash, Tcsh, Ruby, and PowerShell. It is not
available for the compiled languages — C/C++/Fortran, Rust, OCaml, and Go — because
their adapters (`gdb -i dap`, `lldb-dap`, `dlv dap`) spawn the debuggee
themselves and give `tdb` no handle on its stdin; the input line stays
hidden there. It is also unavailable for remote attach (`-r`), where the
program's stdin belongs to whoever started it, and under `--terminal`,
where the program reads from its own terminal window. For those cases run
the program in an external terminal with `--terminal` (launch mode only),
or give it its input another way (a file, a pipe on the command line).

Every line typed here is captured by `--record` as a `stdin` gesture and
replayed by `--replay`/`--replay-tui`, and the JSON-RPC server exposes the
same thing as the `stdin` / `stdin_eof` actions.

If your program prints a lot, or uses colors or
terminal control codes, run the program in an external terminal
with `--terminal` for a better experience.
The `--terminal` switch requires a graphical environment and a compatible
terminal emulator.
