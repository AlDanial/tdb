# Run detached from the quit modal — design

Date: 2026-11-13
Status: awaiting approval

## Intent

A user starts `tdb myprogram`, inspects the program to confirm a start-up
condition or state, and is then done debugging. Today the only way out is
`q` `q`, which kills the program. They want a second choice in the quit
modal — `d: run detached` — after which the debugger gets out of the way
and the program runs (or keeps running) in the terminal where `tdb` was
invoked, as close as possible to having been started there directly.

## Goals

- The quit modal of an ordinary interactive session offers `d: run detached`.
- After `d`: the TUI closes, the program resumes, its stdout/stderr appear
  on tdb's stdout/stderr, terminal input reaches its stdin, and tdb exits
  with the program's exit code.
- No debugger stops after detach: all breakpoints and exception stops are
  removed, and any stop that still happens is resumed automatically.
- Ctrl-C goes to the program as an interrupt. It never reopens the debugger.
- Works for every language tdb supports.

## Non-goals

- Re-entering the debugger after detach. (`tdb --run` is the mode for that.)
- Removing the debugger's runtime overhead after detach (see Approaches, B).
- Giving the program a real terminal. It was started on pipes and stays on
  pipes; `isatty()` is false, exactly as in the TUI.
- New stdin support for gdb / lldb-dap / dlv sessions (C, C++, Rust, Go,
  native OCaml). Those programs have no stdin route in the TUI today and
  have none after detach. **My call**; it is a separate project (it needs a
  pty or adapter-specific redirection at launch time).
- Exposing detach through the RPC server or MCP tools.

## Anti-goals

- The program left frozen at a stop with no UI to resume it.
- The program orphaned (still running after tdb is gone) in a launched
  session, or killed by `d`.
- `d` changing what `q`, Ctrl+Q, or `tdb --run`'s existing modal do.

## Constraints (how things work today)

- In a launched TUI session the program's stdio are pipes owned by tdb
  (`PipeLauncher`, Python) or by the adapter (everything else); output
  reaches tdb as `on_output` events. tdb therefore cannot exit and leave
  the program on the terminal.
- Every adapter and debuggee is spawned in its own session/process group,
  so the terminal's Ctrl-C reaches only tdb.
- The DAP client's tasks live on the event loop Textual's `app.run()`
  creates; that loop is gone when `app.run()` returns.
- `tdb --run` already has the pieces of a headless phase:
  `ConsoleRunHandler` (prints output, tracks exit code),
  `SwappableEventHandler`, `final_exit_code`, `stop_session_on_error`.
- stdin routes (`AdapterQuirks.stdin_route`): `run_in_terminal` (Python,
  tdb owns the pipe), `request` (perl, bash, tcsh, ruby, powershell via
  the private `tdbStdin` request), `None` (gdb, lldb-dap, dlv, earlybird).
- Remote/pid attach sessions already disconnect with
  `terminateDebuggee=false` on quit.

## Approaches

**A. Stay attached, go silent — chosen.** tdb keeps the debug session,
strips every stop condition, resumes, and relays stdio until the program
exits. One code path for all languages, built from run mode's parts.
Costs: debugger overhead remains; program stays on pipes.

**B. True disconnect, relay pipes only.** Sheds overhead, but only where
the debugger is a separate process, and gdb/lldb tie the program's output
to the adapter. Two paths and per-adapter fragility for a speed gain.

**C. Re-exec the program on the terminal.** Real tty, zero overhead, but
restarts the program and discards the state the user just verified.

Al chose A, with Ctrl-C delivered to the program. One piece of B is kept
where it is free: sessions whose program does not depend on tdb's pipes
(`--terminal`, attach) get a real disconnect — see "Session kinds".

## Design

### The modal

`_QuitConfirmModal` grows from one line to three:

    q: quit (ends the program)
    d: run detached
    ESC: cancel

- `q` and ESC behave exactly as now. The modal dismisses with
  `"quit"`, `"detach"`, or `None`.
- The `d` line and binding are present only when detach is available:
  the session is live (not terminated, not post-mortem, not a
  `--replay-tui` playback, startup not failed). Otherwise the modal is the
  two-line quit/cancel form.
- Attach sessions word the first line `q: quit` (the program is not ended).
- Ctrl+Q stays an immediate quit with no modal. `tdb --run`'s
  `_DetachQuitModal` is untouched.
- If the File menu has a Quit entry, add "Run detached" beside it, enabled
  under the same condition, routed through the same action.

### Session kinds

| Session | What `d` does |
|---|---|
| Launched, stdio on tdb's pipes (the default) | Hand over to the **relay** (below). tdb exits when the program does, with its exit code. |
| Launched with `--terminal` | Strip stops, resume, then try a disconnect that leaves the program alive; tdb exits 0. If that adapter's debuggee cannot outlive the adapter, stay attached silently (relay with nothing to relay) until it exits. Which adapters need the fallback is for the builder to establish per adapter. |
| Remote attach / pid attach | Strip stops, resume, then the existing non-terminating disconnect; tdb exits 0. |
| Adopted (`tdb --run`) | Unchanged — existing modal. |

### Handover (launched, piped sessions)

1. `d` pressed. Run the unsaved-edits prompt (`_confirm_discard_edits`),
   set `_is_quitting`, record a `detach` event in the session recording.
2. Save breakpoints to breakpoints.json exactly as a normal quit does.
3. Strip stops in the adapter, for the parent and every child-process
   client: send empty breakpoint lists for every source (state is not
   needed afterwards), empty function breakpoints, empty exception
   filters, cancel any run-to-cursor temporary breakpoint, then send the
   profile's detach-prep commands (see "Ctrl-C").
4. The app exits with a `detach` result **without** calling
   `controller.stop()`. If a `--server` RPC server is running, it is shut
   down as on quit.
5. The TUI and the relay must share one event loop. `_run_tui` changes
   from `app.run()` to `asyncio.run(...)` of a coroutine that awaits
   `app.run_async()` and, on a `detach` result, awaits the relay. Wrap the
   post-TUI part in `stop_session_on_error`.
6. The controller's event handler is repointed from the TUI's handler to a
   `ConsoleRunHandler`. Non-adopted sessions don't use
   `SwappableEventHandler` today; the builder either makes the TUI always
   construct one or adds a retarget on the controller — builder's choice.
7. Replay the program's earlier output: write the stdout/stderr text the
   Console View had captured to tdb's stdout/stderr, in order, so the
   terminal (or a redirect) holds the complete transcript. **My call.**
   Debugger/evaluate chatter is not replayed.
8. Print one line to stderr:
   `tdb: detached — <program> running; Ctrl-C interrupts it`.
9. Resume: if the program is stopped (including the entry stop), continue
   all clients. If it is already running, do nothing.

### The relay

A new function in `run_mode.py` (or a small sibling module) reusing
`ConsoleRunHandler` and `final_exit_code`. It loops until `exited`:

- **Output**: `on_output` already writes to stdout/stderr.
- **Unexpected stop** (`stopped` event of any reason): immediately
  continue. This is the backstop for the anti-goal — signal stops,
  uncaught-exception stops, a `tdb.breakpoint()` hook call, a breakpoint
  that failed to clear. An exception stop that is continued lets the
  program die the way it would have natively.
- **stdin**, when `controller.supports_stdin`: a daemon thread reads tdb's
  stdin (cooked mode, so line at a time) and hands each chunk to
  `controller.write_stdin` on the loop; EOF calls `controller.close_stdin`.
  A thread rather than `add_reader` so the same code works on Windows.
  Before starting, discard keystrokes typed at the TUI that are still
  queued (`termios.tcflush` on POSIX). If stdin is not supported for the
  session, nothing is read.
- **Ctrl-C**: see below.
- **SIGTERM / SIGHUP / terminal gone**: `controller.stop()` (the program
  ends, as on quit) and exit `128 + signal`.
- **End**: on `exited`/`terminated`, `controller.stop()` for cleanup and
  return `final_exit_code(console)`; `_run_tui` exits with it.

### Ctrl-C

Because the debuggee is in its own session, tdb must deliver the
interrupt. New controller method `interrupt_debuggee()`, dispatching on
how the session was launched:

- **Python** (`PipeLauncher`): signal the process tdb spawned
  (`SIGINT`; `CTRL_BREAK_EVENT` on Windows).
- **tdb-owned adapters** (perl, bash, tcsh, ruby, powershell): a new
  tdb-private DAP request `tdbSignal` `{ "signal": "SIGINT" }`, handled by
  each adapter by signalling its debuggee (process or group, whichever
  that adapter already uses for teardown).
- **gdb / lldb-dap / dlv / earlybird**: signal the debuggee pid, obtained
  from the DAP `process` event's `systemProcessId` or an adapter-specific
  query. These debuggers intercept signals, so each profile supplies
  *detach-prep commands* — a new `AdapterSpec` hook, sent in handover
  step 3 — that make SIGINT pass straight to the program without stopping
  (gdb: `handle SIGINT nostop noprint pass`; lldb: `process handle`
  equivalent). The relay's auto-continue covers a stop that slips through.

The observable contract, per language: after detach, Ctrl-C does to the
program what it would do had it been run directly — its own handler runs
if it has one, otherwise it dies and tdb exits with the conventional
status (130 on POSIX). Language debuggers that install their own INT
handling (perl -d, bash traps) must be made to honour this; how is the
builder's to work out per adapter.

**Fallback**, for any language/platform where delivery cannot be made to
work: Ctrl-C ends the program via `controller.stop()` and tdb exits 130.
That matches the default outcome of an unhandled interrupt. The builder
must list in the docs which languages use the fallback.

### Recording and replay

`--record` writes a `detach` event at handover. `--replay` and
`--replay-tui` treat `detach` as the end of the session (same as `quit`);
they do not run a relay.

### Exit paths to audit (per CLAUDE.md)

`q`→`q`, `q`→`d`, `q`→ESC, Ctrl+Q, File-menu quit/detach, `d` pressed
twice quickly, `d` while a quit is in flight, `d` during the unsaved-edits
prompt, program exits between modal open and `d`, adapter dies during
handover, terminal hang-up before and during the relay, SIGTERM during the
relay. None may leave an orphaned debuggee or re-spawn tdb.

## Testing

- Relay unit tests with a fake controller: auto-continue on stop, stdin
  forwarding and EOF, exit-code propagation (including `terminated`
  without `exited` → 1), Ctrl-C dispatch and fallback, SIGTERM stop.
- Modal tests (Textual pilot): three-line vs two-line form by session
  state; `q`, `d`, ESC results; no double-push.
- Handover tests: breakpoints saved, stops stripped on parent and child
  clients, earlier output replayed in order, recorder event.
- Per-language integration tests on a pty: stop at entry, `d`; assert
  later output arrives on stdout, a line typed is read by the program
  (languages with a stdin route), exit code is propagated, Ctrl-C
  terminates a looping program. Skipped where the toolchain is absent, as
  existing per-language tests are.
- `--terminal` and attach variants: program still alive after tdb exits.
- Windows: stdin thread and Ctrl-C path verified or explicitly documented
  as fallback.
- Full `uv run pytest` before calling it done.

## Docs

Update the quit/keys documentation pages under `docs/` (they feed
`--doc`), stating: what `d` does, that the program stays on pipes, the
native-language stdin limitation, and the Ctrl-C fallback list.

## Left to the builder

- Event-handler retargeting mechanism (step 6).
- Where the relay lives and how much of `run_mode.run`'s loop is shared.
- Per-adapter `tdbSignal` delivery and INT-handler neutralising.
- Per-adapter pid discovery and detach-prep commands for gdb/lldb/dlv/
  earlybird, or a documented fallback.
- Which `--terminal` adapters can truly disconnect.
- Exact modal wording, width, and styling.
