# Session Record / Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tdb --record FILE` captures user debugging gestures as replayable JSON-RPC command lines; `tdb --replay FILE` re-executes them headless through the existing RPC dispatch.

**Architecture:** A `SessionRecorder` owned by `TdbApp` gets one `record(action, params)` call from each already-converged TUI gesture handler (approach A from the spec — internal machinery like statement-stepper re-steps and breakpoint-hook auto-step-out is invisible at this layer). Replay parses the JSONL file, reuses a `setup_headless_session()` extracted from `server/runner.py`, and feeds records into `RpcHandlers`' dispatch table in-process — no HTTP.

**Tech Stack:** Python 3, argparse, asyncio, textual (pilot tests via `App.run_test()`), pytest, existing tdb RPC layer (`src/tdb/server/handlers.py`).

**Spec:** `docs/superpowers/specs/2026-07-31-record-replay-design.md` — read it before starting any task.

## Global Constraints

- Run tests with `uv run pytest <paths> -q --no-cov` (never bare `pytest`; the repo's addopts need the venv's coverage plugin).
- NEVER `git add -A`, `git add .`, or `git commit -a` — stage explicit paths only (repo rule; the working tree carries unrelated user files).
- End every commit message with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- The user's shell is zsh — in test commands avoid bare `*` globs and `==` tokens.
- Cross-platform: no POSIX-only path assumptions; recordings store absolute paths as given; use `os.getcwd()`, `Path`, and JSON only.
- A formatter hook may rewrite files after Edit — if an Edit's anchor text is not found, Read the file again before retrying.
- Record shape is EXACTLY `{"t": <float>, "action": <str>, "params": <list>}`; header field names are fixed by the spec (§ File format). Do not add fields the spec doesn't name (one spec-approved addition: attach headers carry `path_mappings`).

## File Structure

- `src/tdb/session/recorder.py` — NEW: `SessionRecorder`, `NullRecorder`, `build_header()`. No tdb imports (stdlib only) so it can't tangle with app/controller.
- `src/tdb/replay.py` — NEW: `load_recording()`, `run_replay()`, `replay_main()`, `RecordingError`, `Recording`.
- `src/tdb/cli.py` — MODIFY: `--record` / `--replay` / `--timing` / `--replay-timeout` flags, validation, recorder construction in `_run_tui`, `_run_replay` dispatch in `main()`.
- `src/tdb/app.py` — MODIFY: `recorder` constructor param + `record()` calls in gesture handlers.
- `src/tdb/server/runner.py` — MODIFY: extract `setup_headless_session()` from `run_headless()` (behavior-preserving refactor + `step_mode` parameter).
- Tests: `tests/unit/test_recorder.py`, `tests/unit/test_cli_record_flags.py`, `tests/unit/record_helpers.py`, `tests/unit/test_record_hooks_stepping.py`, `tests/unit/test_record_hooks_breakpoints.py`, `tests/unit/test_record_hooks_inspection.py`, `tests/unit/test_replay_loader.py`, `tests/integration/test_replay_session.py`, `tests/integration/test_replay_perl.py`.

---

### Task 1: SessionRecorder, NullRecorder, build_header

**Files:**
- Create: `src/tdb/session/recorder.py`
- Test: `tests/unit/test_recorder.py`

**Interfaces:**
- Consumes: nothing tdb-specific (stdlib only).
- Produces (later tasks rely on these exact names):
  - `SessionRecorder(path: str, header: dict)` — opens `path` for writing (`OSError` propagates), writes the header as JSON line 1, flushes. Attributes: `active: bool` (True until closed/failed), `on_error: Callable[[str], None] | None` (default None). Methods: `record(action: str, params: list) -> None`, `close() -> None`.
  - `NullRecorder()` — same attribute/method surface; `active` is False; `record`/`close` are no-ops.
  - `build_header(args, config) -> dict` — `args` is the parsed argparse namespace (uses `args.attach_host`, `args.attach_port`, `args.path_mappings`, `args.profile`, `args.adapter`, `args.program`, `args.args`, `args.cwd`, `args.python`, `args.no_just_my_code`), `config` is a `TdbConfig` (uses `config.step_mode`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_recorder.py
"""SessionRecorder writes spec-shaped JSONL; NullRecorder is inert."""

import json
from types import SimpleNamespace

from tdb.session.recorder import NullRecorder, SessionRecorder, build_header


def read_lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def make_header(**over):
    base = {"tdb_recording": 1, "created": "2026-07-31T00:00:00", "mode": "launch"}
    base.update(over)
    return base


def test_header_is_first_line_and_flushed_immediately(tmp_path):
    p = tmp_path / "rec.jsonl"
    rec = SessionRecorder(str(p), make_header(program="/x.py"))
    # No close, no record: header must already be on disk (flush-per-line).
    lines = read_lines(p)
    assert lines[0]["tdb_recording"] == 1
    assert lines[0]["program"] == "/x.py"
    rec.close()


def test_record_appends_t_action_params_and_flushes(tmp_path):
    p = tmp_path / "rec.jsonl"
    rec = SessionRecorder(str(p), make_header())
    rec.record("set_breakpoint", ["/x.py:3"])
    rec.record("continue", [])
    lines = read_lines(p)  # file readable before close
    assert lines[1]["action"] == "set_breakpoint"
    assert lines[1]["params"] == ["/x.py:3"]
    assert isinstance(lines[1]["t"], float)
    assert lines[2]["action"] == "continue"
    assert lines[2]["t"] >= lines[1]["t"]
    rec.close()


def test_active_flag_and_close(tmp_path):
    rec = SessionRecorder(str(tmp_path / "r.jsonl"), make_header())
    assert rec.active is True
    rec.close()
    assert rec.active is False
    rec.record("continue", [])  # after close: silently ignored
    rec.close()  # double close: no error


def test_write_failure_degrades_and_reports_once(tmp_path):
    p = tmp_path / "r.jsonl"
    rec = SessionRecorder(str(p), make_header())
    errors = []
    rec.on_error = errors.append
    rec._file.close()  # simulate the OS yanking the file mid-session
    rec.record("continue", [])
    rec.record("next", [])
    assert rec.active is False
    assert len(errors) == 1  # reported once, not per record


def test_null_recorder_is_inert(tmp_path):
    rec = NullRecorder()
    assert rec.active is False
    rec.on_error = lambda m: None
    rec.record("continue", [])
    rec.close()


def _ns(**kw):
    base = dict(
        attach_host=None,
        attach_port=None,
        path_mappings=[],
        profile=SimpleNamespace(id="python"),
        adapter=None,
        program="/abs/prog.py",
        args=["a1"],
        cwd="/abs/dir",
        python=None,
        no_just_my_code=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_build_header_launch():
    h = build_header(_ns(), SimpleNamespace(step_mode="statement"))
    assert h["tdb_recording"] == 1
    assert h["mode"] == "launch"
    assert h["language"] == "python"
    assert h["program"] == "/abs/prog.py"
    assert h["args"] == ["a1"]
    assert h["cwd"] == "/abs/dir"
    assert h["python"] is None
    assert h["adapter"] is None
    assert h["step_mode"] == "statement"
    assert h["no_just_my_code"] is False
    assert "host" not in h
    assert isinstance(h["created"], str)


def test_build_header_launch_defaults_cwd_to_getcwd():
    import os

    h = build_header(_ns(cwd=None), SimpleNamespace(step_mode="line"))
    assert h["cwd"] == os.getcwd()


def test_build_header_remote_attach():
    h = build_header(
        _ns(
            attach_host="10.0.0.5",
            attach_port=5678,
            path_mappings=[("/local", "/remote")],
            program=None,
        ),
        SimpleNamespace(step_mode="statement"),
    )
    assert h["mode"] == "remote-attach"
    assert h["host"] == "10.0.0.5"
    assert h["port"] == 5678
    assert h["path_mappings"] == [["/local", "/remote"]]
    assert "program" not in h


def test_build_header_tolerates_missing_profile():
    h = build_header(_ns(profile=None), SimpleNamespace(step_mode="statement"))
    assert h["language"] == "python"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_recorder.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'tdb.session.recorder'`

- [ ] **Step 3: Write the implementation**

```python
# src/tdb/session/recorder.py
"""Session recording: user debugging gestures as replayable JSON-RPC lines.

`--record FILE` captures each TUI gesture as one JSONL command record
(spec: docs/superpowers/specs/2026-07-31-record-replay-design.md).
Stdlib-only on purpose: the recorder must never entangle app/controller
imports, and a write failure must never take the debug session down.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Callable


class SessionRecorder:
    """Appends one flushed JSON line per recorded gesture."""

    def __init__(self, path: str, header: dict) -> None:
        self.on_error: Callable[[str], None] | None = None
        self._file = open(path, "w", encoding="utf-8")
        self._t0 = time.monotonic()
        self.active = True
        self._write_line(header)

    def _write_line(self, obj: dict) -> None:
        self._file.write(json.dumps(obj) + "\n")
        self._file.flush()

    def record(self, action: str, params: list) -> None:
        if not self.active:
            return
        try:
            self._write_line(
                {
                    "t": round(time.monotonic() - self._t0, 3),
                    "action": action,
                    "params": params,
                }
            )
        except (OSError, ValueError) as e:
            # ValueError covers writes to a closed file object. Degrade to
            # inert: the debug session must survive a dead recording.
            self.active = False
            try:
                self._file.close()
            except OSError:
                pass
            if self.on_error is not None:
                self.on_error(f"Recording stopped ({e}); session continues")

    def close(self) -> None:
        if self.active:
            self.active = False
            try:
                self._file.close()
            except OSError:
                pass


class NullRecorder:
    """No-op twin so gesture hooks never need an `if recording` guard."""

    def __init__(self) -> None:
        self.active = False
        self.on_error: Callable[[str], None] | None = None

    def record(self, action: str, params: list) -> None:
        pass

    def close(self) -> None:
        pass


def build_header(args, config) -> dict:
    """Header line for a new recording, from the parsed CLI namespace.

    `args` is argparse output post `parse_args()` (profile resolved,
    program path absolute); `config` is the loaded TdbConfig.
    """
    header = {
        "tdb_recording": 1,
        "created": datetime.now().isoformat(timespec="seconds"),
        "language": args.profile.id if args.profile else "python",
        "adapter": args.adapter,
        "step_mode": config.step_mode,
        "no_just_my_code": args.no_just_my_code,
    }
    if args.attach_host:
        header.update(
            mode="remote-attach",
            host=args.attach_host,
            port=args.attach_port,
            path_mappings=[list(pm) for pm in (args.path_mappings or [])],
        )
    else:
        header.update(
            mode="launch",
            program=args.program,
            args=list(args.args or []),
            cwd=args.cwd or os.getcwd(),
            python=args.python,
        )
    return header
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_recorder.py -q --no-cov`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/tdb/session/recorder.py tests/unit/test_recorder.py
git commit -m "feat: SessionRecorder / NullRecorder / build_header

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `--record` CLI flag, validation, and TdbApp wiring

**Files:**
- Modify: `src/tdb/cli.py` (`build_parser`, `parse_args`, `_run_tui`)
- Modify: `src/tdb/app.py` (`TdbApp.__init__` signature ~line 188-206 and body ~207-280)
- Modify: `docs/superpowers/specs/2026-07-31-record-replay-design.md` (one sentence)
- Test: `tests/unit/test_cli_record_flags.py`

**Interfaces:**
- Consumes: `SessionRecorder`, `NullRecorder`, `build_header` from `tdb.session.recorder` (Task 1 signatures).
- Produces: `TdbApp.__init__(..., recorder=None)` → `self.recorder` (a `SessionRecorder`/`NullRecorder`/duck-typed capture object with `.record/.close/.active/.on_error`). Every later hook task calls `self.recorder.record(...)`. `args.record` (str|None) exists on the parsed namespace.

**Context for the implementer:** `build_parser()` is at `src/tdb/cli.py:17` (flags are `parser.add_argument` calls through ~line 205). `parse_args()` is at line 494; it short-circuits for `--doc/--doc-text/--post-mortem/--mcp` at line 504. `_run_tui` is at line 671 and constructs `TdbApp(...)` then `app.run()`. Existing flags on the namespace: `args.headless` (`--headless`), `args.server` (`--server`), `args.post_mortem`, `args.mcp`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli_record_flags.py
"""--record flag parsing/validation and TdbApp recorder wiring."""

import pytest

from tdb.app import TdbApp
from tdb.cli import parse_args
from tdb.persist import TdbConfig
from tdb.session.recorder import NullRecorder


def test_record_flag_parses(tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    args = parse_args(["--record", str(tmp_path / "s.jsonl"), str(prog)])
    assert args.record == str(tmp_path / "s.jsonl")


def test_record_default_is_none(tmp_path):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    assert parse_args([str(prog)]).record is None


@pytest.mark.parametrize("conflict", ["--headless", "--server"])
def test_record_rejected_with_server_modes(tmp_path, conflict, capsys):
    prog = tmp_path / "p.py"
    prog.write_text("x = 1\n")
    with pytest.raises(SystemExit):
        parse_args(["--record", "out.jsonl", conflict, str(prog)])
    assert "--record" in capsys.readouterr().err


def test_record_rejected_with_post_mortem(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--record", "out.jsonl", "--post-mortem", "snap.json"])
    assert "--record" in capsys.readouterr().err


async def test_app_defaults_to_null_recorder():
    app = TdbApp(program="", config=TdbConfig())
    assert isinstance(app.recorder, NullRecorder)


async def test_app_accepts_recorder_and_wires_on_error():
    class Cap:
        def __init__(self):
            self.active = True
            self.on_error = None

        def record(self, action, params):
            pass

        def close(self):
            pass

    cap = Cap()
    app = TdbApp(program="", config=TdbConfig(), recorder=cap)
    assert app.recorder is cap
    assert cap.on_error is not None  # app installed its notify callback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_cli_record_flags.py -q --no-cov`
Expected: FAIL — `error: unrecognized arguments: --record` (SystemExit in first test) and `AttributeError: 'TdbApp' object has no attribute 'recorder'`.

- [ ] **Step 3: Implement**

3a. In `build_parser()` (add near the `--server`/`--headless` group, ~line 150):

```python
    parser.add_argument(
        "--record",
        metavar="FILE",
        default=None,
        help="Record debugging actions (breakpoints, stepping, evaluate, "
        "stack/variable inspection) to FILE as JSON-RPC commands "
        "replayable with --replay or against `tdb --server`.",
    )
```

3b. In `parse_args()` immediately after `_apply_flag_implications(args)` (line 502):

```python
    if args.record and (args.headless or args.server or args.post_mortem or args.mcp):
        parser.error(
            "--record captures an interactive TUI session; it cannot be "
            "combined with --server, --headless, --post-mortem, or --mcp"
        )
```

3c. In `_run_tui()` (line 671), before the `TdbApp(...)` construction, create the recorder; pass it as `recorder=recorder`; close it after `app.run()`:

```python
    from tdb.session.recorder import NullRecorder, SessionRecorder, build_header

    if args.record:
        try:
            recorder = SessionRecorder(args.record, build_header(args, config))
        except OSError as e:
            print(f"tdb: cannot write recording to {args.record}: {e}", file=sys.stderr)
            sys.exit(2)
    else:
        recorder = NullRecorder()
```

and add `recorder=recorder,` to the `TdbApp(` kwargs, and after `app.run()` add `recorder.close()`.

3d. In `TdbApp.__init__` (app.py:188): add parameter `recorder: object | None = None,` at the end of the signature (after `profile`), and in the body (near `self._profile = profile`, line 226):

```python
        from tdb.session.recorder import NullRecorder

        self.recorder = recorder if recorder is not None else NullRecorder()
        self.recorder.on_error = lambda msg: self.notify(
            msg, title="Recording", severity="error"
        )
```

3e. In the spec file, § File format, extend the remote-attach header sentence to read:

```
Header, remote attach: `"mode": "remote-attach"` with `"host"`, `"port"`,
and `"path_mappings"` (list of `[local, remote]` pairs) in place of
`program`/`args`/`cwd`/`python`.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_cli_record_flags.py tests/unit/test_recorder.py -q --no-cov`
Expected: all pass. Also run `uv run pytest tests/unit -q --no-cov` — no regressions (cli parse tests exist elsewhere).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/cli.py src/tdb/app.py tests/unit/test_cli_record_flags.py docs/superpowers/specs/2026-07-31-record-replay-design.md
git commit -m "feat: --record flag, validation, recorder wiring into TdbApp

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Record stepping, continue, pause, stack nav, restart, quit

**Files:**
- Modify: `src/tdb/app.py` — `on_code_view_debug_action` (~line 828), `_navigate_stack` (~line 875), `_restart_session` (~line 522), `action_quit_debugger` (~line 1353)
- Create: `tests/unit/record_helpers.py`
- Test: `tests/unit/test_record_hooks_stepping.py`

**Interfaces:**
- Consumes: `TdbApp.recorder` (Task 2). `CodeView.DebugAction(action: str)` message (actions: `"continue_"`, `"step_over"`, `"step_in"`, `"step_out"`, `"pause"`, `"stack_up"`, `"stack_down"`, `"restart"`, `"quit"`).
- Produces: `tests/unit/record_helpers.py::CaptureRecorder` — `.records: list[tuple[str, list]]`, `.active = True`, `.on_error`, `.record()`, `.close()` — reused by Tasks 4 and 5.

**Recording rules (from spec):** gestures map to `continue`/`next`/`step_in`/`step_out`/`pause`; stack navigation records only when the move succeeded (boundary no-ops are not gestures worth replaying — replaying them would error); `restart` is recorded in `_restart_session` only when `new_program is None` (File > Open is not recorded — notify instead when a recording is active); `quit` is recorded once in `action_quit_debugger` (both the q-confirm and Ctrl+Q paths land there, `_is_quitting`-guarded).

- [ ] **Step 1: Write the helper and failing tests**

```python
# tests/unit/record_helpers.py
"""Shared capture recorder for gesture-hook tests."""


class CaptureRecorder:
    def __init__(self):
        self.records: list[tuple[str, list]] = []
        self.active = True
        self.on_error = None

    def record(self, action, params):
        self.records.append((action, list(params)))

    def close(self):
        pass
```

```python
# tests/unit/test_record_hooks_stepping.py
"""Stepping/continue/pause/stack/restart/quit gestures produce records."""

import pytest

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.widgets.code_view import CodeView

from tests.unit.record_helpers import CaptureRecorder


async def _noop(*a, **k):
    return None


@pytest.fixture
async def app_cap(monkeypatch):
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
        for name in ("continue_", "step_over", "step_in", "step_out"):
            monkeypatch.setattr(app.controller, name, _noop)
        yield app, cap, pilot


@pytest.mark.parametrize(
    "gesture,expected",
    [
        ("continue_", "continue"),
        ("step_over", "next"),
        ("step_in", "step_in"),
        ("step_out", "step_out"),
    ],
)
async def test_step_gestures_record(app_cap, gesture, expected):
    app, cap, _ = app_cap
    await app.on_code_view_debug_action(CodeView.DebugAction(gesture))
    assert cap.records == [(expected, [])]


async def test_pause_records(app_cap, monkeypatch):
    app, cap, _ = app_cap

    async def fake_pause(*a, **k):
        return True

    monkeypatch.setattr(app.controller, "pause", fake_pause)
    await app.on_code_view_debug_action(CodeView.DebugAction("pause"))
    assert cap.records == [("pause", [])]


async def test_stack_nav_records_only_on_success(app_cap, monkeypatch):
    app, cap, _ = app_cap
    results = iter([True, False])

    async def fake_nav(up):
        return next(results)

    monkeypatch.setattr(app.controller, "navigate_stack", fake_nav)
    await app.on_code_view_debug_action(CodeView.DebugAction("stack_up"))
    await app.on_code_view_debug_action(CodeView.DebugAction("stack_up"))
    assert cap.records == [("stack_up", [])]  # boundary attempt not recorded


async def test_restart_gesture_records(app_cap, monkeypatch):
    app, cap, _ = app_cap
    # Run the REAL _restart_session (the hook lives at the top of it) but
    # stub the controller-heavy remainder so no session actually starts.
    monkeypatch.setattr(app.controller, "stop", _noop)
    monkeypatch.setattr(app, "_start_session", lambda: None)
    await app._restart_session()
    assert ("restart", []) in cap.records


async def test_file_open_restart_not_recorded_but_notifies(
    app_cap, monkeypatch, tmp_path
):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "stop", _noop)
    monkeypatch.setattr(app, "_start_session", lambda: None)
    notes = []
    monkeypatch.setattr(app, "notify", lambda *a, **k: notes.append(a))
    prog = tmp_path / "other.py"
    prog.write_text("x = 1\n")
    await app._restart_session(new_program=str(prog), start_immediately=False)
    assert ("restart", []) not in cap.records
    assert notes  # user warned the recording won't reflect File > Open


async def test_quit_records_once(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "stop", _noop)
    await app.action_quit_debugger()
    await app.action_quit_debugger()  # second press: _is_quitting guard
    assert cap.records.count(("quit", [])) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_record_hooks_stepping.py -q --no-cov`
Expected: FAIL — assertions find `cap.records == []` (no hooks yet). If a test *errors* instead (fixture/monkeypatch issue), fix the test until it fails on the assertion.

- [ ] **Step 3: Implement the hooks**

3a. `on_code_view_debug_action` (app.py:828). In the `stack_up`/`stack_down` branch nothing changes here (recording happens inside `_navigate_stack`). In the `pause` branch, add before `paused = await self.controller.pause()`:

```python
                self.recorder.record("pause", [])
```

In the handler-map branch, add before `await handler()`:

```python
            if handler:
                self.recorder.record(
                    {
                        "continue_": "continue",
                        "step_over": "next",
                        "step_in": "step_in",
                        "step_out": "step_out",
                    }[message.action],
                    [],
                )
                await handler()
```

3b. `_navigate_stack` (app.py:875) — capture the result and record on success:

```python
    async def _navigate_stack(self, up: bool) -> None:
        """Move to the next/previous frame in the call stack."""
        try:
            moved = await self.controller.navigate_stack(up)
            if moved:
                self.recorder.record("stack_up" if up else "stack_down", [])
        except Exception:
            log.exception("Error navigating stack")
        self._update_ui_state()
```

3c. `_restart_session` (app.py:522) — first lines of the method body:

```python
        if new_program is None:
            self.recorder.record("restart", [])
        elif self.recorder.active:
            self.notify(
                "File > Open is not captured in the recording; a replay "
                "will use the originally recorded program",
                title="Recording",
                severity="warning",
            )
```

3d. `action_quit_debugger` (app.py:1353) — immediately after `self._is_quitting = True`:

```python
        self.recorder.record("quit", [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_record_hooks_stepping.py tests/unit -q --no-cov`
Expected: new tests pass; zero regressions in the unit suite.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/app.py tests/unit/record_helpers.py tests/unit/test_record_hooks_stepping.py
git commit -m "feat: record stepping/continue/pause/stack/restart/quit gestures

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Record breakpoint gestures and the session-start dump

**Files:**
- Modify: `src/tdb/app.py` — `on_code_view_breakpoint_toggled` (~741), `on_tdb_app__apply_breakpoint_condition` (~801), `on_breakpoint_view_clear_all_requested` (~1014), `on_breakpoint_view_breakpoint_delete_requested` (~1024), `on_code_view_run_to_cursor` (~943), `on_mount`'s breakpoint-install block (~344, right after `install_cli_breakpoints`)
- Test: `tests/unit/test_record_hooks_breakpoints.py`

**Interfaces:**
- Consumes: `TdbApp.recorder` (Task 2), `CaptureRecorder` from `tests/unit/record_helpers.py` (Task 3). Controller facts: `controller.state.breakpoints: dict[str, list[SourceBreakpoint]]` (`SourceBreakpoint` has `.line`, `.condition`, `.hit_condition` — import from `tdb.dap.types`); `controller.toggle_breakpoint(path, line)`; `controller.add_breakpoint` updates-in-place for an existing line (controller.py:588).
- Produces: recording behavior only.

**Recording rules (from spec):** toggle → `set_breakpoint`/`remove_breakpoint` `["path:line"]` depending on the post-toggle state; condition modal → `set_breakpoint ["path:line", condition-or-"", hit-or-""]` (the RPC handler treats `""` as absent — handlers.py:332-333); Breakpoint-view delete → `remove_breakpoint`; Clear-all → one `remove_breakpoint` per existing breakpoint (recorded before clearing); enable/disable toggles → NOT recorded; run-to-cursor → `set_breakpoint` + `continue` + `remove_breakpoint`, skipping set/remove when a breakpoint already existed at that line (mirrors `controller.run_to_cursor`'s own `had_bp` logic, controller.py:468-496); at session start, every effective breakpoint (persisted + CLI `-k`/`-t`) is dumped as `set_breakpoint` records, followed by one `continue` record when the session will not stop on entry (so a replay — which always launches stopped-at-entry — reproduces the auto-start).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_record_hooks_breakpoints.py
"""Breakpoint gestures and the session-start dump produce records."""

import pytest

from tdb.app import TdbApp
from tdb.dap.types import SourceBreakpoint
from tdb.persist import TdbConfig
from tdb.widgets.breakpoint_view import BreakpointView
from tdb.widgets.code_view import CodeView

from tests.unit.record_helpers import CaptureRecorder


async def _noop(*a, **k):
    return None


@pytest.fixture
async def app_cap(monkeypatch):
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
        cap.records.clear()  # drop anything from mount
        yield app, cap, pilot


async def test_toggle_on_records_set(app_cap, monkeypatch):
    app, cap, _ = app_cap

    async def fake_toggle(path, line):
        app.controller.state.breakpoints[path] = [SourceBreakpoint(line=line)]

    monkeypatch.setattr(app.controller, "toggle_breakpoint", fake_toggle)
    await app.on_code_view_breakpoint_toggled(CodeView.BreakpointToggled("/x.py", 7))
    assert cap.records == [("set_breakpoint", ["/x.py:7"])]


async def test_toggle_off_records_remove(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.breakpoints["/x.py"] = [SourceBreakpoint(line=7)]

    async def fake_toggle(path, line):
        app.controller.state.breakpoints[path] = []

    monkeypatch.setattr(app.controller, "toggle_breakpoint", fake_toggle)
    await app.on_code_view_breakpoint_toggled(CodeView.BreakpointToggled("/x.py", 7))
    assert cap.records == [("remove_breakpoint", ["/x.py:7"])]


async def test_condition_apply_records_set_with_condition(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "set_breakpoint_condition", _noop)
    await app.on_tdb_app__apply_breakpoint_condition(
        app._ApplyBreakpointCondition("/x.py", 7, "n > 3", None)
    )
    assert cap.records == [("set_breakpoint", ["/x.py:7", "n > 3", ""])]


async def test_delete_from_breakpoint_view_records_remove(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "remove_breakpoint", _noop)
    await app.on_breakpoint_view_breakpoint_delete_requested(
        BreakpointView.BreakpointDeleteRequested("/x.py", 7)
    )
    assert cap.records == [("remove_breakpoint", ["/x.py:7"])]


async def test_clear_all_records_remove_per_breakpoint(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.breakpoints = {
        "/x.py": [SourceBreakpoint(line=3), SourceBreakpoint(line=9)],
        "/y.py": [SourceBreakpoint(line=1)],
    }
    monkeypatch.setattr(app.controller, "clear_all_breakpoints", _noop)
    await app.on_breakpoint_view_clear_all_requested(BreakpointView.ClearAllRequested())
    assert sorted(cap.records) == sorted(
        [
            ("remove_breakpoint", ["/x.py:3"]),
            ("remove_breakpoint", ["/x.py:9"]),
            ("remove_breakpoint", ["/y.py:1"]),
        ]
    )


async def test_disable_all_not_recorded(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "disable_all_breakpoints", _noop)
    monkeypatch.setattr(app.controller, "enable_all_breakpoints", _noop)
    await app.on_breakpoint_view_disable_all_requested(
        BreakpointView.DisableAllRequested()
    )
    assert cap.records == []


async def test_run_to_cursor_records_triple(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "run_to_cursor", _noop)
    await app.on_code_view_run_to_cursor(CodeView.RunToCursor("/x.py", 20))
    assert cap.records == [
        ("set_breakpoint", ["/x.py:20"]),
        ("continue", []),
        ("remove_breakpoint", ["/x.py:20"]),
    ]


async def test_run_to_cursor_on_existing_bp_records_continue_only(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.breakpoints["/x.py"] = [SourceBreakpoint(line=20)]
    monkeypatch.setattr(app.controller, "run_to_cursor", _noop)
    await app.on_code_view_run_to_cursor(CodeView.RunToCursor("/x.py", 20))
    assert cap.records == [("continue", [])]


async def test_mount_dumps_initial_breakpoints_and_autocontinue(tmp_path):
    """-k/-t/persisted breakpoints become set_breakpoint records at start;
    a no-entry-stop session appends the auto-start continue record."""
    cap = CaptureRecorder()
    app = TdbApp(
        program="",
        config=TdbConfig(),
        stop_on_entry=False,
        cli_breakpoints=[("/x.py", 5, False)],
        recorder=cap,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
    assert ("set_breakpoint", ["/x.py:5"]) in cap.records
    assert ("continue", []) in cap.records
    assert cap.records.index(("set_breakpoint", ["/x.py:5"])) < cap.records.index(
        ("continue", [])
    )


async def test_mount_no_autocontinue_when_stopping_on_entry():
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), stop_on_entry=True, recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
    assert ("continue", []) not in cap.records
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_record_hooks_breakpoints.py -q --no-cov`
Expected: FAIL on assertions (`cap.records == []` where records are expected). Fix any test *errors* (message constructor signatures, etc.) until failures are pure assertions.

- [ ] **Step 3: Implement the hooks**

3a. `on_code_view_breakpoint_toggled` (app.py:741) — after `await self.controller.toggle_breakpoint(...)` and before `self.post_message(...)`:

```python
now_set = any(
    bp.line == message.line
    for bp in self.controller.state.breakpoints.get(message.source_path, [])
)
self.recorder.record(
    "set_breakpoint" if now_set else "remove_breakpoint",
    [f"{message.source_path}:{message.line}"],
)
```

3b. `on_tdb_app__apply_breakpoint_condition` (app.py:801) — after the `await self.controller.set_breakpoint_condition(...)` call:

```python
            self.recorder.record(
                "set_breakpoint",
                [
                    f"{message.source_path}:{message.line}",
                    message.condition or "",
                    message.hit_condition or "",
                ],
            )
```

3c. `on_breakpoint_view_breakpoint_delete_requested` (app.py:1024) — after the `await self.controller.remove_breakpoint(...)` call:

```python
self.recorder.record("remove_breakpoint", [f"{message.source_path}:{message.line}"])
```

3d. `on_breakpoint_view_clear_all_requested` (app.py:1014) — before `await self.controller.clear_all_breakpoints()`:

```python
            for path, bps in self.controller.state.breakpoints.items():
                for bp in bps:
                    self.recorder.record("remove_breakpoint", [f"{path}:{bp.line}"])
```

3e. `on_code_view_run_to_cursor` (app.py:943) — before `await self.controller.run_to_cursor(...)`:

```python
had_bp = any(
    bp.line == message.line
    for bp in self.controller.state.breakpoints.get(message.source_path, [])
)
if not had_bp:
    self.recorder.record("set_breakpoint", [f"{message.source_path}:{message.line}"])
self.recorder.record("continue", [])
if not had_bp:
    self.recorder.record("remove_breakpoint", [f"{message.source_path}:{message.line}"])
```

3f. `on_mount` breakpoint block (app.py:~344) — immediately after `self.controller.state.install_cli_breakpoints(self._cli_breakpoints)`:

```python
        # Recording: dump every effective breakpoint (persisted + CLI) so
        # the recording is self-contained, then — for a session that will
        # NOT stop on entry (-k/-t/--no-stop-on-entry) — an explicit
        # `continue`: replay always launches stopped-at-entry so the
        # dumped breakpoints can be installed before the program runs.
        for _path, _bps in self.controller.state.breakpoints.items():
            for _bp in _bps:
                if _bp.condition or _bp.hit_condition:
                    self.recorder.record(
                        "set_breakpoint",
                        [
                            f"{_path}:{_bp.line}",
                            _bp.condition or "",
                            _bp.hit_condition or "",
                        ],
                    )
                else:
                    self.recorder.record("set_breakpoint", [f"{_path}:{_bp.line}"])
        if not self._stop_on_entry and self._attach_host is None:
            self.recorder.record("continue", [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_record_hooks_breakpoints.py tests/unit -q --no-cov`
Expected: new tests pass; zero unit regressions.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/app.py tests/unit/test_record_hooks_breakpoints.py
git commit -m "feat: record breakpoint gestures and session-start dump

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Record evaluate, frame selection, and variable expansion

**Files:**
- Modify: `src/tdb/app.py` — `on_evaluate_console_evaluate_requested` (~1103), `on_stack_view_frame_selected` (~950), `on_tdb_app_lazy_load_variables` (~1047)
- Test: `tests/unit/test_record_hooks_inspection.py`

**Interfaces:**
- Consumes: `TdbApp.recorder`, `CaptureRecorder` (Task 3). Facts: `state.stack_frames: list[StackFrame]` ordered top-first; `state.current_frame_id` resets to `frames[0].id` at each stop (`state.set_stack`, state.py:120-142); `navigate_stack(up=True)` moves to a HIGHER index (controller.py:680: "up = toward caller (higher index)"); `state.displayed_frames_are_synthetic` guards async-task synthetic stacks; `Variable.evaluate_name` (dap/types.py:125); `state.variables: dict[int, list[Variable]]`.
- Produces: recording behavior only.

**Recording rules (from spec):** Evaluate entry → `evaluate [expression]`; frame selection → |delta| × `stack_up` (target index higher) or `stack_down` (lower), computed BEFORE `select_frame` mutates `current_frame_id`, skipped entirely for synthetic stacks; variable expansion in the MAIN variable view (`message.source is None`, not the Processes modal) → `inspect [evaluate_name]` of the expanded variable, found by matching `variables_reference` across `state.variables` values; expansion without an `evaluate_name` records nothing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_record_hooks_inspection.py
"""Evaluate, frame-selection, and variable-expansion recording."""

import pytest

from tdb.app import TdbApp
from tdb.dap.types import Source, StackFrame, Variable
from tdb.persist import TdbConfig
from tdb.widgets.evaluate_console import EvaluateConsole
from tdb.widgets.stack_view import StackView
from tdb.widgets.variable_view import VariableView

from tests.unit.record_helpers import CaptureRecorder


async def _noop(*a, **k):
    return None


def _frames():
    return [
        StackFrame(id=101, name="inner", line=3, source=Source(path="/x.py")),
        StackFrame(id=102, name="mid", line=9, source=Source(path="/x.py")),
        StackFrame(id=103, name="outer", line=20, source=Source(path="/x.py")),
    ]


@pytest.fixture
async def app_cap(monkeypatch):
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
        cap.records.clear()
        yield app, cap, pilot


async def test_evaluate_entry_records(app_cap, monkeypatch):
    app, cap, _ = app_cap

    async def fake_eval(expr):
        return "42"

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    await app.on_evaluate_console_evaluate_requested(
        EvaluateConsole.EvaluateRequested("len(data)")
    )
    assert cap.records == [("evaluate", ["len(data)"])]


async def test_frame_click_down_the_stack_records_stack_ups(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.set_stack(_frames())  # current = id 101 (index 0)
    monkeypatch.setattr(app.controller, "select_frame", _noop)
    await app.on_stack_view_frame_selected(
        StackView.FrameSelected(103, None, 20)  # index 2: toward caller
    )
    assert cap.records == [("stack_up", []), ("stack_up", [])]


async def test_frame_click_back_toward_top_records_stack_downs(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.set_stack(_frames())
    app.controller.state.current_frame_id = 103  # user is at index 2
    monkeypatch.setattr(app.controller, "select_frame", _noop)
    await app.on_stack_view_frame_selected(StackView.FrameSelected(102, None, 9))
    assert cap.records == [("stack_down", [])]


async def test_frame_click_on_synthetic_stack_not_recorded(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.set_stack(_frames())
    app.controller.state.displayed_frames_are_synthetic = True
    monkeypatch.setattr(app.controller, "select_frame", _noop)
    await app.on_stack_view_frame_selected(StackView.FrameSelected(103, None, 20))
    assert cap.records == []


async def test_variable_expand_records_inspect_with_evaluate_name(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.variables = {
        5: [
            Variable(
                name="data",
                value="{...}",
                variables_reference=7,
                evaluate_name="data['x']",
            )
        ]
    }

    class FakeClient:
        async def variables(self, ref):
            return []

    monkeypatch.setattr(
        type(app.controller), "active_client", property(lambda self: FakeClient())
    )
    var_view = app.query_one("#variable-view", VariableView)
    await app.on_tdb_app_lazy_load_variables(app.LazyLoadVariables(7, var_view.root))
    assert cap.records == [("inspect", ["data['x']"])]


async def test_variable_expand_without_evaluate_name_records_nothing(
    app_cap, monkeypatch
):
    app, cap, _ = app_cap
    app.controller.state.variables = {
        5: [Variable(name="%h", value="HASH", variables_reference=7)]
    }

    class FakeClient:
        async def variables(self, ref):
            return []

    monkeypatch.setattr(
        type(app.controller), "active_client", property(lambda self: FakeClient())
    )
    var_view = app.query_one("#variable-view", VariableView)
    await app.on_tdb_app_lazy_load_variables(app.LazyLoadVariables(7, var_view.root))
    assert cap.records == []
```

Note for the implementer: if `Variable`'s constructor requires additional
positional fields (check `src/tdb/dap/types.py` around line 115-140),
supply them with neutral values in the tests — the assertions above are
what matters. If `StackFrame` requires `column`, pass `column=1`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_record_hooks_inspection.py -q --no-cov`
Expected: FAIL on assertions (no records). Repair constructor-signature errors until failures are assertions.

- [ ] **Step 3: Implement the hooks**

3a. `on_evaluate_console_evaluate_requested` (app.py:1103) — first line of the body:

```python
        self.recorder.record("evaluate", [message.expression])
```

3b. `on_stack_view_frame_selected` (app.py:950) — before `await self.controller.select_frame(message.frame_id)`:

```python
state = self.controller.state
if not state.displayed_frames_are_synthetic:
    frames = state.stack_frames
    old_idx = next(
        (i for i, f in enumerate(frames) if f.id == state.current_frame_id),
        None,
    )
    new_idx = next(
        (i for i, f in enumerate(frames) if f.id == message.frame_id),
        None,
    )
    if old_idx is not None and new_idx is not None and new_idx != old_idx:
        step = "stack_up" if new_idx > old_idx else "stack_down"
        for _ in range(abs(new_idx - old_idx)):
            self.recorder.record(step, [])
```

3c. `on_tdb_app_lazy_load_variables` (app.py:1047) — insert after the Processes-modal branch returns (i.e. right before the final `try: variables = await self.controller.active_client.variables(...)` block at ~line 1091):

```python
        # Recording: a main-view expansion is a user gesture; replay it as
        # `inspect` of the variable's evaluatable expression when the
        # adapter provided one (DAP evaluateName). Modal expansions and
        # evaluate_name-less variables (e.g. the perl adapter) are skipped.
        if source is None:
            expanded = next(
                (
                    v
                    for vars_ in self.controller.state.variables.values()
                    for v in vars_
                    if v.variables_reference == message.variables_reference
                ),
                None,
            )
            if expanded is not None and expanded.evaluate_name:
                self.recorder.record("inspect", [expanded.evaluate_name])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_record_hooks_inspection.py tests/unit -q --no-cov`
Expected: new tests pass; zero unit regressions.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/app.py tests/unit/test_record_hooks_inspection.py
git commit -m "feat: record evaluate, frame selection, and variable expansion

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Extract setup_headless_session from run_headless

**Files:**
- Modify: `src/tdb/server/runner.py`
- Test: existing suites only (behavior-preserving refactor)

**Interfaces:**
- Consumes: current `run_headless` body (runner.py:24-140).
- Produces (Task 8 relies on this exact signature):

```python
async def setup_headless_session(
    program: str | None,
    args: list[str] | None = None,
    cwd: str | None = None,
    stop_on_entry: bool = False,
    just_my_code: bool = True,
    python: str | None = None,
    cli_breakpoints: list[tuple[str, int, bool]] | None = None,
    attach_host: str | None = None,
    attach_port: int | None = None,
    path_mappings: list[tuple[str, str]] | None = None,
    profile: "LanguageProfile | None" = None,
    step_mode: str | None = None,
) -> tuple[DebugController, ServerEventHandler]:
```

- [ ] **Step 1: Refactor**

Move runner.py lines 48-115 (the `is_remote` check through `log.info("Debug session ready (headless)")`) into `setup_headless_session` with the signature above. Two changes from the moved code: `controller.step_mode = step_mode if step_mode is not None else load_config().step_mode`, and the function ends with `return controller, handler`. `run_headless` keeps its existing signature and becomes:

```python
async def run_headless(
    program: str | None,
    args: list[str] | None = None,
    cwd: str | None = None,
    stop_on_entry: bool = False,
    just_my_code: bool = True,
    python: str | None = None,
    port: int = 8150,
    host: str = "127.0.0.1",
    cli_breakpoints: list[tuple[str, int, bool]] | None = None,
    attach_host: str | None = None,
    attach_port: int | None = None,
    path_mappings: list[tuple[str, str]] | None = None,
    profile: "LanguageProfile | None" = None,
) -> None:
    controller, handler = await setup_headless_session(
        program,
        args=args,
        cwd=cwd,
        stop_on_entry=stop_on_entry,
        just_my_code=just_my_code,
        python=python,
        cli_breakpoints=cli_breakpoints,
        attach_host=attach_host,
        attach_port=attach_port,
        path_mappings=path_mappings,
        profile=profile,
    )
    # ... existing uvicorn block (lines 117-140) unchanged, except the
    # is_remote reference becomes: attach_host is not None and attach_port is not None
```

Keep the `sys.exit(2)` error paths exactly where they are (inside the moved code) — both callers are CLI-facing.

- [ ] **Step 2: Run the server-related tests**

Run: `uv run pytest tests/unit -q --no-cov` then `uv run pytest tests/integration -k "server or headless or rpc" -q --no-cov`
Expected: all pass (pure refactor). If no integration test matches the `-k`, run the full integration suite.

- [ ] **Step 3: Commit**

```bash
git add src/tdb/server/runner.py
git commit -m "refactor: extract setup_headless_session for in-process replay

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Recording loader and validation

**Files:**
- Create: `src/tdb/replay.py` (loader half)
- Test: `tests/unit/test_replay_loader.py`

**Interfaces:**
- Consumes: `RpcHandlers.ACTIONS: tuple[str, ...]` (class attribute, `src/tdb/server/handlers.py:92`) as the known-action set.
- Produces (Task 8 relies on these):
  - `Recording` dataclass: `.header: dict`, `.records: list[dict]`.
  - `RecordingError(Exception)` — message always starts `"<path>:<line>: "` for per-line problems, `"<path>: "` for whole-file problems.
  - `load_recording(path: str) -> Recording`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_replay_loader.py
"""load_recording validates everything before any process is launched."""

import pytest

from tdb.replay import Recording, RecordingError, load_recording

HEADER = (
    '{"tdb_recording": 1, "created": "2026-07-31T00:00:00", "mode": "launch",'
    ' "language": "python", "program": "/abs/p.py", "args": [], "cwd": "/abs",'
    ' "python": null, "adapter": null, "step_mode": "statement",'
    ' "no_just_my_code": false}'
)


def write(tmp_path, *lines):
    p = tmp_path / "rec.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def test_loads_valid_recording(tmp_path):
    path = write(
        tmp_path,
        HEADER,
        '{"t": 0.1, "action": "set_breakpoint", "params": ["/abs/p.py:3"]}',
        '{"t": 0.5, "action": "continue", "params": []}',
    )
    rec = load_recording(path)
    assert isinstance(rec, Recording)
    assert rec.header["mode"] == "launch"
    assert [r["action"] for r in rec.records] == ["set_breakpoint", "continue"]


def test_blank_lines_are_skipped(tmp_path):
    path = write(tmp_path, HEADER, "", '{"t": 1, "action": "quit", "params": []}')
    assert len(load_recording(path).records) == 1


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    with pytest.raises(RecordingError):
        load_recording(str(p))


def test_wrong_version_rejected_line1(tmp_path):
    path = write(tmp_path, '{"tdb_recording": 99, "mode": "launch"}')
    with pytest.raises(RecordingError, match=r":1:"):
        load_recording(path)


def test_unknown_mode_rejected(tmp_path):
    path = write(tmp_path, HEADER.replace('"launch"', '"teleport"'))
    with pytest.raises(RecordingError, match="mode"):
        load_recording(path)


def test_launch_header_missing_program_rejected(tmp_path):
    bad = HEADER.replace('"program": "/abs/p.py", ', "")
    path = write(tmp_path, bad)
    with pytest.raises(RecordingError, match="program"):
        load_recording(path)


def test_malformed_json_names_line(tmp_path):
    path = write(tmp_path, HEADER, "{not json")
    with pytest.raises(RecordingError, match=r":2:"):
        load_recording(path)


def test_unknown_action_names_line(tmp_path):
    path = write(tmp_path, HEADER, '{"t": 1, "action": "teleport", "params": []}')
    with pytest.raises(RecordingError, match=r":2:.*teleport"):
        load_recording(path)


def test_missing_t_rejected(tmp_path):
    path = write(tmp_path, HEADER, '{"action": "continue", "params": []}')
    with pytest.raises(RecordingError, match=r":2:"):
        load_recording(path)


def test_non_list_params_rejected(tmp_path):
    path = write(tmp_path, HEADER, '{"t": 1, "action": "continue", "params": "x"}')
    with pytest.raises(RecordingError, match=r":2:"):
        load_recording(path)


def test_attach_header_requires_host_port(tmp_path):
    attach = (
        '{"tdb_recording": 1, "mode": "remote-attach", "language": "python",'
        ' "host": "127.0.0.1", "step_mode": "statement"}'
    )
    with pytest.raises(RecordingError, match="port"):
        load_recording(write(tmp_path, attach))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_replay_loader.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: No module named 'tdb.replay'`

- [ ] **Step 3: Implement the loader**

```python
# src/tdb/replay.py
"""Replay a --record session file through the headless RPC dispatch.

Spec: docs/superpowers/specs/2026-07-31-record-replay-design.md.
The file format is JSONL: line 1 is the header, every other line is one
{"t", "action", "params"} command identical in shape to a POST /rpc body.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class RecordingError(Exception):
    pass


@dataclass
class Recording:
    header: dict
    records: list[dict]


_LAUNCH_REQUIRED = ("language", "program", "cwd")
_ATTACH_REQUIRED = ("language", "host", "port")


def load_recording(path: str) -> Recording:
    from tdb.server.handlers import RpcHandlers

    known_actions = set(RpcHandlers.ACTIONS)
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if not lines or not lines[0].strip():
        raise RecordingError(f"{path}: empty file (not a tdb recording)")

    def parse(lineno: int, text: str) -> dict:
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise RecordingError(f"{path}:{lineno}: invalid JSON: {e}") from e
        if not isinstance(obj, dict):
            raise RecordingError(f"{path}:{lineno}: expected a JSON object")
        return obj

    header = parse(1, lines[0])
    if header.get("tdb_recording") != 1:
        raise RecordingError(
            f"{path}:1: not a tdb recording, or unsupported version "
            f"(tdb_recording={header.get('tdb_recording')!r}; this tdb reads v1)"
        )
    mode = header.get("mode")
    if mode not in ("launch", "remote-attach"):
        raise RecordingError(f"{path}:1: unknown mode {mode!r}")
    required = _LAUNCH_REQUIRED if mode == "launch" else _ATTACH_REQUIRED
    for key in required:
        if header.get(key) is None:
            raise RecordingError(f"{path}:1: header is missing {key!r}")

    records: list[dict] = []
    for lineno, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        rec = parse(lineno, line)
        if not isinstance(rec.get("t"), (int, float)):
            raise RecordingError(f"{path}:{lineno}: missing or non-numeric 't'")
        if rec.get("action") not in known_actions:
            raise RecordingError(
                f"{path}:{lineno}: unknown action {rec.get('action')!r} "
                "(recording from a newer tdb?)"
            )
        if not isinstance(rec.get("params"), list):
            raise RecordingError(f"{path}:{lineno}: 'params' must be a list")
        records.append(rec)
    return Recording(header=header, records=records)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_replay_loader.py -q --no-cov`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add src/tdb/replay.py tests/unit/test_replay_loader.py
git commit -m "feat: recording loader with line-numbered validation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Replay engine, transcript output, CLI dispatch

**Files:**
- Modify: `src/tdb/replay.py` (add `run_replay`, `replay_main`, `_profile_from_header`, transcript printers)
- Modify: `src/tdb/cli.py` (`--replay`/`--timing`/`--replay-timeout` flags, validation, `main()` dispatch, `_run_replay`)
- Test: `tests/integration/test_replay_session.py`

**Interfaces:**
- Consumes: `Recording`/`load_recording`/`RecordingError` (Task 7); `setup_headless_session` (Task 6 signature); `RpcHandlers(ControllerRef(controller), handler)` and `.dispatch_table() -> dict[str, Callable[[list], Awaitable[RpcResponse]]]` (handlers.py); `RpcResponse.success: bool`, `.value: str` (rpc_types.py); `ServerEventHandler.drain_output() -> str` (event_handler.py:108).
- Produces: `run_replay(recording, timing=False, replay_timeout=30.0, echo=print) -> int` (error count) and `replay_main(path, timing, replay_timeout) -> None` (calls `sys.exit`).

**Semantics (from spec):** replay always launches stopped-at-entry (recordings carry their own `continue` when the original didn't entry-stop — Task 4); blocking actions (`next`, `step_in`, `step_out`, `continue`, `wait_for_stop`) with empty recorded params get `[replay_timeout]` injected; `--timing` sleeps recorded deltas; verbatim RPC value in transcript; program output drained after every command; implicit teardown at EOF when no `quit` record; summary line `N commands, M errors`; exit 0 iff no errors.

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_replay_session.py
"""End-to-end: a recording file drives a real debugpy session in-process."""

import json

import pytest

from tdb.replay import load_recording, run_replay

TOY = """\
x = 1
y = 2
z = x + y
print("z =", z)
"""


def make_recording(tmp_path, records, *, stop_on_entry_continue=False):
    prog = tmp_path / "toy.py"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-07-31T00:00:00",
        "mode": "launch",
        "language": "python",
        "program": str(prog),
        "args": [],
        "cwd": str(tmp_path),
        "python": None,
        "adapter": None,
        "step_mode": "line",
        "no_just_my_code": False,
    }
    lines = [json.dumps(header)]
    t = 0.0
    for action, params in records:
        t += 0.1
        lines.append(json.dumps({"t": round(t, 3), "action": action, "params": params}))
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return str(path), str(prog)


async def test_replay_breakpoint_evaluate_quit(tmp_path):
    path, prog = make_recording(
        tmp_path,
        [
            ("set_breakpoint", [f"{tmp_path}/toy.py:3"]),
            ("continue", []),
            ("evaluate", ["x + y"]),
            ("quit", []),
        ],
    )
    out: list[str] = []
    errors = await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert "4 commands, 0 errors" in text
    assert "ok: 3" in text  # evaluate result, verbatim
    assert "set_breakpoint" in text  # command header lines present


async def test_replay_reports_failed_command_and_continues(tmp_path):
    path, _ = make_recording(
        tmp_path,
        [
            ("set_breakpoint", ["not-a-file-line-spec"]),  # ERROR
            ("continue", []),  # still runs
            ("quit", []),
        ],
    )
    out: list[str] = []
    errors = await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert errors == 1
    assert "ERROR:" in text
    assert "3 commands, 1 errors" in text


async def test_replay_interleaves_program_output(tmp_path):
    path, _ = make_recording(
        tmp_path,
        [("continue", []), ("quit", [])],
    )
    out: list[str] = []
    await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert "z = 3" in text  # debuggee stdout surfaced in the transcript


async def test_replay_timing_sleeps_recorded_deltas(tmp_path):
    import time

    path, _ = make_recording(tmp_path, [("quit", [])])
    # rewrite the single record with t=0.5 to force a measurable delay
    lines = open(path).read().splitlines()
    rec = json.loads(lines[1])
    rec["t"] = 0.5
    open(path, "w").write(lines[0] + "\n" + json.dumps(rec) + "\n")
    out: list[str] = []
    t0 = time.monotonic()
    await run_replay(load_recording(path), timing=True, echo=out.append)
    assert time.monotonic() - t0 >= 0.5


async def test_condition_reset_updates_in_place(tmp_path):
    """Spec § limitations: re-recording a breakpoint with a condition
    (the condition-modal gesture) must yield ONE breakpoint on replay —
    controller.add_breakpoint updates in place (controller.py:588)."""
    path, prog = make_recording(
        tmp_path,
        [
            ("set_breakpoint", [f"{tmp_path}/toy.py:3"]),
            ("set_breakpoint", [f"{tmp_path}/toy.py:3", "x == 1", ""]),
            ("list_breakpoints", []),
            ("quit", []),
        ],
    )
    out: list[str] = []
    errors = await run_replay(load_recording(path), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert text.count("toy.py:3") >= 1
    # exactly one breakpoint listed for line 3 (update, not duplicate):
    # the list_breakpoints block contains a single 'toy.py:3' entry line
    listing = [l for l in out if "toy.py:3" in l and "condition" in l]
    assert len(listing) == 1


async def test_recorder_file_round_trips(tmp_path):
    """Round-trip property (spec § Testing): a file written by the REAL
    SessionRecorder replays to the recorded stop-line sequence."""
    from tdb.session.recorder import SessionRecorder

    prog = tmp_path / "toy.py"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-07-31T00:00:00",
        "mode": "launch",
        "language": "python",
        "program": str(prog),
        "args": [],
        "cwd": str(tmp_path),
        "python": None,
        "adapter": None,
        "step_mode": "line",
        "no_just_my_code": False,
    }
    rec_path = tmp_path / "rt.jsonl"
    rec = SessionRecorder(str(rec_path), header)
    rec.record("set_breakpoint", [f"{prog}:3"])
    rec.record("continue", [])  # -> stops at toy.py:3
    rec.record("next", [])  # -> stops at toy.py:4
    rec.record("quit", [])
    rec.close()

    out: list[str] = []
    errors = await run_replay(load_recording(str(rec_path)), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    # Stop-location responses appear in recorded order: line 3, then 4.
    assert text.index("toy.py:3") < text.index("toy.py:4")


def test_replay_cli_flags_parse(tmp_path):
    from tdb.cli import parse_args

    args = parse_args(["--replay", "session.jsonl", "--timing"])
    assert args.replay == "session.jsonl"
    assert args.timing is True
    assert args.replay_timeout == 30.0


def test_replay_rejects_program_argument(capsys):
    from tdb.cli import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--replay", "s.jsonl", "prog.py"])
    assert "--replay" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_replay_session.py -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'run_replay'` and `--replay` unrecognized.

- [ ] **Step 3: Implement**

3a. Append to `src/tdb/replay.py`:

```python
import asyncio
import sys

BLOCKING_ACTIONS = {"next", "step_in", "step_out", "continue", "wait_for_stop"}


def _profile_from_header(header: dict):
    from tdb.languages import registry
    from tdb.persist import load_config

    config = load_config()
    lang = header.get("language") or "python"
    adapter = header.get("adapter") or config.default_adapters.get(lang)
    return registry.resolve(lang, adapter=adapter, adapter_paths=config.adapters)


def _print_command(echo, rec: dict, success: bool, value: str) -> None:
    echo(f"[{rec['t']:8.3f}] {rec['action']} {json.dumps(rec['params'])}")
    prefix = "ok:" if success else "ERROR:"
    lines = (value or "").splitlines() or [""]
    echo(f"          {prefix} {lines[0]}".rstrip())
    for line in lines[1:]:
        echo(f"          {line}")


def _print_program_output(echo, text: str) -> None:
    echo("          --- program output ---")
    for line in text.splitlines():
        echo(f"          {line}")
    echo("          ----------------------")


async def run_replay(
    recording: Recording,
    timing: bool = False,
    replay_timeout: float = 30.0,
    echo=print,
) -> int:
    """Feed every record through the RPC dispatch table. Returns the
    number of failed commands (0 == clean replay)."""
    from tdb.server.handlers import ControllerRef, RpcHandlers
    from tdb.server.runner import setup_headless_session

    h = recording.header
    if h["mode"] == "launch":
        controller, handler = await setup_headless_session(
            program=h["program"],
            args=list(h.get("args") or []),
            cwd=h["cwd"],
            # Always park at entry: the recording's own records install
            # breakpoints and (for originally non-entry-stop sessions)
            # carry the explicit `continue` that starts the program.
            stop_on_entry=True,
            just_my_code=not h.get("no_just_my_code", False),
            python=h.get("python"),
            profile=_profile_from_header(h),
            step_mode=h.get("step_mode"),
        )
    else:
        controller, handler = await setup_headless_session(
            program=None,
            attach_host=h["host"],
            attach_port=h["port"],
            path_mappings=[tuple(pm) for pm in (h.get("path_mappings") or [])] or None,
            profile=_profile_from_header(h),
            step_mode=h.get("step_mode"),
        )

    handlers = RpcHandlers(ControllerRef(controller), handler)
    table = handlers.dispatch_table()
    errors = 0
    prev_t = 0.0
    saw_quit = False
    try:
        for rec in recording.records:
            if timing and rec["t"] > prev_t:
                await asyncio.sleep(rec["t"] - prev_t)
            prev_t = rec["t"]
            params = list(rec["params"])
            if rec["action"] in BLOCKING_ACTIONS and not params:
                params = [replay_timeout]
            resp = await table[rec["action"]](params)
            _print_command(echo, rec, resp.success, resp.value)
            if not resp.success:
                errors += 1
            if rec["action"] == "quit":
                saw_quit = True
            pending = handler.drain_output()
            if pending:
                _print_program_output(echo, pending)
    finally:
        if not saw_quit:
            try:
                await controller.stop()
            except Exception:
                pass
    echo(f"{len(recording.records)} commands, {errors} errors")
    return errors


def replay_main(path: str, timing: bool, replay_timeout: float) -> None:
    try:
        recording = load_recording(path)
    except (OSError, RecordingError) as e:
        print(f"tdb: {e}", file=sys.stderr)
        sys.exit(2)
    errors = asyncio.run(
        run_replay(recording, timing=timing, replay_timeout=replay_timeout)
    )
    sys.exit(0 if errors == 0 else 1)
```

3b. `src/tdb/cli.py` — in `build_parser()` next to `--record`:

```python
    parser.add_argument(
        "--replay",
        metavar="FILE",
        default=None,
        help="Replay a --record session headless (no TUI): launches the "
        "recorded program and feeds each recorded command through the "
        "RPC dispatch, printing a transcript.",
    )
    parser.add_argument(
        "--timing",
        action="store_true",
        help="With --replay: reproduce the recorded pacing between commands.",
    )
    parser.add_argument(
        "--replay-timeout",
        type=float,
        default=30.0,
        metavar="S",
        help="With --replay: per-command stop-wait timeout (default 30).",
    )
```

In `parse_args()` right after the `--record` conflict check (Task 2's 3b):

```python
if args.replay:
    if args.program:
        parser.error(
            "--replay takes no program argument (the recording "
            "header supplies the program)"
        )
    if args.record or args.headless or args.server or args.post_mortem:
        parser.error(
            "--replay cannot be combined with --record, "
            "--server, --headless, or --post-mortem"
        )
    return args
```

(Place this BEFORE the line-504 short-circuit `if args.doc or ...` return so `--replay` skips launch validation the same way `--post-mortem` does. Note: `args.program` may not exist if the positional wasn't parsed as such — check with `getattr(args, "program", None)`.)

In `main()` add before the `elif args.headless:` branch:

```python
    elif args.replay:
        from tdb.replay import replay_main

        replay_main(args.replay, timing=args.timing, replay_timeout=args.replay_timeout)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_replay_session.py -q --no-cov`
Expected: all pass (real debugpy sessions; allow ~30 s).
Then: `uv run pytest tests/unit -q --no-cov` — zero regressions.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/replay.py src/tdb/cli.py tests/integration/test_replay_session.py
git commit -m "feat: tdb --replay engine with transcript, timing, exit codes

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Perl replay proof, README docs, full-suite gate

**Files:**
- Create: `tests/integration/test_replay_perl.py`
- Modify: `README.md` (new "Recording and replaying sessions" section)
- Test: full suites

**Interfaces:**
- Consumes: `load_recording`/`run_replay` (Tasks 7-8). Perl guard pattern from `tests/integration/test_perl_session_driver.py:11-15`.

- [ ] **Step 1: Write the failing perl replay test**

```python
# tests/integration/test_replay_perl.py
"""Replay is language-agnostic: a perl recording replays through the
perl DAP adapter."""

import json
import shutil
import subprocess

import pytest

from tdb.replay import load_recording, run_replay

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

TOY = """\
my $x = 1;
my $y = 2;
my $z = $x + $y;
print "z=$z\\n";
"""


async def test_perl_recording_replays(tmp_path):
    prog = tmp_path / "toy.pl"
    prog.write_text(TOY)
    header = {
        "tdb_recording": 1,
        "created": "2026-07-31T00:00:00",
        "mode": "launch",
        "language": "perl",
        "program": str(prog),
        "args": [],
        "cwd": str(tmp_path),
        "python": None,
        "adapter": None,
        "step_mode": "line",
        "no_just_my_code": False,
    }
    records = [
        {"t": 0.1, "action": "set_breakpoint", "params": [f"{prog}:3"]},
        {"t": 0.2, "action": "continue", "params": []},
        {"t": 0.3, "action": "evaluate", "params": ["$x + $y"]},
        {"t": 0.4, "action": "quit", "params": []},
    ]
    path = tmp_path / "perl.jsonl"
    path.write_text(
        "\n".join([json.dumps(header)] + [json.dumps(r) for r in records]) + "\n"
    )
    out: list[str] = []
    errors = await run_replay(load_recording(str(path)), echo=out.append)
    text = "\n".join(out)
    assert errors == 0
    assert "3" in text  # $x + $y evaluated through perl5db
```

- [ ] **Step 2: Run it to verify current behavior**

Run: `uv run pytest tests/integration/test_replay_perl.py -q --no-cov`
Expected: PASS if Tasks 6-8 were fully language-agnostic — that is the assertion this test exists to keep true. If it FAILS, the failure is a real bug in replay's profile resolution (`_profile_from_header`) or in `setup_headless_session`'s language handling: fix the product code (never special-case the test) and re-run.

- [ ] **Step 3: Write the README section**

Add after the existing `--server` / JSON-RPC documentation section, matching the README's heading style:

```markdown
## Recording and replaying sessions

`tdb --record session.jsonl prog.py` runs a normal TUI session and captures
your debugging actions — breakpoints (including `-k`/`-t` and persisted
ones), stepping, continue/pause, Evaluate-console entries, stack-frame
navigation, variable expansion, restart, quit — to `session.jsonl` as
JSON-RPC commands. Works with launch mode (any language) and `-r`
remote attach.

Replay it two ways:

- `tdb --replay session.jsonl` — one command: launches the recorded
  program headless, feeds every recorded command through the same RPC
  dispatch `tdb --server` uses, and prints a transcript (recorded time,
  command, verbatim result, interleaved program output). Exit code 0 iff
  every command succeeded. Add `--timing` to reproduce the original
  pacing, `--replay-timeout S` to bound each stop-wait (default 30 s).
- Against a live server: start `tdb --server prog.py`, then feed line 2
  onward of the file to `POST /rpc` — each line is already a valid
  request body:

      tail -n +2 session.jsonl | while read line; do
          curl -s -X POST -H 'Content-Type: application/json' \
               -d "$line" http://127.0.0.1:8150/rpc
      done

  (On Windows, an equivalent loop in Python: read the file, skip the
  first line, `requests.post` each remaining line.)

Not captured: pure viewing (scrolling, search, modals, thread/task
lists), breakpoint enable/disable toggles, variable expansions when the
adapter reports no `evaluateName` (currently the Perl adapter), and
File > Open program switches.
```

- [ ] **Step 4: Full-suite gate**

Run: `uv run pytest tests/unit -q --no-cov` and `uv run pytest tests/integration -q --no-cov`
Expected: all green. Fix regressions before committing.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_replay_perl.py README.md
git commit -m "feat: perl replay coverage and record/replay documentation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
