# Run Mode (`--run`)

`tdb`'s run mode acts as a job shepherd.
It runs your unmodified program at nearly full speed, without bringing up the TUI,
and listens for two interrupt signals:

- `Ctrl-C` pauses the program and brings up TUI, placing you at the currently
active line of code.  From there you can debug interactively as usual.  When
you quit the TUI, you can either detach and let the program continue running, or terminate it.

- `Ctrl-\` temporarily pauses the program and prints a JSON-formatted stack trace to stdout
or a log file, then resumes the program.

```bash
tdb --run my_program.py args...
```

The debuggee's stdout/stderr stream straight to the terminal, and for the
languages with stdin forwarding (see
[Console Output and Input](console.md)) the program reads
its stdin from that terminal too. If the program exits on
its own, `tdb` exits with the same code:

```bash
tdb --run my_program.py; echo $?   # my_program.py's own exit code
```

**Windows caveat:** the Ctrl-Break trigger is untested. Unlike Ctrl-C, a console
Ctrl-Break is delivered to every process attached to the console, including the debug
adapter and the program itself, so it may terminate the program instead of snapshotting
it. There is no signal-based alternative on Windows.

`tdb` pauses the program, writes one JSON line describing the call stack of every thread
(plus asyncio tasks and multiprocessing children for Python, goroutines for Go, and
the concurrency snapshot for Rust), and resumes the program. Ctrl-C behavior is
unchanged. By default the line goes to stdout; `--examine-log DEST` sends it to a file
instead (`-` means stdout; repeat the flag to write to several places):

```bash
tdb --run --examine-log hang.jsonl my_program.py     # file only, appended
tdb --run --examine-log - --examine-log hang.jsonl my_program.py   # both
```

Each record looks like (abridged):

```json
{"schema":1,"seq":1,"trigger":"SIGQUIT","status":"ok",
 "requested_at":"2026-09-07T14:02:11.482-07:00","landed_at":"2026-09-07T14:02:11.511-07:00",
 "elapsed_s":87.3,"language":"python","program":"/home/al/work/app.py",
 "processes":[{"pid":41213,"role":"parent","name":null,
   "threads":[{"id":1,"name":"MainThread","frames":[
     {"function":"wait","file":"/usr/lib/python3.13/threading.py","line":359},
     {"function":"main","file":"/home/al/work/app.py","line":42}]}],
   "tasks":[{"name":"worker-2","state":"PENDING","awaiting":"Lock.acquire","frames":["run (/home/al/work/app.py:18)"]}]}],
 "errors":[]}
```

Frames are innermost first. `tasks` appears only for Python programs with live asyncio
tasks; `goroutines` only for Go; `rust_concurrency` only for Rust. `status` is
`"pending"` when the pause could not land within a few seconds (the program is blocked
inside a single call) -- a second record with the same `seq` and `status: "ok"` follows
when it does land. If you press Ctrl-C or the program exits before it lands, no
completing record is written. `errors` lists any sub-collector that failed, so a partial
record is explicit about what is missing. A stderr line confirms each capture and where
it went.

Narrow a long capture with `jq`, for example only threads with a frame in your own code:

```bash
jq -c '.processes[].threads[] | select(any(.frames[]; .file != null and (.file|test("/work/"))))' hang.jsonl
```

**Quitting an adopted session** adds a third choice to the usual quit dialog:
- `d` (or `q`) -- detach and resume: the program keeps running headlessly; interrupt it
  again later the same way.
- `t` -- terminate the program and quit `tdb`.
- `Esc` -- cancel, stay in the TUI.

A breakpoint set during one of these episodes stays live after you detach; if it's hit,
`tdb` reopens the TUI there, just like an interrupt.

If the program instead dies on an unhandled exception -- in Python this includes a
nonzero `sys.exit()`, which `debugpy` treats as an uncaught `SystemExit` -- `tdb` opens
the TUI at the point of failure instead of exiting silently, so you can inspect the
crash. Exit-code passthrough applies only to a clean exit during the headless phase.

Supported languages: Python, Perl, Bash, Tcsh, Ruby, PowerShell 7, C/C++/Fortran
(both `gdb` and `lldb-dap`), and native OCaml (both `gdb` and `lldb-dap`;
bytecode/`ocamlearlybird` sessions don't support pause, so `--run` is
unavailable for those). `--run` is rejected up front for any other
language.

**Limitation:** pausing is cooperative. If the debuggee is blocked inside a single
blocking external call or syscall, the pause can't land until that call returns --
`tdb` prints a notice and opens the TUI once the stop actually arrives.

**Breakpoints:** run mode never uses breakpoints saved in `breakpoints.json`, and
never writes to it either, so the breakpoints you saved for normal sessions are left
untouched. Breakpoints given on the command line with `-k`/`-t` are live from the
start: the program runs headlessly until it reaches one, and then `tdb` opens the TUI
there, just as Ctrl-C would. They stay live after you detach, so the next hit reopens
the TUI. In run mode `-k` and `-t` behave the same, since nothing is saved.

Add `--if EXPR` after a `-k`/`-t` to make that breakpoint conditional: the TUI opens
only when `EXPR` is true at that line. The expression uses the program's own language
(the debug adapter's expression syntax); for Bash it is a command whose exit status
decides, e.g. `--if '(( count > 7 ))'` or `--if '[[ $name == foo ]]'`. Tcsh doesn't
support conditions.

```bash
tdb --run -k 120 -k utils.py:45 my_program.py         # stop at either line
tdb --run -k module.py:27 --if "a > 7" my_program.py  # stop at line 27 only when a > 7
```

If a `Ctrl-\` snapshot's pause lands as a breakpoint hit, the snapshot is still
written, and then the TUI opens at the breakpoint instead of the program resuming.

**Incompatible flags:** `-r`, `--record`, `--replay`, `--replay-tui`, `--server`,
`--headless`, `--mcp`, `--terminal`.
