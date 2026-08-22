# OCaml exit-path / lifecycle audit (Task 11)

Walks every lifecycle/exit path for OCaml debugging (native lldb-dap route
and bytecode ocamlearlybird route) against real compiled OCaml binaries,
per CLAUDE.md's "audit ALL exit paths" rule. Consumes the complete feature
from Tasks 2-10.

**Method.** A throwaway pytest script drove real sessions: a real
`TdbApp` (Textual's `run_test()` pilot) wired to a real `DebugController`
talking to a real `lldb-dap` or `ocamlearlybird` subprocess debugging a
compiled `ocaml_domains.exe` / `ocaml_fatal.exe` / bytecode `add.byte` /
`ocaml_fatal.byte`, compiled fresh into a scratch directory (never the
repo). The script is **not** committed (per the brief). Where an existing
integration/unit test already covers a row for the OCaml profile, that
test is cited as additional/primary evidence instead of re-deriving it.

**Process hygiene.** After each exit path, orphan-checked scoped to (a)
PIDs not present in a pre-run baseline snapshot, AND (b) direct children
of the audit's own process (matching on adapter comm name / our own
scratch-dir binaries) — never touching the unrelated, pre-existing
`lldb-server` (tdb_rust_support) or other concurrent agents' processes
also running on this shared machine. A full run of `tests/unit` (1469
passed, 1 skipped) confirms no regression.

**Result: no product bugs found.** The table below has 18 data rows: 17
resolve to PASS with scripted or cited evidence, and 1 (the Threads
modal's `a`-toggle cursor-reset row, one of the two rows carried over
from Task 9's review) resolves to a documented observation rather than a
pass/fail verdict — it isn't a bug, just a pre-existing, shared
behavior worth recording rather than silently dropping. Every
exit/lifecycle path already does the right thing. Every failure hit
while building the audit script was a bug in the *script* (documented
under each row's Notes), not in tdb — root-caused and fixed in the
script; no tdb source changes were needed, so there are no separate fix
commits for this task.

| Path | Action | Expected | How verified | Result | Notes |
|---|---|---|---|---|---|
| quit key | `q` while stopped at a domain breakpoint (native, lldb-dap) | clean exit, no orphan lldb-dap/debuggee | Scripted: real `TdbApp` + `OCamlLldbAdapter` + `ocaml_domains.exe`, breakpoint on `Atomic.incr counter;`, press q,q, poll for exit, PID-scoped orphan check | PASS | — |
| quit while running | `q` while continuing (native, no breakpoint, `spin.exe` infinite loop) | same | Scripted: same harness, no breakpoints so the program runs freely, quit mid-run | PASS | — |
| Ctrl-C (main TUI) | press ctrl+c while stopped | clean exit, no orphan | Scripted: ctrl+c is bound to `focus_code` (not quit) in `TdbApp.BINDINGS`; verified it does NOT exit the app and DOES refocus the Code View, then quit normally via q,q | PASS | Documented behavior (README.md "Run Mode" section, `BINDINGS` in `src/tdb/app.py`): inside the interactive TUI, Ctrl+C is a plain keybinding — Textual's raw terminal mode never lets the keystroke reach the OS as a real `SIGINT` while the TUI owns the tty, so there is no OS-level "interrupt" exit path here at all. Real signal-driven interruption is `--run` mode's job (its own row below), which is exercised separately with an actual `SIGUSR1`. |
| restart | `R` at a breakpoint (native) | fresh session stops again; thread labels still correct | Scripted: press `R`, wait for `app.controller` to become a new instance and re-stop, then call `classify_ocaml_threads` against the fresh session's live threads/stacks and assert a "Domain N" label is present; then quit cleanly | PASS | — |
| ESC modals | open Threads modal (incl. `a` toggle), ESC | modal closes, main views intact | Scripted: `app.action_menu_threads()`, wait for `ThreadsModal`, press `a`, press `escape`, assert modal gone and `controller.state.can_step` still true (session intact) | PASS | Cursor-reset-to-row-0 sub-observation below. |
| *(carried over)* `a` toggle cursor reset | after pressing `a` in the Threads modal | — | Scripted: asserted `DataTable.cursor_row == 0` immediately after the `a` toggle | Observation, not a bug | Confirmed in source: `_InspectableListModal._reload_after_items_change` (`src/tdb/widgets/_inspection_modal.py`) always calls `self._show_detail(0)` after any item-list change — pre-existing, shared base-class behavior used by every inspection modal (Threads, Breakpoints, etc.), not something Task 8-10's OCaml work introduced. UX call: mildly jarring on a long thread list but consistent everywhere else in the app; no fix needed unless a future UX pass wants "preserve cursor by ID across a filter toggle" as a general `_InspectableListModal` improvement (out of scope here). |
| *(carried over)* stopped thread hidden from filtered list | Threads modal opens while the current/stopped thread is classified hidden | modal opens sanely (cursor valid, current-thread bolding simply absent) | Scripted, modal-level (no live adapter needed): built a `ThreadsModal` directly with 2 threads where the "current" thread is the hidden one; asserted the hidden thread is excluded from `_items` and `_initial_cursor_index()` falls back to `0` without raising | PASS | `_initial_cursor_index` degrades correctly: loops over the *visible* items looking for `current_thread_id`, falls through to `0` when absent. |
| menu quit | "File menu → Quit" | clean exit | Scripted: `Ctrl+Q` (labeled "Quit", shown in the Footer) at a breakpoint → app exits directly | PASS | There is **no literal "File → Quit" dropdown item** in this codebase: the menu bar has only "Configure" (Color Theme/Keybindings/Step Mode) and "Help" (Documentation/About) dropdowns, plus quick-action buttons (File opens the file picker directly, not a submenu; Threads/Processes/Async Tasks are quick jumps). `Ctrl+Q` is the closest menu-equivalent "Quit" command. Unlike `q` (which routes through `_QuitConfirmModal`'s "hit q again" confirmation for a non-adopted session), `Ctrl+Q` (`action_quit_debugger`) quits **directly, with no confirmation dialog** — both paths correctly call `await self.controller.stop()` before `self.exit()` (`src/tdb/app.py`). The row above exercises a **non-adopted** session specifically: per `action_quit_debugger`, the direct-quit-with-no-confirmation behavior is conditional on `not self._adopted` — an adopted session (`tdb --run`) gets the detach/terminate confirm dialog instead on the very same `Ctrl+Q`/`q` keys, which is covered separately by `tests/unit/test_app_adopted_session.py` (not re-derived here since it's adapter-agnostic). This is pre-existing, language-agnostic app behavior, not OCaml-specific, but confirmed here against a real OCaml lldb-dap session. |
| `--run` | `tdb --run ./spin.exe`, then a real `SIGUSR1` | pause lands on a VISIBLE thread (Task 8) | Scripted: `run_mode.run()` with the real OCaml lldb-dap profile, a custom `tui_episode` callback, and a genuine `os.kill(os.getpid(), signal.SIGUSR1)` fired from a concurrent task (mirrors `tests/integration/test_run_mode.py`'s own pattern) — episode asserts `console.last_stop`'s thread id is a member of `classify_ocaml_threads(...)`'s visible-id set | PASS | — |
| `--terminal` | debuggee I/O in external terminal (lldb-dap `runInTerminal`) | works | Scripted: real `DebugController` + `OCamlLldbAdapter`, `terminal="fakeem"` with `_TERMINAL_SPECS` monkeypatched to a fake emulator script (same trick as `tests/unit/test_terminal_launcher.py`), breakpoint hit through the fake terminal, `argv.log` confirms `runInTerminal` invoked the emulator with `-e`, then continue-to-exit shows `sum=6 total=300000` | PASS | Exercises the real, unmocked lldb-dap `runInTerminal` reverse-request handshake end-to-end (no GUI/X11 needed — the "terminal" is a `/bin/sh` script that `exec`s the given command, exactly mirroring what a real terminal emulator does for `TerminalLauncher.handle_run_in_terminal`). |
| natural exit | let the program run to completion | exit code + `sum=6` in console, no hang | Cited: `tests/integration/test_ocaml_native_session.py::test_step_and_continue_at_domain_breakpoint` (existing, asserts `"sum=6" in handler.drain_output()` after removing the breakpoint and continuing to termination). Also independently reconfirmed as a side effect of the `--terminal` row above (`sum=6 total=300000` printed) and the bytecode natural-exit row below | PASS | — |
| fatal exit | `tdb ./ocaml_fatal.exe`, continue past the stop | error modal shows parsed backtrace | Scripted: ran `ocaml_fatal.exe` directly (`OCAMLRUNPARAM=b`) to get genuine Printexc stderr, fed it through the real `TdbApp` + `registry.resolve("ocaml")` + `app._dap._check_stderr_traceback(...)` path (same pattern as `tests/unit/test_error_modal_routing.py`), asserted the modal is pushed and `'Failure("boom")'` appears in `panels.last_exception_text` | PASS | Also cited: `tests/integration/test_ocaml_native_session.py::test_uncaught_exception_stops_or_parses` (live-session variant, confirms the preRunCommands `caml_fatal_uncaught_exception` breakpoint + `parse_ocaml_error` end-to-end). |
| no debug info | breakpoint in a binary built WITHOUT `-g` | existing unbound-breakpoint console warning appears once, mentioning `-g` | Scripted: compiled `ocaml_domains.exe` **without** `-g` into the scratch dir, set a breakpoint on the same source/line via a real lldb-dap session, asserted the console output contains `warning` and `-g` | PASS | Confirms the generic `DebugController._emit_unbound_warning` mechanism (`src/tdb/session/controller.py`) fires correctly for the OCaml/lldb-dap profile specifically; the mechanism itself is also covered generically by `tests/unit/test_controller_actions.py::test_unbound_breakpoints_warn_on_console` and `test_do_configure_warns_on_unbound_breakpoints`. |
| bytecode: quit | `q` at a breakpoint, `--adapter ocamlearlybird` | clean exit, no orphan `ocamlearlybird`/debuggee | Scripted: real `TdbApp` + `EarlybirdAdapter` (`~/.opam/default/bin/ocamlearlybird`) + `add.byte`, breakpoint on `print_endline msg;`, quit via q,q | PASS | — |
| bytecode: Ctrl-C | ctrl+c while stopped | same focus-code behavior | Scripted: same as native — ctrl+c focuses Code View, does not quit | PASS | Keybinding is adapter-agnostic; same conclusion as the native row. |
| bytecode: restart | `R` at a breakpoint | fresh session stops again | Scripted: press `R`, wait for a new controller + new stop, assert `stack_frames` populated, quit cleanly | PASS | — |
| bytecode: natural exit | let the program run to completion | exit code + output, no hang | Scripted: remove the breakpoint, `c`ontinue, wait for `state.is_terminated`, then quit (q,q) | PASS | — |
| bytecode: fatal exit | continue past an uncaught exception under earlybird | tdb degrades gracefully (no crash, no false-positive parsed-backtrace modal) | Scripted: fed earlybird's real generic termination text (`"Program exited due to Uncaught_exc"`) through `app._dap._check_stderr_traceback`, asserted **no** modal is pushed | PASS | Per Task 10's documented external limitation (also re-confirmed live by `tests/integration/test_ocaml_earlybird_session.py::test_fatal_error_parses`): earlybird intercepts the uncaught exception itself and never delivers the real exception text to tdb's stdout/stderr, so `parse_ocaml_error` correctly declines to parse the generic message rather than guessing — this row verifies tdb does NOT crash or false-positive, not that a backtrace appears (that's not achievable for this adapter). |

## Script bugs found and fixed while building the audit (not tdb bugs)

Every one of these surfaced as a scripted-test FAIL during development,
was root-caused with the systematic-debugging approach, and was fixed in
the (uncommitted) scratch script itself — none required a tdb source
change:

1. **Wrong profile in the harness.** Early native-TUI tests constructed
   `TdbApp` without an explicit `profile=`, so `DebugController` defaulted
   to `PYTHON_PROFILE` (real production code always resolves the profile
   from the program path *before* constructing `TdbApp` — see
   `src/tdb/cli.py`'s `registry.detect`/`registry.resolve` call ahead of
   `_run_tui`). Fixed by passing `build_ocaml_profile(adapter="lldb-dap")`
   explicitly, matching what `cli.py` does for real.
2. **Wrong assumption about Ctrl+Q.** Assumed `Ctrl+Q` pushes a
   confirmation modal like `q` does; it doesn't for a non-adopted session
   (`action_quit_debugger` quits directly, `action_confirm_quit` is the
   one behind `q`). Fixed the test's expectation; documented in the "menu
   quit" row above.
3. **Missing breakpoint removal before continue-to-exit** in the
   `--terminal` test: `BP_LINE` sits inside a 100k-iteration loop in every
   domain, so continuing without removing it just re-hit the same
   breakpoint forever, manifesting as a 45s timeout waiting for
   `terminated`. Fixed by calling `ctrl.remove_breakpoint(...)` first,
   mirroring `test_ocaml_native_session.py`'s own `_continue_to_exit`
   helper.
4. **Missing second `q` press** in the bytecode natural-exit test (the
   `_QuitConfirmModal` needs "hit q again"); the test's loop just spun
   until timeout without ever calling `controller.stop()`, which looked
   exactly like a leaked `ocamlearlybird` process. Fixed by reusing the
   shared `_quit_and_wait` helper (presses q twice) everywhere.
5. **Cleanup helper gated on the wrong flag.** The audit script's own
   safety-net cleanup (`_cleanup_on_exit`, guarding against a leaked
   adapter subprocess if a mid-test assertion aborts before the test's own
   deliberate quit) skipped calling `controller.stop()` whenever
   `state.is_terminated` was already `True`. That flag means "the
   debuggee died," not "the adapter subprocess exited" — ocamlearlybird
   in particular stays alive and listening after the debuggee terminates,
   by design, until the user explicitly quits. Made the cleanup
   unconditional (safe/idempotent to call `controller.stop()` twice).
6. **Too-narrow orphan-detection heuristic.** An early version matched any
   `ps` line containing the scratch directory path as a substring, which
   false-positived on the audit's own shell/pytest command line (which
   naturally mentions its own script path). A later version matched on
   PID-baseline-diff plus bare adapter name, which turned out to still be
   fooled by **other concurrent, unrelated agent sessions on this shared
   machine** independently starting/stopping their own short-lived
   `lldb-dap` processes during the audit run (confirmed directly by
   inspecting one such process's cwd/argv, which pointed at an entirely
   different project's temp directory). The final heuristic requires a
   match to ALSO be a direct child of the audit script's own process
   (`ppid == os.getpid()`), which is both necessary and sufficient since
   every adapter here is spawned via `asyncio.create_subprocess_exec`
   directly from the controller's own process, never through an
   intermediate shell.

## Self-review

- Every row has real scripted evidence (or a cited existing test); no row
  is a guessed PASS.
- No FAILs remain; no tdb source changes were needed, so there are no
  separate fix commits to reference from this document.
- Full unit suite: `uv run pytest tests/unit -x -q` → 1469 passed, 1
  skipped (unchanged from the pre-audit baseline — confirms the audit
  made no product changes).
- Process hygiene: final `ps` scoped to `lldb-dap` / `ocamlearlybird` /
  our scratch binaries shows nothing but the pre-existing, unrelated
  `tdb_rust_support` artifacts on this shared machine — nothing from this
  audit was left running.
- The throwaway audit script lived under a `/tmp` scratch directory for
  the whole audit and is not part of this commit.
