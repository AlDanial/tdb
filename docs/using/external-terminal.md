# External Terminal Support

Some programs, notably text user interfaces, use terminal control codes and
require direct access to the terminal to function properly. Such programs
can be debugged with `tdb` by having it launch the debuggee in a separate
terminal:

```bash
tdb --terminal xterm my_tui_app.py
```

`--terminal` works for every language `tdb` can *launch* (as opposed to
attach to): Python, Perl, Bash, Tcsh, Ruby, and C/C++/Fortran or native OCaml
sessions via `--adapter lldb-dap`. Neither `gdb -i dap` (the default C/C++/Fortran
adapter, and an alternate for native OCaml) nor `ocamlearlybird` (the
bytecode OCaml adapter) has any terminal integration -- `tdb` rejects
`--terminal` up front for both, with an error pointing at `--adapter
lldb-dap` instead. Go and PowerShell don't support `--terminal` either
(`dlv dap` has no terminal-routing mode; PSES likewise). `--terminal` also
only applies when `tdb` launches the program itself; it is rejected for
remote-attach (`-r`), since there's no program for `tdb` to spawn a
terminal around.

The debuggee runs in a separate window of the specified terminal, including
all of its stdin/stdout/stderr. Keyboard input, program output, and
anything the program itself draws to the terminal all happen in that
window, not in `tdb`'s Console View (whose stdin line is hidden in this
mode). Supported choices:
`xterm`, `konsole`, `gnome-terminal`, `ghostty`, `kitty`, `iterm2`, `warp`,
`wezterm`, `terminator`. The selected terminal must be on `PATH`. Debugging
(breakpoints, stepping, variable inspection, the evaluate console) proceeds
as usual in the terminal where `tdb` was invoked.

This feature only works in graphical environments where external terminals are available.
