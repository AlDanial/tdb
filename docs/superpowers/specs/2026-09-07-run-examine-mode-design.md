# Run-Mode Examine — Design

**Date:** 2026-09-07
**Branch:** `run-examine-mode`
**Status:** Approved design, pre-implementation

## Problem

`tdb --run` runs a program headless and lets the user open the TUI when
it appears hung. Opening the TUI is the right tool for a deep look, but
often the user only wants a quick answer to "where is every thread right
now?" — without leaving the terminal, and in a form they can save and
compare. Today that requires opening the TUI, visiting the thread and
task views by hand, and detaching again.

## Goal

A new run-mode capability, **examine**: on a keystroke or signal, tdb
pauses the debuggee, captures the current time and the call stack of
every thread, async task, goroutine, and multiprocessing child, writes
the capture as one JSON line to stdout and/or a log file, and resumes
the program. The primary user is a human watching a hung program in the
terminal where `tdb --run` is running.

## Scope

**In version one**

- Manual trigger: Ctrl-\ (SIGQUIT) and SIGUSR2 on POSIX, Ctrl-Break
  (SIGBREAK) on Windows.
- Output routing: stdout by default, a log file, or both.
- Capture of threads and frames for every language that supports
  `--run`; asyncio tasks and multiprocessing children for Python;
  goroutine snapshot for Go; concurrency snapshot for Rust.
- JSON Lines output, one record per capture, JSON everywhere (no
  human-readable rendering).

**Deferred to their own specs** (the record format is designed so they
slot in without changing it)

- Selective capture (typed filters by file, function, thread, task,
  process). Users narrow with `jq` for now.
- Periodic capture (an interval timer setting the same trigger event).
- Conditional capture (time conditions inside tdb; variable conditions
  as conditional breakpoints with a capture-and-continue action).

## Decisions made during brainstorming

- **Trigger is a signal, not a raw-mode keypress.** Ctrl-E was the
  original proposal; it would require switching the terminal to cbreak
  mode, reading stdin during the run phase, handing the terminal back
  for each TUI episode, and restoring on every exit path, with a
  different stdin reader on Windows. The terminal driver already maps
  Ctrl-\ to SIGQUIT and Ctrl-Break to SIGBREAK in cooked mode, so tdb
  never touches terminal settings. SIGUSR2 mirrors the existing SIGUSR1
  convention for out-of-band triggering.
- **Ctrl-C behavior is unchanged.** A prompt-after-Ctrl-C variant was
  considered and rejected because it adds a keystroke to the existing
  path.
- **JSON everywhere.** One format to test and document; stdout gets the
  same line a log file gets.
- **Language-specific keys are omitted, not null**, when they do not
  apply: `tasks` only for Python, `goroutines` only for Go,
  `rust_concurrency` only for Rust.
- **No variables.** A capture is time plus stacks only.

## Architecture

### Run-phase flow with examine

```
tdb --run [--examine-log DEST ...] prog args
  └─ run_mode.py
       ├─ open log files (append) — fail before launch on a bad path
       ├─ DebugController(handler=SwappableEventHandler(ConsoleRunHandler))
       ├─ start, configure, print startup hint (now names the examine key)
       ├─ arm signals:
       │     SIGINT, SIGUSR1           → interrupt event   (unchanged)
       │     SIGQUIT, SIGUSR2 / SIGBREAK → examine event   (new)
       └─ wait (exited | interrupt | stopped | examine):
            ├─ exited     → exit with debuggee code
            ├─ interrupt  → pause, TUI episode        (unchanged)
            ├─ examine    → pause, collect, write, continue
            └─ stopped with examine outstanding → collect, write, continue
```

Priority when several events are set at once: exited, then interrupt,
then examine. A user who pressed both Ctrl-C and Ctrl-\ gets the
debugger; the examine request is dropped.

### Components

#### 1. CLI (`cli.py`)

- New flag `--examine-log DEST`, `action="append"`, metavar `DEST`. DEST
  is `-` for stdout or a file path. Omitting the flag means stdout.
  Repeating the flag writes to every DEST listed, so
  `--examine-log - --examine-log hang.jsonl` gives both.
- Validation: `--examine-log` without `--run` is a `parser.error`, in
  the style of the existing mode-conflict checks.
- Dispatch: `_run_run` passes the parsed DEST list to `run_mode.run`.
- Help text for `--run` gains one clause naming the examine key.

#### 2. Signals (`run_mode.py`)

- `_arm_signals` gains a second trigger callable for the examine
  signals. It arms SIGQUIT and SIGUSR2 on POSIX via
  `loop.add_signal_handler`, and SIGBREAK on Windows via
  `signal.signal` with `call_soon_threadsafe`, alongside the existing
  SIGINT and SIGUSR1 handling. The returned "installed" list covers all
  of them.
- `_disarm_signals` with `ignore=True` (during a TUI episode) sets the
  examine signals to `SIG_IGN`, so a stray Ctrl-\ inside Textual is a
  keypress and a stray SIGUSR2 from another terminal is a no-op rather
  than the default core dump. With `ignore=False` (final exit) SIGQUIT
  and SIGBREAK return to `SIG_DFL`.
- The adapter and debuggee tree already run in their own session or
  process group, so none of these signals reach the debuggee.
- Failure to install handlers (non-main thread) degrades to "no examine
  trigger" with a logged warning, matching current behavior for the
  interrupt signals.

#### 3. Startup hint

The existing stderr line
`tdb: running PROG — Ctrl-C or kill -USR1 PID opens the debugger`
gains a second clause, for example
`; Ctrl-\ or kill -USR2 PID writes a stack snapshot to hang.jsonl`
(`Ctrl-Break` on Windows; `stdout` when no file is configured).

#### 4. Capture cycle (`run_mode.py` loop)

State added to the loop: `examine` (asyncio.Event), `seq` (int, starts
at 1), `outstanding` (the requested-at time and seq of a pending
capture, or None).

- **Normal cycle.** `examine` set and the debuggee running: clear the
  event, record the request time, call `controller.pause` with the
  existing `_PAUSE_TIMEOUT`. On a landed stop: record the landed time,
  run the collector, write the record, `controller.continue_()`,
  `console.stopped.clear()`. Signals stay armed throughout; no TUI owns
  the terminal. Presses during a cycle coalesce into the single event
  and are cleared at the end of the cycle, so a double-press yields one
  record.
- **Pause does not land within the timeout.** Write a record with
  `status: "pending"` (no `landed_at`, empty `processes`), print the
  existing "blocked inside a single call" note, set `outstanding`, and
  return to the wait. When `stopped` later fires with `outstanding`
  set, complete the capture with the same `seq` and `status: "ok"`,
  then continue. A Ctrl-C arriving while `outstanding` is set clears it
  and opens the TUI on that stop, exactly as today.
- **Exit during capture.** If `console.exited` is set after the pause
  returns, write a record with `status: "exited"` and `exit_code`, then
  leave the loop with that code, mirroring the existing Ctrl-C race
  handling.
- **Already stopped.** A spontaneous stop (a breakpoint left over from a
  TUI episode) still opens the TUI when no examine is outstanding;
  nothing changes there.
- **Collection failures never escape.** Every sub-collector runs under
  its own try/except; a failure contributes a string to `errors` and
  the cycle still continues the debuggee.

#### 5. Collector (new `src/tdb/examine.py`)

`async def collect(controller, *, trigger, seq, requested_at,
landed_at, launched_at) -> dict`. Pure with respect to I/O: it reads
from the controller and returns a plain dict. Steps:

1. Header fields (see record schema).
2. **Processes.** Parent first: `controller.client`, pid and name from
   the process collector when it succeeds, else pid from the adapter or
   null. Then each attached child from the controller's child clients,
   keyed by pid.
3. **Threads per process.** `client.threads()`, then
   `client.stack_trace(thread_id, levels=EXAMINE_FRAME_CAP)` per
   thread. `EXAMINE_FRAME_CAP` is a module constant (200): the client's
   default of 20 is tuned for the TUI and can hide the interesting
   frame in a deep hang. Each child client is queried under a short
   per-child timeout (`_timeouts` module, new constant) so one slow
   child cannot stall the capture; a timed-out child yields an error
   entry and an empty thread list.
4. **Tasks** (Python only, profile `task_inspection`): the existing
   `InspectService.collect_tasks`; each task contributes name, state,
   awaiting label, and its frames.
5. **Goroutines** (Go only): `InspectService.collect_go_concurrency`
   and its `to_dict()`.
6. **Rust concurrency** (Rust only):
   `InspectService.collect_rust_concurrency` and its `to_dict()`.

Frame conversion: innermost first as the adapter reports; fields
`function`, `file`, `line`. Frames with no source keep `function` and
carry null `file` and `line`.

#### 6. Writer (`src/tdb/examine.py`)

`write(record, sinks)` serializes once with compact separators, writes
the line plus newline to each open text stream, and flushes each. Sinks
are `sys.stdout` and/or file objects opened in append mode with UTF-8
encoding. After writing, one stderr notice:
`tdb: examine #3 written to stdout, hang.jsonl`. A write error on a
sink is reported to stderr once per sink and does not stop the run.

Stdout writes go through the same stream the debuggee's output uses, so
ordering with program output is preserved.

#### 7. Lifecycle

Log files open before `controller.start` so a bad path fails before the
program launches. Directories are not created. Files close in the run
function's `finally`, on every exit path: normal exit, terminate from
the TUI, and escaping exceptions.

## Record schema

One JSON object per line. `schema` is bumped only on an incompatible
change.

```json
{
  "schema": 1,
  "seq": 3,
  "trigger": "SIGQUIT",
  "status": "ok",
  "requested_at": "2026-09-07T14:02:11.482-07:00",
  "landed_at": "2026-09-07T14:02:11.511-07:00",
  "elapsed_s": 87.3,
  "language": "python",
  "program": "/home/al/work/app.py",
  "processes": [
    {
      "pid": 41213,
      "role": "parent",
      "name": "MainProcess",
      "threads": [
        {
          "id": 1,
          "name": "MainThread",
          "frames": [
            {"function": "wait", "file": "/usr/lib/python3.13/threading.py", "line": 359},
            {"function": "main", "file": "/home/al/work/app.py", "line": 42}
          ]
        }
      ],
      "tasks": [
        {"name": "worker-2", "state": "PENDING", "awaiting": "Lock.acquire",
         "frames": [{"function": "run", "file": "/home/al/work/app.py", "line": 18}]}
      ]
    }
  ],
  "errors": []
}
```

Field notes:

- `trigger`: `"SIGQUIT"`, `"SIGUSR2"`, or `"SIGBREAK"`.
- `status`: `"ok"`, `"pending"`, or `"exited"`. A pending record has no
  `landed_at` and an empty `processes` list; a later record with the
  same `seq` and status `ok` completes it. An exited record carries
  `exit_code`.
- `requested_at`, `landed_at`: local time with UTC offset, ISO 8601,
  millisecond precision. `elapsed_s`: seconds since the launch request,
  one decimal.
- `language`: the language profile's `id` (lowercase, e.g. `"python"`,
  `"go"`, `"rust"`).
- `role`: `"parent"` or `"child"`. Children appear in pid order after
  the parent.
- `tasks` appears only on Python process entries and only when the
  collector returns at least one task.
- `goroutines` (top level, Go only) and `rust_concurrency` (top level,
  Rust only) hold the existing snapshot dicts verbatim, including their
  wait graphs and findings. Both keys are absent for other languages.
- `errors`: list of strings, one per failed sub-collector, e.g.
  `"tasks: evaluate failed"` or `"child 41230: timeout"`.

## Error handling summary

| Situation | Behavior |
|---|---|
| `--examine-log` without `--run` | parser error |
| Log path unwritable | error before launch, exit 2 |
| Pause does not land | pending record, note on stderr, completed on later stop |
| Program exits mid-capture | exited record, exit with program's code |
| Sub-collector raises | entry in `errors`, capture continues |
| Child client slow | per-child timeout, entry in `errors` |
| Sink write fails | one stderr notice per sink, run continues |
| Signal handlers cannot install | warning, no examine trigger |
| Ctrl-\ or SIGUSR2 during a TUI episode | ignored |

## Testing

- **Collector units** (`tests/unit/test_examine.py`): fake controller
  with scripted threads, frames, and child clients. Cover record shape,
  frame ordering, null-source frames, per-child timeout, omission of
  language-specific keys, and `errors` population when a sub-collector
  raises.
- **Writer units**: string-buffer sinks; single serialization, newline
  termination, flush, per-sink error isolation.
- **Loop units** (extend `tests/unit` run-mode coverage with the
  existing fake episode and fake controller): four-event priority,
  coalesced presses, pending-then-landed completion, Ctrl-C cancelling
  an outstanding examine, exit during capture, sequence numbering.
- **Integration** (`tests/integration/test_run_mode.py` additions):
  send SIGUSR2 to a real `tdb --run` and parse the line for a threaded
  Python program, an asyncio program, and a multiprocessing program;
  Go and Rust programs under the existing toolchain skip conditions;
  a SIGBREAK case guarded to Windows; a log-file case checking append
  behavior across two captures.

## Documentation

- README run-mode section: the examine key per platform, the flag, the
  record shape, the pending caveat, and a `jq` example for narrowing.
- `--run` help text: one added clause.
