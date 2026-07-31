# Session Record / Replay (`--record` / `--replay`) — Design

Date: 2026-07-31
Status: approved (brainstormed with Al; approach A — TUI-intent recording, in-process replay)

## Overview

`tdb --record FILE` captures the user's debugging actions — breakpoints,
stepping, evaluate-console entries, stack navigation, variable
examination — as a file of JSON-RPC commands identical in shape to what
`tdb --server` accepts over `POST /rpc`. The file can be replayed two
ways: piped by any external client to a running `tdb --server`, or in
one command via `tdb --replay FILE`, which runs the same RPC dispatch
in-process with no HTTP involved.

Use cases, in priority order: regression testing (replay a session
after changes and diff the replay output), automation (record a routine
once by hand, replay it to drive a program to a deep state), and demos
(replay with original pacing via `--timing`).

## Goals

- A recording is a self-contained, human-readable script of user
  intent: replaying it reproduces the session without the original
  CLI flags or hand-set breakpoints.
- Records are exactly the `{"action": ..., "params": [...]}` shape the
  RPC endpoint accepts, so external replay needs no translation layer.
- Works for every language profile (Python, C++, Perl) and for both
  launch mode and `-r`/`--remote-attach`.
- Internal debugger machinery never pollutes a recording: a record
  always corresponds to a user gesture.

## Non-goals

- Capturing command *responses* in the recording (regression comparison
  is done by diffing replay output between runs).
- Recording pure viewing: scrolling, search, modals, thread / task /
  process listings.
- Recording the breakpoints-enable/disable toggles (no RPC equivalent
  today).
- Post-mortem mode, `--server` mode, and `--replay` mode themselves
  cannot record.
- Replay "fidelity" beyond command order and optional pacing (no
  attempt to fake wall-clock time, pids, or environment inside the
  debuggee).

## CLI surface

- `tdb --record FILE <program> [args...]` — valid with launch mode
  (any language) and with `-r/--remote-attach`. Combining `--record`
  with `--server`, `--replay`, or post-mortem mode is a startup error.
  `FILE` is created or truncated at startup; an unwritable path is a
  startup error reported before the TUI opens.
- `tdb --replay FILE [--timing] [--replay-timeout S]` — takes no
  program argument; the header supplies launch/attach configuration.
  `--timing` reproduces the recorded inter-command pacing; the default
  replays at full debugger speed. `--replay-timeout` (default 30 s) is
  the stop-wait timeout passed to blocking commands (`next`,
  `step_in`, `step_out`, `continue`).

## File format

JSON Lines. Line 1 is the header; every subsequent line is one command
record, flushed as it is written so a crashed or killed session keeps
everything up to its last action.

Header, launch mode:

```json
{"tdb_recording": 1, "created": "2026-07-31T14:02:11", "mode": "launch",
 "language": "python", "program": "/abs/prog.py", "args": ["src"],
 "cwd": "/abs/dir", "python": null, "adapter": null,
 "step_mode": "statement", "no_just_my_code": false}
```

Header, remote attach: `"mode": "remote-attach"` with `"host"` and
`"port"` in place of `program`/`args`/`cwd`/`python`.

Command records:

```json
{"t": 3.412, "action": "set_breakpoint", "params": ["/abs/prog.py:14"]}
{"t": 5.020, "action": "continue", "params": []}
{"t": 9.771, "action": "evaluate", "params": ["len(data)"]}
{"t": 11.030, "action": "quit", "params": []}
```

- `t` is seconds since session start (float, millisecond precision),
  monotonically non-decreasing.
- `tdb_recording` is the format version; replay rejects versions it
  does not know.
- Stripped of `t`, each command line is a valid `POST /rpc` body.
  External replay is a two-line shell loop:
  `tail -n +2 FILE | while read l; do curl -s -d "$l" host:port/rpc; done`
  (a small Python equivalent will be shown in the README for Windows).

## Recorder

New unit `src/tdb/session/recorder.py`:

- `SessionRecorder(path, header_fields)` — writes the header, stamps
  `t` on each `record(action, params)`, writes one JSON line, flushes.
- `NullRecorder` — no-op twin, so call sites are unconditional
  (`self.recorder.record(...)` always works; no `if recording` at any
  call site).
- The `TdbApp` owns the recorder instance. CLI plumbing creates a
  `SessionRecorder` when `--record` was given, else `NullRecorder`.

### Hook points (gesture → record)

Hooks live in the TUI's already-converged action methods — the single
methods that keyboard, menu, and mouse paths all funnel into. A record
therefore always means "the user did this"; internal machinery that
reuses the same controller methods (statement-stepper re-steps, the
breakpoint-hook auto-step-out, run-to-cursor's cleanup) is invisible
at this layer and can never appear in a recording.

| User gesture | Recorded as |
|---|---|
| step over / step in / step out | `next` / `step_in` / `step_out` |
| continue, pause | `continue`, `pause` |
| toggle breakpoint on | `set_breakpoint ["file:line"]` |
| toggle breakpoint off / delete from Breakpoint view | `remove_breakpoint ["file:line"]` |
| condition / hit-count modal applied | `set_breakpoint ["file:line", condition, hit_condition]` |
| run-to-cursor (`t`) | `set_breakpoint` + `continue` + `remove_breakpoint` (mirrors what the TUI actually does) |
| Evaluate-console entry | `evaluate ["expression"]` |
| stack frame selection (click or keyboard) to frame *i* | *n* × `stack_up` / `stack_down` (delta from the previously selected frame; the baseline resets to the top frame at every stop, matching both the TUI and the RPC server) |
| expand a variable node | `inspect [evaluate_name]` — only when the adapter supplied DAP `evaluateName` |
| restart (R) | `restart` |
| quit (q, Ctrl-C) | `quit` |

`-k` / `-t` CLI breakpoints are recorded as ordinary `set_breakpoint`
records emitted when they are applied at configure time, so a
recording is self-contained even when the session started with CLI
breakpoints.

### Known recording limitations (documented in README)

- Variable expansion is not recorded when the adapter provides no
  `evaluateName` (today: the Perl adapter; adding `evaluateName` there
  is a separate enhancement).
- The global breakpoints-disable toggle and per-breakpoint disable are
  not recorded.
- Editing an *existing* breakpoint's condition re-records it as
  `set_breakpoint` with condition params; replay must yield one
  breakpoint, not two (the RPC handler's existing update-in-place
  semantics; verified by test).

## Replay

New unit `src/tdb/replay.py`:

1. Parse and validate the whole file up front: header version, known
   `action` names (checked against `RpcHandlers`' dispatch table),
   well-formed JSON per line. Errors name the offending line number
   and abort before any process is launched.
2. Build the launch or attach configuration from the header
   (`no_just_my_code` is part of that configuration); apply
   `step_mode` to the controller so statement-granularity steps land
   on the same lines as during recording.
3. Start the existing headless runner (`server/runner.py`) in-process
   — no uvicorn, no port — and feed each record to the same
   `RpcHandlers` dispatch table `tdb --server` uses.
4. Per command, print `t`, the action, its params, and the RPC result
   (ok/error) to stdout. With `--timing`, sleep the recorded
   inter-command delta before dispatching.
5. A recording without a trailing `quit` gets an implicit teardown at
   EOF. Exit code 0 if every command succeeded, 1 otherwise. Execution
   continues past failed commands — a full divergence report is more
   useful than a truncated one, and a failing `evaluate` can be a
   legitimate part of the recorded session.

## Error handling

- **Recording:** a mid-session write failure (disk full, permission
  change) notifies the user once in the TUI, closes the file, and
  swaps in `NullRecorder`. The debug session never dies because the
  recorder did.
- **Replay:** malformed files fail validation before launch (see
  above). A remote-attach replay that cannot connect reports the same
  error `-r` does today. Blocking commands that never reach a stop
  fail with the `--replay-timeout` error from the RPC layer and are
  reported like any failed command.

## Testing

- **Unit — recorder:** header fields for both modes; one JSON line per
  record with flush; `t` monotonicity; `NullRecorder` inertness;
  write-failure degradation to `NullRecorder`.
- **Unit — hook mapping:** drive the TUI action methods directly with
  a capturing recorder; assert the exact `(action, params)` per
  gesture, including run-to-cursor's three records, frame-selection
  deltas, and the no-`evaluateName` skip.
- **Unit — replay validation:** unknown version, unknown action,
  malformed line, wrong-mode header each produce a line-numbered error
  and no launch.
- **Integration:** record a scripted session against a toy program by
  driving the TUI action methods, then `--replay` the file headless
  and assert the stop-line sequence and evaluate results; a `--timing`
  smoke test (elapsed ≥ recorded span); one Perl-profile replay to
  prove language-agnosticism.
- **Round-trip property:** replaying a recording of N steps stops at
  the same file:line sequence as the original session.

## New / touched files

- New: `src/tdb/session/recorder.py`, `src/tdb/replay.py`.
- Touched: `src/tdb/cli.py` (flags + validation), `src/tdb/app.py` and
  `src/tdb/app_handlers/*` (recorder ownership + hook calls at the
  converged action methods), `README.md` (usage, format, limitations,
  external-replay examples).
- Untouched by design: `server/handlers.py` dispatch surface (replay
  consumes it as-is; any gesture found unrepresentable there during
  implementation is a plan-level decision, not a silent format
  extension).
