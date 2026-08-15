# `--terminal` for All Languages — Design

**Date:** 2026-08-14
**Status:** Approved
**Branch:** `support_terminal_on_all_languages`

## Problem

`tdb --terminal X program` runs the debuggee in a fresh terminal
emulator window so that TUI/curses programs and programs that read
stdin can be debugged without fighting tdb's own TUI for the tty.
Today only Python supports it: debugpy sends the DAP `runInTerminal`
reverse request and tdb's `TerminalLauncher` answers it. For every
other language the `console` launch parameter is ignored — and worse,
the controller suppresses debuggee stdout/stderr from the Console View
in terminal mode, so `--terminal` with Perl/Bash/Tcsh silently
swallows output today.

## Goal

`--terminal` gives the debuggee a real interactive terminal — stdin,
curses, and `^C` all work, because the debuggee is a child of the
emulator with the window's tty as its controlling terminal — for
Perl, Bash, Tcsh, and C/C++ via lldb-dap. Python is unchanged.

## Decisions made during brainstorming

- **Approach:** the standard DAP `runInTerminal` reverse-request flow
  in every adapter (approved over a tdb-side pty-proxy approach, which
  cannot deliver a real controlling terminal and bypasses the
  protocol).
- **gdb is deferred.** `gdb -i dap` (verified against GDB 17) has no
  `runInTerminal` support and no tty launch parameter. A follow-up
  effort can use its `evaluate` `context:"repl"` escape hatch to run
  `set inferior-tty` against a tty held open in the emulator window.
  In this effort, `--terminal` with the gdb adapter fails fast with an
  error naming `--adapter lldb-dap`.
- **Attach modes reject `--terminal`.** The process already exists;
  the flag cannot apply. Argparse error rather than silent ignore.
- **Native Windows:** out of scope, unchanged. The emulator table is
  Unix-only and `--terminal` PATH validation already fails naturally.

## Architecture

### The uniform flow

Existing client-side plumbing is already language-agnostic and needs
no changes:

- `DebugController.start` advertises `supportsRunInTerminalRequest`
  in `initialize` and passes `console="externalTerminal"` into every
  profile's `launch_body()` when `--terminal` is set.
- `tdb.session.terminal.TerminalLauncher` is registered as the
  generic reverse-request handler on the shared `DAPClient` (whose
  reverse-request dispatch is adapter-independent) and wraps the
  requested command in the chosen emulator.
- Terminal mode already suppresses debuggee stdout/stderr in the
  Console View and shows the "running in external terminal" hint via
  `TerminalLauncher.on_started`.

Each adapter (perl, bash, tcsh servers; lldb-dap via its native
support) does what debugpy does:

1. Record `supportsRunInTerminalRequest` from the `initialize`
   request arguments.
2. Read `console` from the `launch` request.
3. When `console == "externalTerminal"` and the client supports it:
   prepare the control channel first, then **send `runInTerminal`**
   (command, cwd, env) instead of spawning the debuggee, and wait for
   the debuggee to connect back over the control channel. Everything
   after that is identical to pipe mode.
4. `externalTerminal` without client support is a launch error.

### The load-bearing constraint

An emulator-spawned debuggee inherits nothing from the adapter:
daemon-based emulators (gnome-terminal) spawn commands from a
pre-existing server process, so inherited file descriptors are lost
and the adapter can never `wait()` on the debuggee. Therefore:

- **Control channels must be path- or network-based.** Perl's TCP
  connect-back socket and tcsh's probe FIFOs already qualify; bash's
  inherited-fd pair and the tcsh guardian's `--status-fd` /
  `--control-fd` do not.
- **Exit status must be reported, not reaped** (see below).
- **Adapter message loops must route DAP `Response` messages** to the
  pending reverse request; today `run()` discards anything that is
  not a `Request`.

## Per-adapter changes

### Perl (smallest)

The perl5db control channel is already a connect-back TCP socket
(shared with attach mode). Terminal mode: listen as today, then send
`runInTerminal` with the exact `perl -d` command and env
(`PERLDB_OPTS=RemotePort=...`) that launch mode builds now.

### Bash

The harness's command/response channels move from inherited fds
(`__TDB_CMD_FD`/`__TDB_RESP_FD` + `pass_fds`) to **FIFOs in the
adapter's existing tmpdir**, announced via `__TDB_CMD_PATH` /
`__TDB_RESP_PATH`. Both pipe mode and terminal mode use the FIFO
form so there is one channel implementation, not two. The
ready-handshake timeout rises from 15 s to 30 s in terminal mode to
absorb emulator cold start.

### Tcsh

The probe FIFOs already work by path. The guardian remains the
in-terminal parent (keeping session-generation cleanup and the
termination escalation ladder) and gains:

- `--status-path` / `--control-path` FIFO variants of its fd options
  (FIFOs live in the adapter workspace), and
- a final status line `exit <code>` or `signal <n>`, because in
  terminal mode nobody can reap the guardian to read its mirrored
  exit status.

The `runInTerminal` command is the same
`python guardian.py -- tcsh -f <instrumented>` invocation used today.

### C/C++

- `LldbDapAdapter.launch_body`: add `"runInTerminal": true` when
  `console == "externalTerminal"` (lldb-dap then drives the standard
  reverse-request flow itself).
- `GdbDapAdapter.launch_body`: raise the clear not-supported error
  for `externalTerminal`, suggesting `--adapter lldb-dap`.

### Python

No changes.

## Exit codes and termination in terminal mode

- **Exit status.**
  - Tcsh: the guardian observes `waitpid` and writes `exit <code>` /
    `signal <n>` on the status FIFO — correct even for signal deaths,
    no debuggee cooperation needed.
  - Perl/Bash: the `runInTerminal` command is wrapped in a reporter —
    `/bin/sh -c '<cmd>; echo $? > <tmpdir>/exit-status'` — so `$?`
    (including `128+n` for signal deaths) lands in a file the adapter
    reads when the control channel closes.
  - If the status never arrives (the window was destroyed so hard even
    `sh` died), the adapter emits `exited` with code `-1` after a
    short grace period instead of hanging.
- **terminate / disconnect.** First resort is the existing polite
  in-band route (perl5db `q`; bash harness command; tcsh guardian
  `terminate` over the control FIFO with its full escalation ladder).
  For the force path, the debuggee side reports its **pid** over the
  control channel at startup (bash harness `$$` in the ready message;
  perl helpers likewise; the guardian already owns this), letting the
  adapter `killpg` directly.
- **User closes the window.** SIGHUP → debuggee dies → control-channel
  EOF → the adapters' existing EOF paths emit `terminated`/`exited`
  with the reported (or `-1`) code. No new machinery; covered by
  tests.

## CLI and controller changes

- `--terminal` combined with any attach mode (`-r`,
  `--remote-attach`) is an argparse error: "`--terminal` only applies
  when tdb launches the program".
- Controller behavior is otherwise unchanged (60 s launch timeout in
  terminal mode, Console View suppression, external-terminal hint —
  all already language-agnostic).

## Error handling

- `runInTerminal` fails on the client side (emulator vanished after
  preflight, spawn error): the launch request fails with the client's
  message, surfaced by tdb's existing launch-error modal.
- The debuggee never connects back (emulator opened, command died):
  each adapter's existing ready-timeout error fires, with the message
  extended to mention the external terminal.
- gdb + `--terminal`: fails at launch-body construction, before any
  process is spawned.

## Testing

Core trick: **the test acts as the DAP client**, receives the
`runInTerminal` reverse request, and spawns the command itself as a
plain subprocess — optionally under a Python `pty` for a real tty.
This exercises the entire adapter-side flow (capability recording,
reverse-request round trip, path-based channels, exit reporting)
headlessly on both Ubuntu and Alpine CI.

- **Unit:** per-language `launch_body` (lldb flag; gdb error);
  adapters send `runInTerminal` iff capability ∧ `externalTerminal`;
  `Response` routing in adapter message loops; bash FIFO channels;
  guardian path-mode options and `exit`/`signal` status lines;
  exit-reporter parsing; CLI rejection of `--terminal` + attach.
- **Integration (per language):** launch → stop → step → continue →
  exit with the correct code via the client-spawn trick; nonzero and
  signal exit codes; window-close simulation (kill the spawned
  subprocess, assert `terminated` + exit code); interactive stdin
  (write into the pty, the debuggee reads it).
- **TerminalLauncher:** existing tests plus one using a fake emulator
  script that records its argv and execs the payload.
- **Manual:** smoke test with a real emulator (xterm) under a local X
  session before declaring the feature done.

## Non-goals

- gdb terminal support (follow-up effort via `set inferior-tty`).
- Attach-mode terminal support.
- Native Windows terminal support.
- New emulator table entries (the existing `_TERMINAL_SPECS` set is
  unchanged).
