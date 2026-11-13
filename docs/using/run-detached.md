# Run Detached (`q` then `d`)

When you are done looking at a program but want it to finish, press `q` in
the TUI and then `d`. The `q` key opens a small menu:

```
q: quit (ends the program)
d: run detached
ESC: cancel
```

(For remote-attach sessions the first line reads `q: quit`, since quitting
leaves the program running anyway.)

`d` closes the TUI and lets the program run on, under `tdb`, in the terminal
you started it from. `q` `q` still ends the program, `ESC` still returns to the
debugger, and `Ctrl+q` is unchanged.

## What `d` does

1. Saves your breakpoints, exactly as a normal quit does, so they are there
   the next time you debug this program.
2. Removes every breakpoint and exception stop from the running session.
3. Closes the TUI and restores the terminal.
4. Replays the program output the TUI had already shown (the most recent
   4 MiB; if older output was dropped, a one-line note says so).
5. Prints a notice on stderr:
   `tdb: detached — my_program.py running; Ctrl-C interrupts it`
6. Resumes the program, relays its stdout and stderr, and forwards what you
   type to its stdin.
7. Exits with the program's exit code when the program ends.

Any stop the debugger still reports after detaching is resumed
automatically, so the program is never left frozen. The debugger cannot be
reopened. To be able to drop back into the TUI with `Ctrl-C`, start the
program with [`tdb --run`](run-mode.md) instead.

The program stays under its debugger: its output still travels over pipes,
so `isatty()` is false for its stdout and stderr, and it does not run any
faster than before.

## Standard input

Terminal input is forwarded to the program for Python, Perl, Bash, tcsh,
Ruby and PowerShell. It is not available for C, C++, Rust, Go and OCaml,
the same as in the TUI's console (see [Console Output and Input](console.md)).

## Ctrl-C

After detaching, `Ctrl-C` goes to the program and never reopens the
debugger. `tdb` then exits with status 130.

| Ctrl-C is delivered to the program (its own handler runs, otherwise it dies) | Ctrl-C ends the program (its handlers do not run) |
|---|---|
| Python, Bash, Perl, tcsh, Ruby | PowerShell |
| C, C++, Rust and native OCaml, under gdb or lldb-dap | bytecode OCaml (ocamlearlybird) |
| Go, under dlv | Any session on Windows where delivery is unavailable (Perl on Windows) |
| | tcsh and Ruby `--terminal` sessions: interrupt the script in its own terminal instead |

Caveats:

- **Perl:** if you press `d` while the script is already running and blocked
  reading stdin, `Ctrl-C` takes effect only once you enter a line. Pressing
  `d` while the program is stopped is unaffected.
- **gdb and lldb-dap** report a program killed by SIGINT as exit 0 (gdb) or
  2 (lldb-dap). So `tdb` reports 130 when the program exits with that code
  within 2 seconds of a `Ctrl-C`. A program that catches `Ctrl-C` and
  deliberately exits with 0 (gdb) or 2 (lldb-dap) that quickly is also
  reported as 130.
- **Python:** an exit status of 254 after a delivered `Ctrl-C` is reported
  as 130.

## `--terminal` and attach sessions

- With [`--terminal`](external-terminal.md), the program does not survive the
  debugger disconnecting, so `d` leaves `tdb` attached, silently, until the
  program exits.
- With [remote attach](remote-attach.md) or `tdb -a PID`, `d` removes the
  stops, resumes the program, and really disconnects. `tdb` prints
  `tdb: detached — my_program keeps running` and exits 0. If the disconnect
  was not acknowledged, it prints
  `tdb: detached — could not confirm my_program is still running` and
  exits 1.

## Exit codes

| Exit code | Meaning |
|-----------|---------|
| the program's own | the program ended by itself |
| 130 | `Ctrl-C` ended the program |
| 128 + N | `tdb` itself received signal N (SIGTERM gives 143, SIGHUP gives 129); the program is ended |
| 1 | the session ended without an exit code (for example, the debug adapter died) |

If the terminal goes away while `tdb` is relaying, the program is ended
too, rather than being left behind.

## Recording

`--record` writes a `detach` event. `--replay` and `--replay-tui` treat it as
the end of the session.
