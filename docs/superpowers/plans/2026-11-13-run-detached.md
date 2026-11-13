# Run Detached Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `d: run detached` to the interactive quit modal: the TUI closes, the program resumes with no debugger stops, and tdb relays its stdio until it exits.

**Architecture:** The TUI and a new headless *relay* share one asyncio loop (`_run_tui` moves from `app.run()` to `asyncio.run` + `app.run_async()`). On `d` the app strips all stop conditions, exits with `run_detached=True`, and the CLI retargets the controller's events to a `ConsoleRunHandler` and awaits the relay. Ctrl-C is delivered to the debuggee through a new `controller.interrupt_debuggee()` with a per-launch-kind mechanism and a terminate fallback.

**Tech Stack:** Python, textual, asyncio, DAP (debugpy, gdb, lldb-dap, dlv, tdb-owned adapters), pytest via `uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-11-13-run-detached-design.md`

## Global Constraints

- Work on a branch `run-detached` off `main`; never commit to `main`.
- `q`, ESC, Ctrl+Q and `tdb --run`'s `_DetachQuitModal` keep their current behavior.
- After detach no stop may leave the program frozen: every `stopped` event is answered with a continue.
- A launched session must never leave an orphaned debuggee and `d` must never kill the program.
- Ctrl-C after detach goes to the program; it never reopens the debugger. Where delivery is impossible: `controller.stop()` and exit 130.
- No new stdin for gdb / lldb-dap / dlv / earlybird sessions.
- Not exposed through the RPC server or MCP.
- Cross-platform: nothing POSIX-only without an `os.name` guard; stdin forwarding uses a thread, not `add_reader`.
- Detach notice, exact copy, on stderr: `tdb: detached — {program} running; Ctrl-C interrupts it`
- Modal copy, exact: `q: quit (ends the program)` (attach sessions: `q: quit`), `d: run detached`, `ESC: cancel`.
- Every task ends with `uv run pytest <its tests>` green; the last task runs the full suite (use the project's memory-capped pytest wrapper if one is present).
- CLAUDE.md: when touching a quit path, audit all of them (list in the spec, "Exit paths to audit").

## Review Focus

1. Program exits between the modal opening and `d` being pressed → tdb exits with the program's exit code, no hang, no traceback (Task 4).
2. Program already *running* (not stopped) when `d` is pressed → no continue is sent, relay simply starts (Task 3).
3. Program writes a huge amount of output before detach → replay is bounded (last 4 MiB) and says it was truncated (Task 3).
4. tdb's stdin is not a terminal (`tdb prog < file`, or closed) → file contents reach the program then EOF; no `termios` error (Task 1).
5. A stop storm: the adapter reports `stopped` repeatedly (e.g. a breakpoint that could not be cleared in a hot loop) → each is continued, output keeps flowing, CPU does not spin when no stop is pending (Task 1).

## File Structure

| File | Responsibility |
|---|---|
| `src/tdb/detach_relay.py` (new) | The relay loop, stdin forwarder, signal handling, the disconnect variant. |
| `src/tdb/session/controller.py` | `strip_stops`, `interrupt_debuggee`, `detach_disconnect`, debuggee-pid tracking. |
| `src/tdb/session/terminal.py` | `PipeLauncher.send_interrupt`. |
| `src/tdb/languages/base.py` + `cpp.py`, `rust.py`, `go.py`, `ocaml.py` | `AdapterSpec.detach_prep_commands()`, `AdapterQuirks.interrupt_route`. |
| `src/tdb/dap/client.py` | `send_signal` (`tdbSignal`). |
| `src/tdb/adapters/{bash,perl,tcsh,ruby,powershell}/` | `tdbSignal` handlers. |
| `src/tdb/widgets/modals.py`, `src/tdb/app.py`, `src/tdb/app_handlers/dap_events.py` | Modal, detach flow, output transcript. |
| `src/tdb/cli.py` | Shared-loop `_run_tui`, post-TUI dispatch, exit code. |
| `src/tdb/replay.py`, `src/tdb/replay_tui.py` | `detach` ends a replay. |
| `docs/using/debugging-controls.md`, `docs/using/run-detached.md` (new), `mkdocs.yml` | User docs. |

---

### Task 1: The relay loop

**Files:**
- Create: `src/tdb/detach_relay.py`
- Test: `tests/unit/test_detach_relay.py`

**Interfaces:**
- Consumes: `tdb.run_mode.ConsoleRunHandler`, `tdb.run_mode.final_exit_code`; a controller exposing `continue_()`, `stop()`, `supports_stdin`, `write_stdin(text)`, `close_stdin()`, `state.is_terminated`, and (from Task 2) `async interrupt_debuggee() -> bool`.
- Produces:
  - `async def relay(controller, console: ConsoleRunHandler, *, stdin: BinaryIO | None = None, install_signals: bool = True) -> int` — runs until the program exits; returns tdb's exit code. `stdin=None` means `sys.stdin.buffer`.
  - `INTERRUPT_EXIT = 130`

Behavior the tests pin:
- Waits on `console.exited` / `console.stopped` (events, no polling).
- On `stopped`: clear it, `await controller.continue_()`.
- stdin: only if `controller.supports_stdin`. A daemon thread reads `stdin.read1(65536)`; each chunk is decoded (utf-8, `errors="replace"`) and passed to `controller.write_stdin` via `loop.call_soon_threadsafe` → task; empty read → `controller.close_stdin()`. Before starting, if `stdin.isatty()` and `termios` imports, `termios.tcflush(fd, TCIFLUSH)`; any `OSError`/`termios.error` is ignored. `RuntimeError` from `write_stdin` (program closed stdin) stops forwarding quietly.
- SIGINT (when `install_signals`): `ok = await controller.interrupt_debuggee()`; if `ok` is False → `await controller.stop()`, return `INTERRUPT_EXIT`. POSIX uses `loop.add_signal_handler`; Windows uses `signal.signal` + `call_soon_threadsafe`.
- SIGTERM / SIGHUP (POSIX): `await controller.stop()`, return `128 + signum`.
- On `exited`: `await controller.stop()` (cleanup; ignore exceptions), return `final_exit_code(console)`. If `console.exited` is already set on entry, same path immediately.
- Signal handlers are removed/restored in a `finally`.

- [ ] **Step 1: Write failing tests** in `tests/unit/test_detach_relay.py` with a `FakeController` (records calls; `supports_stdin` settable; `interrupt_debuggee` returns a settable bool) and a real `ConsoleRunHandler`. All call `relay(..., install_signals=False)` except the signal tests.

```python
async def test_returns_program_exit_code():          # console.on_exited(3) -> 3; controller.stop called once
async def test_terminated_without_exit_code_is_1():  # console.on_terminated() -> 1
async def test_already_exited_returns_immediately():
async def test_stop_is_auto_continued():             # on_stopped(1,"breakpoint") x3 -> continue_ called 3 times, then exit
async def test_idle_relay_does_not_spin():           # no events for 0.2s -> continue_ call count == 0
async def test_stdin_forwarded_then_eof():           # stdin=io.BytesIO(b"a\nb\n") -> write_stdin got "a\nb\n" total; close_stdin called
async def test_stdin_ignored_without_route():        # supports_stdin False -> stdin never read (BytesIO position stays 0)
async def test_write_stdin_runtime_error_is_quiet(): # write_stdin raises RuntimeError -> relay still returns exit code
@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
async def test_sigint_delivered_to_program():        # os.kill(os.getpid(), SIGINT) -> interrupt_debuggee called; relay keeps running
async def test_sigint_fallback_stops_and_returns_130():  # interrupt_debuggee -> False
async def test_sigterm_stops_and_returns_143():
async def test_signal_handlers_restored():           # signal.getsignal(SIGINT) same before/after
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_detach_relay.py -v` — expect import failure.
- [ ] **Step 3: Implement `relay`** as specified above.
- [ ] **Step 4: Run** the same command — expect all PASS.
- [ ] **Step 5: Commit** `feat: add detach relay loop`.

---

### Task 2: Controller — strip stops and interrupt the debuggee (Python path)

**Files:**
- Modify: `src/tdb/session/controller.py`, `src/tdb/session/terminal.py` (`PipeLauncher`), `src/tdb/languages/base.py` (`AdapterSpec`, `AdapterQuirks`)
- Test: `tests/unit/test_controller_detach.py`

**Interfaces:**
- Produces:
  - `AdapterSpec.detach_prep_commands(self) -> tuple[str, ...]` — default `()`. Debugger console commands run by `strip_stops`.
  - `AdapterQuirks.interrupt_route: str | None = None` — `"request"` (tdb-owned adapters, `tdbSignal`), `"pid"` (signal the pid from the DAP `process` event), `None`. Set in later tasks; the `PipeLauncher` path needs no quirk.
  - `PipeLauncher.send_interrupt(self) -> bool` — `SIGINT` on POSIX, `CTRL_BREAK_EVENT` on Windows; False if no live process.
  - `DebugController.strip_stops(self) -> None` — for the parent client and every child client: `setBreakpoints` with `[]` for every source in `state.breakpoints` (state itself untouched), `set_function_breakpoints([])`, `set_exception_breakpoints([])`, `cleanup_run_to_cursor()`, then each `detach_prep_commands()` entry via `client.evaluate(cmd, context="repl")`. Every request is best-effort: a `DAPError`/timeout is logged and the rest still run. No-op if `state.is_terminated`.
  - `DebugController.interrupt_debuggee(self) -> bool` — PipeLauncher → `send_interrupt()`; else `interrupt_route == "request"` → `await client.send_signal("SIGINT")` (Task 6), True unless it raises; `"pid"` → `os.kill(self._debuggee_pid, SIGINT)` if known; otherwise False. Never raises.
  - `DebugController._debuggee_pid: int | None` — set from the DAP `process` event body `systemProcessId` (register in `_setup_event_handlers`).

- [ ] **Step 1: Write failing tests** (follow the fake-client pattern of `tests/unit/test_controller_actions.py`):

```python
async def test_strip_stops_clears_source_function_exception_bps():  # two sources in state -> 2 setBreakpoints([]) + function [] + exception []; state.breakpoints unchanged
async def test_strip_stops_reaches_child_clients():
async def test_strip_stops_survives_failing_request():   # setExceptionBreakpoints raises DAPError -> prep commands still sent
async def test_strip_stops_runs_detach_prep_commands():  # profile adapter returns ("x","y") -> evaluated in order
async def test_strip_stops_noop_when_terminated():
async def test_interrupt_uses_pipe_launcher():           # returns True, fake process got SIGINT
async def test_interrupt_pid_route_uses_process_event_pid():  # fire `process` event {systemProcessId: N}; monkeypatched os.kill sees (N, SIGINT)
async def test_interrupt_without_route_returns_false():
async def test_interrupt_never_raises():                 # os.kill raises ProcessLookupError -> False
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_controller_detach.py -v` — FAIL.
- [ ] **Step 3: Implement** the Interfaces above.
- [ ] **Step 4: Run** — PASS; also `uv run pytest tests/unit -k "controller or pipe" -q` — no regressions.
- [ ] **Step 5: Commit** `feat: controller strip_stops and interrupt_debuggee`.

---

### Task 3: Modal and the app's detach flow

**Files:**
- Modify: `src/tdb/widgets/modals.py` (`_QuitConfirmModal`), `src/tdb/app.py` (`__init__`, restart path ~l.915-945, `action_confirm_quit`, new `_detach_flow`), `src/tdb/app_handlers/dap_events.py` (`on_output`)
- Test: `tests/unit/test_quit_modal_detach.py`, `tests/unit/test_app_detach_flow.py`

**Interfaces:**
- Consumes: `DebugController.strip_stops()` (Task 2).
- Produces:
  - `_QuitConfirmModal(can_detach: bool = False, ends_program: bool = True)`; dismisses with `"quit"`, `"detach"`, or `None`. With `can_detach=False` there is no `d` binding and no `d` line. Height grows to fit three lines.
  - `TdbApp.event_switch: SwappableEventHandler` — non-adopted sessions now always pass a `SwappableEventHandler` wrapping `self._event_handler` to `DebugController` (constructor **and** the restart path), so `PipeLauncher`'s bound `on_output` follows a retarget.
  - `TdbApp.run_detached: bool` (False by default) — set by the detach flow, read by the CLI.
  - `TdbApp.program_output: list[tuple[str, str]]` — `(text, category)` for every `DapOutput` whose category is `stdout` or `stderr`, in arrival order, capped at 4 MiB of text (drop oldest); `TdbApp.program_output_truncated: bool`. Cleared on restart.
  - `TdbApp.detach_available() -> bool` — not adopted, not `_is_quitting`, no `_startup_error`, `not state.is_terminated`, `not state.is_post_mortem`, not a replay-TUI playback, session started (`state.is_ready`).
  - `TdbApp._detach_flow()` (worker): `_confirm_discard_edits()` → re-check `detach_available()` (if the program ended meanwhile, fall through to `_finish_quit()`) → set `_is_quitting` → `recorder.record("detach", [])` → save breakpoints exactly as `_finish_quit` does → `await controller.strip_stops()` → shut the RPC server as `_finish_quit` does → `self.run_detached = True` → `self.exit()`. It does **not** call `controller.stop()` and does **not** continue the program (the CLI does, after the terminal is restored).

`action_confirm_quit` (non-adopted branch) pushes `_QuitConfirmModal(can_detach=self.detach_available(), ends_program=not self.controller.is_remote_attach)` and routes `"quit"` → existing `action_quit_debugger`, `"detach"` → `run_worker(self._detach_flow(), name="detach")`. Factor the breakpoint-save lines out of `_finish_quit` into `_save_breakpoints()` and use it from both.

- [ ] **Step 1: Write failing modal tests** (Textual `run_test` pilot):

```python
async def test_modal_three_lines_when_detachable():   # text contains "q: quit (ends the program)", "d: run detached", "ESC: cancel"
async def test_modal_attach_wording():                # ends_program=False -> "q: quit" and no "(ends the program)"
async def test_modal_without_detach_ignores_d():      # can_detach=False: pressing d leaves modal open; no "run detached" text
async def test_q_d_escape_results():                  # "quit" / "detach" / None
```

- [ ] **Step 2: Write failing app tests** (construct `TdbApp` with a stub controller as existing app unit tests do):

```python
async def test_detach_flow_strips_saves_and_exits_without_stop():  # strip_stops awaited, save_breakpoints called, controller.stop NOT called, run_detached True, recorder got ("detach", [])
async def test_detach_unavailable_when_terminated_or_post_mortem_or_adopted():
async def test_detach_flow_falls_back_to_quit_if_program_ended():  # state terminated before flow runs -> controller.stop called, run_detached False
async def test_detach_while_running_sends_no_continue():           # controller.continue_ never called by the flow
async def test_double_d_runs_flow_once():
async def test_program_output_captured_in_order_and_capped():      # stdout/stderr kept, "console" category dropped; >4 MiB -> truncated flag, total <= 4 MiB
async def test_controller_handler_is_swappable_after_restart():
async def test_ctrl_q_still_quits_immediately():
```

- [ ] **Step 3: Run** both test files — FAIL.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run** both files plus `uv run pytest tests/unit -k "modal or quit or restart or run_mode" -q` — PASS.
- [ ] **Step 6: Commit** `feat: quit modal offers run detached`.

---

### Task 4: CLI handover — TUI and relay on one loop

**Files:**
- Modify: `src/tdb/cli.py` (`_run_tui`), `src/tdb/detach_relay.py`
- Test: `tests/unit/test_cli_detach_handover.py`, `tests/integration/test_detach_python.py`

**Interfaces:**
- Consumes: `relay` (Task 1); `TdbApp.run_detached`, `.event_switch`, `.program_output`, `.program_output_truncated`, `.controller` (Task 3).
- Produces: `async def run_after_tui(app, program: str) -> int` in `detach_relay.py`:
  1. `console = ConsoleRunHandler()`; if `controller.state.is_terminated`, seed it from state (exit code if known) so `relay` returns at once.
  2. `app.event_switch.retarget(console)`.
  3. Write `program_output` to `sys.stdout`/`sys.stderr` by category, flush; if truncated first print `tdb: earlier program output truncated` to stderr.
  4. Print the detach notice (Global Constraints) to stderr.
  5. If the session is stopped (`not state.is_running`), `await controller.continue_()`.
  6. `return await relay(controller, console)` — all inside `run_mode.stop_session_on_error(controller)`.

`_run_tui` becomes: build the app as now; `asyncio.run(main())` where `main` awaits `app.run_async()` then, if `app.run_detached`, returns `await run_after_tui(app, args.program)`, else `None`. Keep `recorder.close()` in `finally` and `_exit_on_startup_error(app)`; `sys.exit(code)` when a code came back. Check every other `app.run()` caller in `cli.py` (replay-TUI, post-mortem) and leave those unchanged.

- [ ] **Step 1: Write failing unit tests** (fake app object + `FakeController` from Task 1's test module):

```python
async def test_retargets_then_replays_output_then_notice(capsys):  # stdout has earlier stdout text; stderr has earlier stderr text then "tdb: detached — prog.py running; Ctrl-C interrupts it"
async def test_truncation_note_printed(capsys):
async def test_continues_only_when_stopped():
async def test_program_already_ended_returns_its_code():   # Review Focus 1
async def test_relay_error_stops_session():                # relay raises -> controller.stop called, exception propagates
```

- [ ] **Step 2: Write the failing integration test** `tests/integration/test_detach_python.py`. Add a pty driver `tests/integration/detach_harness.py` (model the pty spawn on `test_lost_terminal.py`): `run_detached(argv, *, keys=b"qd", stdin_lines=(), interrupt_after=None, timeout=30) -> (output: str, exit_code: int)` — spawns `python -m tdb ...` on a pty, waits for the TUI's entry stop, sends `keys`, then lines / Ctrl-C byte `\x03`, collects everything until exit. Fixture program `tests/integration/fixtures/detach_prog.py`: prints `before`, (breakpoint-able line), prints `after`, reads one stdin line and prints `got:<line>`, `sys.exit(7)`; with argv `loop` it prints `looping` then sleeps forever.

```python
def test_python_detach_output_stdin_exit_code():   # "after" and "got:hello" in output; exit_code == 7; "tdb: detached" in output
def test_python_detach_ctrl_c_kills_looping_program():  # exit_code == 130 (or -SIGINT mapped to 130); no tdb/python child left (reuse _tree from test_lost_terminal)
def test_python_detach_saved_breakpoint_does_not_stop():  # -k on the "after" line; after d the program still finishes with 7
def test_q_q_still_kills_program():                # keys=b"qq" -> "after" not in output
def test_q_escape_keeps_tui():                     # keys=b"q\x1b" then b"qq" -> exits cleanly
```

- [ ] **Step 3: Run** both — FAIL.
- [ ] **Step 4: Implement.** Map a debuggee killed by SIGINT to exit code 130 in `relay` if `final_exit_code` gives a negative/odd value on POSIX.
- [ ] **Step 5: Run** both files, then `uv run pytest tests/unit/test_cli*.py tests/integration/test_lost_terminal.py -q` — PASS.
- [ ] **Step 6: Commit** `feat: hand over from TUI to detach relay`.

---

### Task 5: `--terminal` and attach sessions disconnect for real

**Files:**
- Modify: `src/tdb/session/controller.py`, `src/tdb/detach_relay.py`
- Test: `tests/unit/test_detach_disconnect.py`, additions to `tests/integration/test_detach_python.py`

**Interfaces:**
- Produces:
  - `DebugController.detach_disconnect(self) -> bool` — closes child clients, sends `disconnect(terminate=False)`, stops the DAP client **without** closing/killing a debuggee; returns True if the debuggee is expected to live on. Returns False (and does nothing) when `profile.adapter.quirks.survives_disconnect` is False.
  - `AdapterQuirks.survives_disconnect: bool = True`. The builder sets it False for each tdb-owned adapter whose `--terminal` debuggee dies when the adapter goes (verify each of perl, bash, tcsh, ruby, powershell with its existing `*_terminal` integration test harness; record the result in the quirk's comment).
  - `run_after_tui` branches: if `controller.is_remote_attach` or the session used `--terminal` → skip output replay and stdin; continue if stopped; `if await controller.detach_disconnect(): print("tdb: detached — {program} keeps running"); return 0`; else fall through to `relay` (nothing to relay, but it holds the session until exit and auto-continues).

- [ ] **Step 1: Failing unit tests:**

```python
async def test_attach_session_disconnects_without_terminating():  # disconnect sent with terminateDebuggee False; exit 0
async def test_terminal_session_non_surviving_adapter_falls_back_to_relay():
async def test_detach_disconnect_closes_children_first():
```

- [ ] **Step 2: Failing integration test** (Python remote attach; reuse the listener fixture from the existing remote-attach tests): start a `debugpy --listen` program that writes a heartbeat file, `tdb -r host:port`, `qd`; assert tdb exits 0 and the heartbeat file keeps advancing; then kill the program in teardown.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat: detach disconnects --terminal and attach sessions`.

---

### Task 6: `tdbSignal` for tdb-owned adapters

One sub-task per adapter; each is independently reviewable. 6a sets the pattern.

**Interfaces:**
- Produces: `DAPClient.send_signal(self, name: str = "SIGINT") -> None` — tdb-private request `tdbSignal` with `{"signal": name}`; and `interrupt_route="request"` on the profile's `AdapterQuirks`.
- Adapter contract: `tdbSignal` is accepted while the script is running (not gated on "stopped"); only `"SIGINT"` is accepted (anything else → DAP error); it signals the debuggee the same way that adapter's teardown addresses it (process or group); in `--terminal` mode it signals the recorded debuggee pid; with no debuggee it returns a DAP error.
- Observable contract (integration): after detach, Ctrl-C makes a looping script die and tdb exit 130; a script with its own INT handler runs the handler and continues/exits as written. The language debugger's own INT handling must not turn the signal into a debugger stop that swallows it — if a `stopped` arrives it is auto-continued and the script must still observe the interrupt.

| Sub-task | Adapter files | Profile | Notes for the implementer |
|---|---|---|---|
| 6a | `src/tdb/dap/client.py`, `src/tdb/adapters/bash/{server,session}.py` | `languages/bash.py` | Handler `_on_tdbSignal` next to `_on_tdbStdin`. The DEBUG trap must let the script's own `trap ... INT` (or default death) take effect. |
| 6b | `src/tdb/adapters/perl/{server,session}.py` | `languages/perl.py` | `perl -d` installs `$SIG{INT}` = `DB::catch`; the shim must restore the program's/default handler when told the session is detached, or re-raise. Windows perl has no real SIGINT delivery: return a DAP error there so the fallback applies. |
| 6c | `src/tdb/adapters/tcsh/{server,session,guardian}.py` | `languages/tcsh.py` | Add to the handler table at `server.py:139`; signal via the guardian's process-group path. |
| 6d | `src/tdb/adapters/ruby/server.py` | `languages/ruby.py` | rdbg traps INT to pause; after detach the signal must reach the program (`Interrupt` raised). |
| 6e | `src/tdb/adapters/powershell/server.py` | `languages/powershell.py` | pwsh has no script-level SIGINT handler; delivering SIGINT to pwsh ends the script. If that cannot be made reliable, leave `interrupt_route=None` and document the fallback. |

Per sub-task, **Files → Test:** `tests/integration/test_<lang>_detach.py` (+ `tests/unit/test_dap_client.py` for 6a).

- [ ] **Step 1 (6a only): failing unit test** `test_send_signal_sends_tdbSignal_request` in `tests/unit/test_dap_client.py`.
- [ ] **Step 2: failing integration tests** per adapter, using `detach_harness.run_detached` and a fixture script equivalent to `detach_prog.py` (print `before`/`after`, read a line, exit 7; `loop` mode; `trap` mode that prints `caught` on INT and exits 5):

```python
def test_<lang>_detach_output_stdin_exit_code():   # "after", "got:hello", exit 7
def test_<lang>_detach_ctrl_c_kills_loop():        # exit 130, no leftover processes
def test_<lang>_detach_ctrl_c_runs_program_handler():  # "caught" in output, exit 5 (skip for 6e)
```
  Each module is skipped when the toolchain is absent, with the same skip helper that language's existing integration tests use.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement** handler + quirk. **Step 5: Run** that language's whole integration set (`uv run pytest tests/integration -k <lang> -q`) — PASS.
- [ ] **Step 6: Commit** `feat(<lang>): deliver Ctrl-C after detach`.

---

### Task 7: Native debuggers — gdb, lldb-dap, dlv, earlybird

**Files:**
- Modify: `src/tdb/languages/cpp.py`, `rust.py`, `go.py`, `ocaml.py`
- Test: `tests/integration/test_native_detach.py`, `tests/unit/test_detach_prep_commands.py`

**Interfaces:**
- Consumes: `AdapterSpec.detach_prep_commands`, `AdapterQuirks.interrupt_route`, `_debuggee_pid` (Task 2).
- Produces, per adapter spec: `interrupt_route="pid"` where the adapter emits a `process` event with `systemProcessId`, and `detach_prep_commands()` returning the commands that make SIGINT pass to the inferior without stopping:
  - gdb: `("handle SIGINT nostop noprint pass",)`
  - lldb-dap: `("process handle SIGINT --pass true --stop false --notify false",)`
  - dlv, earlybird: `()`.
- If an adapter gives no pid (check each: gdb DAP by version, dlv, earlybird), add a pid query in that profile via an existing hook or leave `interrupt_route=None` so the fallback (stop, exit 130) applies. Record which in the quirk comment; Task 9 documents it.

- [ ] **Step 1: Failing unit test:** each native `AdapterSpec` returns the exact tuple above.
- [ ] **Step 2: Failing integration tests**, parametrized over the C (gdb), C++ (lldb-dap if present), Rust, Go and both OCaml toolchains with existing skip helpers; fixture programs print `before`, `after`, exit 7; `loop` mode; C `handler` mode installing a SIGINT handler that prints `caught` and exits 5:

```python
def test_native_detach_output_and_exit_code(lang):   # "after" in output, exit 7
def test_native_detach_ctrl_c_ends_loop(lang):       # exit 130, no leftover debugger or program process
def test_gdb_detach_ctrl_c_reaches_program_handler():  # "caught", exit 5
def test_native_detach_function_entry_breakpoint_cleared():  # Go main.main / gdb bootstrap bp do not stop the program after d
```
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** `uv run pytest tests/integration -k "native or cpp or gdb or go or rust or ocaml" -q` — PASS.
- [ ] **Step 6: Commit** `feat: deliver Ctrl-C to native debuggees after detach`.

---

### Task 8: Record / replay

**Files:**
- Modify: `src/tdb/replay.py` (~l.193), `src/tdb/replay_tui.py` (~l.108, `_ACTIONS`)
- Test: `tests/unit/test_replay_loader.py`, `tests/unit/test_replay_tui.py`

**Interfaces:**
- Consumes: the `detach` record written by Task 3.
- Produces: both replays treat `{"action": "detach"}` exactly as `quit` — the session is ended (terminated, no relay) and the loop breaks; it is reported `ok`.

- [ ] **Step 1: Failing tests:**

```python
def test_replay_detach_ends_session_like_quit():      # recording [..., detach] -> saw_quit path, errors == 0
async def test_replay_tui_detach_quits_app():         # driver calls app.action_quit_debugger; returns (True, "detach")
```
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest tests/unit -k "replay or record" -q` — PASS.
- [ ] **Step 5: Commit** `feat: replay treats detach as end of session`.

---

### Task 9: Exit-path audit, docs, full suite

**Files:**
- Create: `docs/using/run-detached.md`; Modify: `docs/using/debugging-controls.md` (keys table: add `q d` — Run detached), `docs/using/run-mode.md` (one cross-reference), `mkdocs.yml` nav (and whatever list `tdb.documentation` stitches for `--doc`)
- Test: additions to `tests/integration/test_detach_python.py`; existing docs tests

- [ ] **Step 1: Failing audit tests** (pty harness, Python):

```python
def test_program_exits_while_modal_open_then_d():   # tdb exits with program's code, no traceback text in output
def test_d_during_unsaved_edits_prompt_cancel():    # cancel the prompt -> still in TUI
def test_terminal_hangup_during_relay_kills_program():   # close pty master after detach -> no leftover processes (reuse _tree)
def test_sigterm_during_relay_kills_program_exit_143():
def test_adapter_death_during_relay_exits_nonzero_no_orphan():  # SIGKILL the adapter pid after detach
def test_detach_with_child_process_program():       # multiprocessing fixture: child output still arrives, exit code propagated
def test_stdin_from_file_reaches_program():         # tdb's stdin not a tty (Review Focus 4)
```
- [ ] **Step 2: Run** — fix whatever fails in the owning module; re-run until PASS.
- [ ] **Step 3: Write `docs/using/run-detached.md`:** what `d` does; the program stays on pipes (`isatty()` false); no stdin for C/C++/Rust/Go/OCaml; `--terminal`/attach disconnect; the table of languages where Ctrl-C is delivered vs. where it ends the program (from the quirks set in Tasks 6–7); exit codes (program's, 130, 128+n).
- [ ] **Step 4: Run** the docs tests (`uv run pytest tests -k "doc" -q`) — PASS.
- [ ] **Step 5: Run the full suite** `uv run pytest` (memory-capped wrapper if present) — PASS; then check for leftover processes (`pgrep -fa "tdb|debugpy|detach_prog"`), expecting none from this run.
- [ ] **Step 6: Commit** `docs: run detached; test: exit-path audit`.
