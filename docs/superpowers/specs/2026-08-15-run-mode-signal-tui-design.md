# `--run` Mode with Signal-Triggered TUI — Design

**Date:** 2026-08-15
**Branch:** `catch_breakpoint_signal`
**Status:** Approved design, pre-implementation

## Problem

When a program appears to be hung, the user wants to know where it is
stuck and what its state is. Today tdb always opens the TUI up front and
(by default) stops on entry — there is no way to run a program at full
speed with no debugger UI and then drop into the debugger *later*, at
the moment the user decides something is wrong.

## Goal

Two coupled capabilities:

1. `tdb --run PROGRAM [ARGS...]` runs the debuggee with **no TUI**, **no
   stop on entry**, and **no breakpoints** (saved breakpoints are not
   installed; `-k`/`-t` are rejected). Debuggee output flows to the
   terminal. When the program exits, tdb exits with the program's exit
   code.
2. When the user sends a signal to tdb — **Ctrl-C** (SIGINT) in the
   terminal, or **`kill -USR1 <tdb pid>`** (POSIX only) from another
   terminal — tdb pauses the debuggee via DAP `pause` and opens the full
   TUI with the cursor on the line where execution stopped.

Quitting the TUI offers "detach & resume": the program continues, tdb
returns to headless waiting, and the user can interrupt again later.

## Decisions made during brainstorming

- **Flag spelling:** `--run`, long form only. `-r` is already taken by
  `--remote-attach`; no short alias to avoid confusion.
- **Trigger signals:** SIGINT (Ctrl-C, portable including Windows) plus
  SIGUSR1 on POSIX for out-of-band triggering. The debuggee never
  receives these signals: the adapter/debuggee tree is isolated from
  the terminal's process group (`start_new_session=True` on POSIX —
  already the norm in `dap/client.py` — and `CREATE_NEW_PROCESS_GROUP`
  on Windows).
- **TUI quit:** returns to run mode by default ("detach & resume"); an
  explicit alternative terminates the program and exits tdb.
- **Language scope:** all languages whose adapter honors DAP `pause`
  while running. Python (debugpy) and bash support it today; tcsh gets
  a small pause handler as part of this work; Perl and C/C++ are
  enabled if a verification task confirms their adapters honor `pause`,
  otherwise they get a clear CLI error.
- **Architecture:** unified in-process design (headless
  `DebugController` + TUI adoption), *not* the Python-only alternative
  of running the debuggee under a `debugpy` listen server and attaching
  a separate TUI process. The in-process design covers every language
  with one mechanism and keeps output routing in one process.

## Architecture

### Run-phase flow

```
tdb --run prog args
  └─ run_mode.py (new)
       ├─ DebugController(handler=SwappableEventHandler(ConsoleRunHandler))
       ├─ controller.start(stop_on_entry=False)     # no breakpoints installed
       ├─ install signal handlers (SIGINT, SIGUSR1 on POSIX)
       └─ wait:
            ├─ debuggee exited  → tdb exits with debuggee exit code
            └─ signal received  → controller.pause()
                                  wait for stopped event
                                  run TdbApp in adopted-session mode
```

### TUI-episode flow

```
TdbApp(adopted controller)
  ├─ swappable handler retargets → TextualEventHandler
  ├─ views populate from current stop (existing fetch-stop-info flow)
  ├─ user debugs normally (step, evaluate, breakpoints, …)
  └─ quit dialog:
       ├─ Detach & resume (default)
       │    ├─ handler retargets → ConsoleRunHandler
       │    ├─ controller continues the debuggee
       │    └─ back to run-phase wait loop (Ctrl-C works again)
       └─ Terminate program & quit
            └─ controller.stop(); tdb exits
```

The run phase and TUI episodes alternate any number of times within one
tdb process and one debug session. The session (adapter subprocess, DAP
connection, breakpoint state) survives across episodes; only the event
handler target and the terminal owner change.

## Components

### 1. CLI (`cli.py`)

- `--run` flag (store_true, long only). Help text names the purpose:
  run without the TUI, interrupt with Ctrl-C (or SIGUSR1 on POSIX) to
  open the debugger at the paused line.
- `_apply_flag_implications`: `--run` implies `stop_on_entry = False`.
- Validation (`parser.error`) — `--run` cannot be combined with:
  `-r/--remote-attach`, `-k/--breakpoint`, `-t/--to-line`, `--record`,
  `--replay`, `--server`, `--headless`, `--mcp`, `--terminal`.
- Language gate: after `_resolve_language`, if
  `args.run and not profile.capabilities.pause_while_running`, error
  with the language name and which languages are supported.
- Dispatch: `main()` routes `args.run` to `_run_run_mode(args)` before
  the `_run_tui` fallback.

### 2. Capability flag (`languages/base.py` + profiles)

New `LanguageCapabilities` field `pause_while_running: bool`
(default False). Set True for python and bash now; tcsh becomes True in
this work once its pause handler lands; perl and cpp flip to True only
after the verification task (below) passes manual/automated testing.

### 3. Run-mode runner (new `src/tdb/run_mode.py`)

Mirrors `server/runner.py::setup_headless_session` structure but with
no uvicorn/RPC server.

- **`ConsoleRunHandler`** — event handler for the headless phase:
  - `on_output(text, category)` → write to stdout (stderr for the
    `stderr` category) immediately, unbuffered.
  - records `terminated` / `exited` events (captures exit code) and
    sets an asyncio event the wait loop watches.
  - `on_stopped` → sets a "stopped" asyncio event consumed by the
    signal sequence.
- **`SwappableEventHandler`** — delegates every handler method to a
  `target` attribute; `retarget(new_handler)` swaps it. Lives in
  `tdb/session/event_bus.py` next to `CompositeEventHandler`.
- **Main loop:**
  1. Build controller with
     `SwappableEventHandler(ConsoleRunHandler())`; apply
     `step_mode` from config as other entry points do.
  2. `controller.start(...)` with `stop_on_entry=False`; wait for the
     `initialized` event; `do_configure()` — with **no** breakpoints
     installed into state.
  3. Install signal handlers: `loop.add_signal_handler` for SIGINT and
     SIGUSR1 on POSIX; `signal.signal` for SIGINT on Windows.
  4. Wait on (exited | signal):
     - **exited:** print nothing extra (output already streamed);
       `sys.exit(debuggee_exit_code)` (0 if the adapter reported none).
     - **signal:** disarm handlers (repeat signals during TUI startup
       are ignored), `await controller.pause()`, wait for the stopped
       event with the standard timeout, then run a TUI episode.
  5. After a TUI episode ends with "detach & resume": retarget the
     handler to the console handler, re-arm signal handlers, loop back
     to 4. If the debuggee exited while the TUI was open, exit after
     the TUI closes.
- **Pause timing caveat** (documented in README + `--run` help): DAP
  pause is cooperative. It lands at the next statement boundary
  (shells) or next traceable event (Python). A hang inside one
  blocking external call/syscall surfaces only when that call returns.
  If the stopped event does not arrive within the timeout, print a
  message explaining this ("program is blocked inside a single call;
  will stop when it returns"), keep the pause pending, and return to
  the wait loop — when the stop eventually lands, open the TUI then.

### 4. TUI adoption (`app.py`)

`TdbApp.__init__` gains `adopted_controller: DebugController | None`
and `adopted_handler: SwappableEventHandler | None` (both or neither).
In adopted mode:

- Skip controller construction; use the adopted one. Retarget the
  swappable handler to the newly built `TextualEventHandler` before
  mount so no events are lost.
- `_start_session` skips launch/attach entirely; instead it fetches
  stop info and populates code view, stack, variables from the current
  stop — the same path a stopped event normally drives — so the cursor
  lands on the paused line.
- Saved breakpoints load into state and install via `setBreakpoints`
  as usual: interactive debugging after the interrupt is full-featured.
- Restart is disabled in adopted mode (same policy and guard as
  remote-attach; `controller.supports_restart` pattern at
  `app.py:576`).
- The app exposes which way it exited: a result field
  (`detach_and_resume: bool`) read by the run-mode loop.

### 5. Quit paths (adopted mode)

Per the project rule, **all** exit paths are audited and routed to one
decision: `q`, `Ctrl+Q`, quit menu item, and any other path that
reaches the quit confirmation. The dialog in adopted mode offers:

1. **Detach & resume** (default): controller issues `continue`, app
   exits with `detach_and_resume=True`. No `controller.stop()`.
2. **Terminate program & quit:** existing quit behavior
   (`controller.stop()`), app exits with `detach_and_resume=False`;
   run-mode loop then exits tdb.

The `_is_quitting` guard (`app.py:262`) continues to prevent stacked
modals/duplicate shutdowns.

### 6. Signals and process isolation

- POSIX: adapters/debuggees already run under `start_new_session=True`
  (`dap/client.py:88`, bash `session.py:264`), so terminal Ctrl-C
  reaches only tdb. Audit the tcsh and perl/cpp adapter spawn paths for
  the same property as part of implementation.
- Windows: spawn adapters with
  `creationflags=CREATE_NEW_PROCESS_GROUP` in run mode so
  `CTRL_C_EVENT` is not delivered to the adapter/debuggee console
  group; SIGINT handled via `signal.signal`. SIGUSR1 does not exist on
  Windows and is not offered there.
- While the TUI owns the terminal, run-mode signal handlers are
  removed; textual's own key handling applies. Handlers are
  reinstalled on detach.

### 7. tcsh pause handler (`adapters/tcsh/server.py`)

New `pause` request handler: sets a `pause_requested` flag on the
session. At the next probe rendezvous (the instrumented script blocks
on the control FIFO after reporting each probe), the adapter emits
`stopped(reason="pause")` instead of writing the continue fragment.
Flag clears on stop. No instrumenter/runtime changes — the existing
per-statement rendezvous is the mechanism.

### 8. Perl / C++ pause verification (implementation task)

Small scripted checks: launch a looping debuggee under each adapter,
issue DAP `pause` mid-run, assert a `stopped` event with a usable stack
arrives. If an adapter fails, its profile keeps
`pause_while_running=False` and `--run` reports it unsupported — no
partial enablement.

## Error handling

- `--run` + unsupported language → CLI error naming supported
  languages.
- Adapter missing (`AdapterNotFoundError`) → same stderr hint + exit 2
  pattern as headless mode.
- Pause timeout → informational message, pause left pending, loop
  continues (see §3).
- Debuggee exits between signal and TUI open → print exit notice and
  exit tdb with the debuggee's code instead of opening the TUI.
- Signal received while a TUI episode is starting/active → ignored.

## Testing

- **CLI units:** `--run` implications, each rejected flag combination,
  language gate for a non-pause-capable profile.
- **`SwappableEventHandler` unit:** retarget mid-stream, no dropped
  method calls.
- **Run-mode integration (POSIX):** launch a looping script under
  `tdb --run`, send SIGUSR1 to the tdb process, assert the controller
  reaches a paused state with a current frame; then drive
  detach-and-resume and assert the debuggee continues; repeat the
  interrupt to prove multiple episodes work.
- **Exit-code passthrough:** `--run` on a script exiting 7 → tdb exits
  7.
- **Output streaming:** debuggee stdout/stderr appear on the terminal
  during the run phase.
- **tcsh pause:** adapter-level test — launch looping tcsh script,
  send `pause`, assert stopped at the next probe with correct line.
- **Windows:** existing CI matrix runs CLI/unit layers; signal
  integration tests are POSIX-marked, with a manual Windows check for
  Ctrl-C isolation noted in the PR.

## Non-goals

- `--run` combined with `--terminal` (interactive/TUI debuggees):
  rejected for v1; revisit if needed.
- Restart from an adopted TUI session.
- Interrupting a debuggee blocked inside a single foreign call
  (documented limitation, cooperative pause only).
- A JSON-RPC server alongside run mode (`--server`); use `--headless`
  for programmatic control.
- Forwarding Ctrl-C to the debuggee (the isolation is the point; a
  future "send signal to debuggee" TUI action could cover it).
