# `--run` Mode with Signal-Triggered TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `tdb --run PROG` executes the debuggee headless at (near) full speed; Ctrl-C or SIGUSR1 pauses it and opens the TUI at the paused line; quitting the TUI can detach back to headless running.

**Architecture:** A new `run_mode.py` owns a headless `DebugController` whose event handler is swappable. The run phase prints DAP output events to the terminal and waits on (exit | signal | stop). On signal it issues DAP `pause`, then runs `TdbApp` on the same asyncio loop in a new "adopted session" mode that reuses the live controller. Quit offers detach-&-resume (back to the run phase) or terminate.

**Tech Stack:** Python, textual (`App.run_async`, `run_test` pilot), DAP via existing `DebugController`/adapters, pytest + pytest-asyncio (`asyncio_mode = auto`).

**Spec:** `docs/superpowers/specs/2026-08-15-run-mode-signal-tui-design.md`

## Global Constraints

- Repo root: `/home/al/projects/tdbg/work`; all paths below are relative to it. Branch: `catch_breakpoint_signal`.
- Python deps are installed with `uv pip install`, never bare `pip`.
- Run tests with `python -m pytest <path> -v` from the repo root.
- Cross-platform: no POSIX-only calls outside `os.name != "nt"` guards; SIGUSR1 is POSIX-only; Windows uses `signal.signal` for SIGINT (no `loop.add_signal_handler` on Proactor).
- The flag is `--run` — long form only, no short alias (`-r` is `--remote-attach`).
- When touching quit/exit behavior, audit ALL exit paths (q, Ctrl+Q, menu, code-view `quit` action) — project rule from CLAUDE.md.
- Commit after each task with a conventional-commit message; end commit messages with the project's Claude trailer lines (see repo git log for format).

---

### Task 1: `pause_while_running` capability flag

**Files:**
- Modify: `src/tdb/languages/base.py:165-179` (ProfileCapabilities)
- Modify: `src/tdb/languages/python.py:109-113`
- Modify: `src/tdb/languages/perl.py:101`
- Modify: `src/tdb/languages/bash.py:83`
- Test: `tests/unit/test_pause_while_running_capability.py`

**Interfaces:**
- Consumes: existing `ProfileCapabilities` dataclass (all fields have defaults).
- Produces: `ProfileCapabilities.pause_while_running: bool = False`; True on the python, perl, and bash profiles. (tcsh flips in Task 8, cpp conditionally in Task 9.) Task 7's CLI gate reads `args.profile.capabilities.pause_while_running`.
- Evidence for enabling perl without further work (the spec's §8 verification): `tests/integration/test_perl_adapter_pause_source.py::test_pause_stops_running_program` already proves the perl adapter pauses a running loop with `reason == "pause"`. Bash: `pause` is handled at `src/tdb/adapters/bash/server.py:353`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_pause_while_running_capability.py
"""--run needs adapters that honor DAP `pause` while the debuggee is
running. python/perl/bash do today; tcsh and cpp are enabled by later
tasks (tcsh pause handler; cpp after verification)."""

from tdb.languages.base import ProfileCapabilities
from tdb.languages.bash import build_bash_profile
from tdb.languages.perl import build_perl_profile
from tdb.languages.python import build_python_profile


def test_default_is_false():
    assert ProfileCapabilities().pause_while_running is False


def test_python_perl_bash_support_pause_while_running():
    assert build_python_profile().capabilities.pause_while_running is True
    assert build_perl_profile().capabilities.pause_while_running is True
    assert build_bash_profile().capabilities.pause_while_running is True
```

Note: check the actual builder-function names at the bottom of each
profile module first (`grep -n "^def build\|_PROFILE =" src/tdb/languages/*.py`).
If a module exposes only a module-level `*_PROFILE` constant or a
differently named builder, import that instead — the assertion content
stays the same.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_pause_while_running_capability.py -v`
Expected: FAIL — `ProfileCapabilities` has no attribute `pause_while_running`.

- [ ] **Step 3: Implement**

In `src/tdb/languages/base.py`, add to `ProfileCapabilities` after `task_inspection`:

```python
    # True -> the adapter honors a DAP `pause` request while the
    # debuggee is running (required for `tdb --run`). debugpy, the
    # perl adapter, and the bash adapter all do; tcsh gains it with
    # its pause handler; gdb/lldb-dap pending verification.
    pause_while_running: bool = False
```

In `src/tdb/languages/python.py`, add `pause_while_running=True,` inside the existing `ProfileCapabilities(...)` call.

In `src/tdb/languages/perl.py` and `src/tdb/languages/bash.py`, change `capabilities=ProfileCapabilities(),` to `capabilities=ProfileCapabilities(pause_while_running=True),`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_pause_while_running_capability.py tests/unit/test_languages_base.py tests/unit/test_bash_profile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/languages/base.py src/tdb/languages/python.py src/tdb/languages/perl.py src/tdb/languages/bash.py tests/unit/test_pause_while_running_capability.py
git commit -m "feat: add pause_while_running capability flag (python/perl/bash)"
```

---

### Task 2: `SwappableEventHandler`

**Files:**
- Modify: `src/tdb/session/event_bus.py` (append after `CompositeEventHandler`)
- Test: `tests/unit/test_swappable_event_handler.py`

**Interfaces:**
- Consumes: `DebugEventHandler` protocol (same file) — methods `on_initialized`, `on_stopped(thread_id, reason, description=None, text=None)`, `on_continued`, `on_terminated`, `on_exited(exit_code)`, `on_output(text, category)`, `on_external_terminal_started`.
- Produces: `SwappableEventHandler(target)` with property `.target` and method `.retarget(new_handler) -> DebugEventHandler` (returns the previous target). Tasks 5 and 6 call `retarget`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_swappable_event_handler.py
"""Run mode swaps the live controller's event sink between a console
printer and the TUI without recreating the controller; every protocol
method must delegate to the *current* target."""

from tdb.session.event_bus import DebugEventHandler, SwappableEventHandler


class _Recorder:
    def __init__(self):
        self.calls = []

    def on_initialized(self):
        self.calls.append(("initialized",))

    def on_stopped(self, thread_id, reason, description=None, text=None):
        self.calls.append(("stopped", thread_id, reason, description, text))

    def on_continued(self):
        self.calls.append(("continued",))

    def on_terminated(self):
        self.calls.append(("terminated",))

    def on_exited(self, exit_code):
        self.calls.append(("exited", exit_code))

    def on_output(self, text, category):
        self.calls.append(("output", text, category))

    def on_external_terminal_started(self):
        self.calls.append(("terminal",))


def test_delegates_every_method_and_retargets():
    a, b = _Recorder(), _Recorder()
    h = SwappableEventHandler(a)
    assert isinstance(h, DebugEventHandler)  # runtime_checkable protocol
    assert h.target is a

    h.on_initialized()
    h.on_stopped(1, "pause", "d", "t")
    h.on_continued()
    h.on_output("x", "stdout")
    assert [c[0] for c in a.calls] == ["initialized", "stopped", "continued", "output"]
    assert a.calls[1] == ("stopped", 1, "pause", "d", "t")

    old = h.retarget(b)
    assert old is a
    assert h.target is b
    h.on_terminated()
    h.on_exited(3)
    h.on_external_terminal_started()
    assert b.calls == [("terminated",), ("exited", 3), ("terminal",)]
    assert len(a.calls) == 4  # nothing leaked to the old target
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_swappable_event_handler.py -v`
Expected: FAIL — cannot import `SwappableEventHandler`.

- [ ] **Step 3: Implement**

Append to `src/tdb/session/event_bus.py`:

```python
class SwappableEventHandler:
    """Delegates every event to a replaceable target handler.

    Run mode (`tdb --run`) alternates between a console-printing
    handler (headless phase) and the TUI's TextualEventHandler
    (interactive episodes) on one live controller; `retarget` is the
    switch. Called synchronously from the DAP read loop, same as any
    other DebugEventHandler.
    """

    def __init__(self, target: DebugEventHandler) -> None:
        self._target = target

    @property
    def target(self) -> DebugEventHandler:
        return self._target

    def retarget(self, new_target: DebugEventHandler) -> DebugEventHandler:
        old, self._target = self._target, new_target
        return old

    def on_initialized(self) -> None:
        self._target.on_initialized()

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        self._target.on_stopped(thread_id, reason, description, text)

    def on_continued(self) -> None:
        self._target.on_continued()

    def on_terminated(self) -> None:
        self._target.on_terminated()

    def on_exited(self, exit_code: int) -> None:
        self._target.on_exited(exit_code)

    def on_output(self, text: str, category: str) -> None:
        self._target.on_output(text, category)

    def on_external_terminal_started(self) -> None:
        self._target.on_external_terminal_started()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_swappable_event_handler.py tests/unit/test_event_handler.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/session/event_bus.py tests/unit/test_swappable_event_handler.py
git commit -m "feat: SwappableEventHandler for run-mode handler switching"
```

---

### Task 3: Controller support — pause without a prior stop, breakpoint push, adopted-session restart guard

**Files:**
- Modify: `src/tdb/session/controller.py:29-80` (`__init__`), `:121-128` (`supports_restart`), `:538-573` (`pause`), new method after `pause`
- Test: `tests/unit/test_controller_run_mode_support.py` (extend patterns from `tests/unit/test_controller_pause.py`)

**Interfaces:**
- Consumes: `DebugState` (`state.current_thread_id`, `state.is_terminated`, `state.phase`, `state.breakpoints`, `state.breakpoints_disabled`), `self.client.threads()`, existing private helpers `_send_breakpoints` / `_enabled_bps`.
- Produces:
  - `controller.pause(timeout)` now works when `state.current_thread_id is None` (the run-mode case — no stop has ever happened) by asking the adapter for its thread list and pausing the first thread.
  - `async def push_all_breakpoints(self) -> None` — installs every enabled breakpoint in `state.breakpoints` via `setBreakpoints` (Task 5 calls it when the TUI adopts a mid-flight session).
  - `controller.adopted_session: bool` attribute (default False); `supports_restart` returns False when it is True. Task 6 sets it.

- [ ] **Step 1: Read the existing pause tests**

Read `tests/unit/test_controller_pause.py` fully to reuse its fake-client/handler fixtures. Follow its style for the new tests.

- [ ] **Step 2: Write the failing tests**

```python
# tests/unit/test_controller_run_mode_support.py
"""Run mode pauses a debuggee that has never stopped (current_thread_id
is None), pushes breakpoints when the TUI adopts a live session, and
never offers restart for adopted sessions."""

import asyncio

import pytest

from tdb.dap.types import Thread
from tdb.session.controller import DebugController


class _NullHandler:
    def on_initialized(self):
        pass

    def on_stopped(self, thread_id, reason, description=None, text=None):
        pass

    def on_continued(self):
        pass

    def on_terminated(self):
        pass

    def on_exited(self, exit_code):
        pass

    def on_output(self, text, category):
        pass

    def on_external_terminal_started(self):
        pass


@pytest.fixture
def controller():
    return DebugController(_NullHandler())


async def test_pause_falls_back_to_thread_query(controller, monkeypatch):
    controller.state.current_thread_id = None  # never stopped: run mode
    paused = []

    async def fake_threads():
        return [Thread(id=7, name="MainThread")]

    async def fake_pause(thread_id):
        paused.append(thread_id)
        controller._stopped_event.set()  # simulate the stop landing

    monkeypatch.setattr(controller.client, "threads", fake_threads)
    monkeypatch.setattr(controller.client, "pause", fake_pause)
    assert await controller.pause(timeout=1.0) is True
    assert paused == [7]


async def test_pause_reports_false_when_no_threads(controller, monkeypatch):
    controller.state.current_thread_id = None

    async def fake_threads():
        return []

    monkeypatch.setattr(controller.client, "threads", fake_threads)
    assert await controller.pause(timeout=0.1) is False


async def test_push_all_breakpoints_sends_each_file(controller, monkeypatch):
    from tdb.session.state import SourceBreakpoint

    controller.state.breakpoints = {
        "/a.py": [SourceBreakpoint(line=3)],
        "/b.py": [SourceBreakpoint(line=9)],
    }
    sent = []

    async def fake_send(path, bps):
        sent.append((path, [bp.line for bp in bps]))

    monkeypatch.setattr(controller, "_send_breakpoints", fake_send)
    await controller.push_all_breakpoints()
    assert sorted(sent) == [("/a.py", [3]), ("/b.py", [9])]

    sent.clear()
    controller.state.breakpoints_disabled = True
    await controller.push_all_breakpoints()
    assert sent == []


def test_adopted_session_disables_restart(controller):
    assert controller.supports_restart is True
    controller.adopted_session = True
    assert controller.supports_restart is False
```

Note: verify `SourceBreakpoint`'s constructor and the `Thread` type's
module (`grep -n "class SourceBreakpoint" src/tdb/session/state.py` and
`grep -n "class Thread" src/tdb/dap/types.py`) and adjust the imports /
keywords to match; existing tests in `tests/unit/test_controller_pause.py`
show the working idioms.

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_controller_run_mode_support.py -v`
Expected: FAIL — pause returns False with `current_thread_id is None`; `push_all_breakpoints` and `adopted_session` don't exist.

- [ ] **Step 4: Implement**

In `__init__` (near the `_is_remote_attach` assignment):

```python
        # True for sessions handed to the TUI by run mode (`tdb --run`).
        # Restart is meaningless there: the run-mode loop owns the
        # process lifecycle, not the TUI episode.
        self.adopted_session: bool = False
```

`supports_restart` (keep the docstring, extend it):

```python
        return not (self._is_remote_attach or self.adopted_session)
```

In `pause()`, replace the `current_thread_id is None -> return False` early-out with a thread-list fallback:

```python
        thread_id = self.state.current_thread_id
        if thread_id is None:
            # Run mode: the debuggee has never stopped, so no stop event
            # ever recorded a thread id. Ask the adapter directly.
            try:
                threads = await self.client.threads()
            except Exception:
                log.exception("thread query for pause failed")
                return False
            if not threads:
                return False
            thread_id = threads[0].id
```

and use the local `thread_id` in the subsequent `await self.client.pause(...)` call instead of `self.state.current_thread_id`.

New method after `pause()`:

```python
    async def push_all_breakpoints(self) -> None:
        """Install every enabled breakpoint over DAP.

        Used when the TUI adopts an already-configured session (run
        mode): `do_configure` ran long ago with an empty breakpoint
        map, so saved breakpoints loaded at adoption time must be sent
        explicitly.
        """
        if self.state.breakpoints_disabled:
            return
        for source_path, bps in self.state.breakpoints.items():
            await self._send_breakpoints(source_path, self._enabled_bps(bps))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_controller_run_mode_support.py tests/unit/test_controller_pause.py tests/unit/test_controller_actions.py -v`
Expected: PASS (existing pause tests must not regress).

- [ ] **Step 6: Commit**

```bash
git add src/tdb/session/controller.py tests/unit/test_controller_run_mode_support.py
git commit -m "feat: controller support for run mode (pause fallback, breakpoint push, adopted guard)"
```

---

### Task 4: `ConsoleRunHandler`

**Files:**
- Create: `src/tdb/run_mode.py`
- Test: `tests/unit/test_console_run_handler.py`

**Interfaces:**
- Consumes: `DebugEventHandler` protocol shape.
- Produces (Task 6 waits on these):
  - `ConsoleRunHandler()` with `initialized: asyncio.Event`, `stopped: asyncio.Event`, `exited: asyncio.Event`, `exit_code: int | None`, `last_stop: tuple[int | None, str, str | None, str | None] | None`.
  - `on_output` writes stdout-category text to `sys.stdout`, `stderr`-category to `sys.stderr`, flushing each write; other categories (e.g. `console`) go to stdout.
  - `on_terminated` sets `exited` (a terminate without an exit code still ends the run phase); `on_exited` records the code and sets `exited`.
  - `on_continued` clears `stopped`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_console_run_handler.py
"""Headless run phase: debuggee output streams straight to the
terminal; exit/stop events become asyncio.Events the run loop awaits."""

import asyncio

from tdb.run_mode import ConsoleRunHandler
from tdb.session.event_bus import DebugEventHandler


async def test_protocol_and_events(capsys):
    h = ConsoleRunHandler()
    assert isinstance(h, DebugEventHandler)
    assert not h.initialized.is_set()

    h.on_initialized()
    assert h.initialized.is_set()

    h.on_output("out\n", "stdout")
    h.on_output("err\n", "stderr")
    h.on_output("note\n", "console")
    captured = capsys.readouterr()
    assert captured.out == "out\nnote\n"
    assert captured.err == "err\n"

    h.on_stopped(4, "pause", None, None)
    assert h.stopped.is_set()
    assert h.last_stop == (4, "pause", None, None)
    h.on_continued()
    assert not h.stopped.is_set()

    h.on_exited(7)
    assert h.exited.is_set()
    assert h.exit_code == 7


async def test_terminated_without_exit_code_ends_run_phase():
    h = ConsoleRunHandler()
    h.on_terminated()
    assert h.exited.is_set()
    assert h.exit_code is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_console_run_handler.py -v`
Expected: FAIL — no module `tdb.run_mode`.

- [ ] **Step 3: Implement**

```python
# src/tdb/run_mode.py
"""`tdb --run`: headless execution with signal-triggered TUI episodes.

The debuggee runs under the normal adapter session but with no TUI, no
stop-on-entry, and no breakpoints. Debuggee output streams to the
terminal. Ctrl-C (or SIGUSR1 on POSIX) pauses the debuggee and opens
the TUI at the paused line; quitting the TUI can detach back here.
See docs/superpowers/specs/2026-08-15-run-mode-signal-tui-design.md.
"""

from __future__ import annotations

import asyncio
import logging
import sys

log = logging.getLogger(__name__)


class ConsoleRunHandler:
    """Event sink for the headless phase of run mode.

    Called synchronously from the DAP read loop (same asyncio loop),
    so setting asyncio.Events here is safe without call_soon_threadsafe.
    """

    def __init__(self) -> None:
        self.initialized = asyncio.Event()
        self.stopped = asyncio.Event()
        self.exited = asyncio.Event()
        self.exit_code: int | None = None
        self.last_stop: tuple[int | None, str, str | None, str | None] | None = None

    def on_initialized(self) -> None:
        self.initialized.set()

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        self.last_stop = (thread_id, reason, description, text)
        self.stopped.set()

    def on_continued(self) -> None:
        self.stopped.clear()

    def on_terminated(self) -> None:
        self.exited.set()

    def on_exited(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.exited.set()

    def on_output(self, text: str, category: str) -> None:
        stream = sys.stderr if category == "stderr" else sys.stdout
        stream.write(text)
        stream.flush()

    def on_external_terminal_started(self) -> None:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_console_run_handler.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/run_mode.py tests/unit/test_console_run_handler.py
git commit -m "feat: ConsoleRunHandler for headless run phase"
```

---

### Task 5: TdbApp adopted-session mode + detach/terminate quit dialog

**Files:**
- Modify: `src/tdb/app.py:173-272` (`__init__`), `:314-368` (`on_mount`), `:1492-1509` (`action_quit_debugger`), `:1585-1595` (`action_confirm_quit`), new `_adopt_session` worker + `_detach_and_exit`/`_terminate_and_exit`
- Modify: `src/tdb/widgets/modals.py` (add `_DetachQuitModal` after `_QuitConfirmModal`)
- Test: `tests/unit/test_app_adopted_session.py`

**Interfaces:**
- Consumes: `SwappableEventHandler.retarget` (Task 2), `DebugController.push_all_breakpoints` / `adopted_session` (Task 3), `DapStopped` message (`tdb/session/messages.py`), existing `_QuitConfirmModal` import site in app.py.
- Produces (Task 6 relies on these exact names):
  - `TdbApp(..., adopted_controller: DebugController | None = None, adopted_handler: SwappableEventHandler | None = None, adopted_stop: tuple | None = None)` — both `adopted_*` handler/controller must be passed together.
  - `app.detach_and_resume: bool` — True iff the user chose "Detach & resume".
  - Adopted mode never starts/attaches a session, retargets `adopted_handler` to the app's `TextualEventHandler` in `__init__`, and populates the UI by pushing breakpoints then posting a synthetic `DapStopped` from `adopted_stop`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_app_adopted_session.py
"""Adopted-session TUI episodes (tdb --run): the app reuses a live
controller, retargets the swappable handler, routes EVERY quit path
through the detach/terminate dialog, and never calls controller.stop()
on detach (the debuggee must keep running)."""

import pytest

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.run_mode import ConsoleRunHandler
from tdb.session.controller import DebugController
from tdb.session.event_bus import SwappableEventHandler
from tdb.widgets.modals import _DetachQuitModal


@pytest.fixture
def adopted(monkeypatch):
    console = ConsoleRunHandler()
    handler = SwappableEventHandler(console)
    controller = DebugController(handler)
    controller.adopted_session = True
    controller.state.enter_stop(1, "pause")
    stops = []

    async def fake_fetch_stop_info():
        stops.append(True)

    async def fake_push_all_breakpoints():
        pass

    stopped_calls = []

    async def fake_stop():
        stopped_calls.append(True)

    monkeypatch.setattr(controller, "fetch_stop_info", fake_fetch_stop_info)
    monkeypatch.setattr(controller, "push_all_breakpoints", fake_push_all_breakpoints)
    monkeypatch.setattr(controller, "stop", fake_stop)
    app = TdbApp(
        program="",
        config=TdbConfig(),
        profile=controller.profile,
        adopted_controller=controller,
        adopted_handler=handler,
        adopted_stop=(1, "pause", None, None),
    )
    return app, handler, controller, stopped_calls


async def test_adoption_retargets_handler_and_shows_stop(adopted):
    app, handler, controller, _ = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.controller is controller
        assert handler.target is app._textual_handler
    assert app.detach_and_resume is False


async def test_q_detach_path_keeps_debuggee_alive(adopted):
    app, handler, controller, stopped_calls = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_confirm_quit()
        await pilot.pause()
        assert isinstance(app.screen, _DetachQuitModal)
        await pilot.press("d")
        for _ in range(20):
            await pilot.pause()
    assert app.detach_and_resume is True
    assert stopped_calls == []  # detach must NOT stop the controller


async def test_ctrl_q_routes_to_dialog_and_terminate_stops(adopted):
    app, handler, controller, stopped_calls = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, _DetachQuitModal)
        await pilot.press("t")
        for _ in range(20):
            await pilot.pause()
    assert app.detach_and_resume is False
    assert stopped_calls == [True]


async def test_escape_cancels_dialog(adopted):
    app, handler, controller, stopped_calls = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_confirm_quit()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _DetachQuitModal)
        assert app._is_quitting is False
    assert stopped_calls == []
```

Note: `DebugController(handler)` defaults to the python profile;
`state.enter_stop(1, "pause")` is the same call `_on_stopped` makes
(controller.py:918). If `enter_stop`'s signature differs, mirror
whatever `_on_stopped` does to reach a stopped state.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_app_adopted_session.py -v`
Expected: FAIL — `TdbApp.__init__` rejects `adopted_controller`; `_DetachQuitModal` missing.

- [ ] **Step 3: Add `_DetachQuitModal` to `src/tdb/widgets/modals.py`**

After `_QuitConfirmModal`:

```python
class _DetachQuitModal(ModalScreen):
    """Adopted-session quit (tdb --run): detach & resume, or terminate.

    `d`/`q` -> "detach" (default: program keeps running, tdb returns to
    headless run mode), `t` -> "terminate", ESC cancels.
    """

    DEFAULT_CSS = """
    _DetachQuitModal {
        align: center middle;
    }
    _DetachQuitModal #dialog {
        width: 56;
        height: 7;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("d", "detach", "Detach", show=False),
        Binding("q", "detach", "Detach", show=False),
        Binding("t", "terminate", "Terminate", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[bold]d[/bold]: detach & resume program", markup=True)
            yield Static("[bold]t[/bold]: terminate program & quit", markup=True)
            yield Static("[dim]ESC: cancel[/dim]", markup=True)

    def action_detach(self) -> None:
        self.dismiss("detach")

    def action_terminate(self) -> None:
        self.dismiss("terminate")

    def action_cancel(self) -> None:
        self.dismiss(None)
```

(Reuse the imports already present in modals.py — `ModalScreen`, `Binding`, `ComposeResult`, `Vertical`, `Static` are all used by neighboring modals.)

- [ ] **Step 4: Modify `TdbApp.__init__`**

Add parameters after `recorder`:

```python
adopted_controller: "DebugController | None" = (None,)
adopted_handler: "SwappableEventHandler | None" = (None,)
adopted_stop: tuple | None = (None,)
```

(Import `SwappableEventHandler` under `TYPE_CHECKING` or reuse the module's existing import style.) After the `self._textual_handler = TextualEventHandler(self)` / `_event_handler` block, replace the unconditional controller construction (app.py:235-236) with:

```python
        self._adopted = adopted_controller is not None
        self._adopted_handler = adopted_handler
        self._adopted_stop = adopted_stop
        # Set by the detach path; read by run_mode after run_async returns.
        self.detach_and_resume = False
        if adopted_controller is not None:
            # Run-mode episode: reuse the live session. The swappable
            # handler flips from the console printer to the TUI here;
            # the debuggee is paused, so no events race the swap.
            assert adopted_handler is not None
            self.controller = adopted_controller
            adopted_handler.retarget(self._textual_handler)
        else:
            self.controller = DebugController(self._event_handler, profile=self._profile)
            self.controller.step_mode = self._config.step_mode
```

- [ ] **Step 5: Modify `on_mount` and add the adoption worker**

In `on_mount`, after the `if self._post_mortem_snapshot is not None:` block, insert:

```python
        if self._adopted:
            code_view.focus()
            # Load saved breakpoints once per run-mode process: episode 2+
            # inherits live state from episode 1 and must not re-merge.
            program_key = str(Path(self._program).resolve()) if self._program else ""
            saved = load_breakpoints(program=program_key) if program_key else {}
            if saved and not self.controller.state.breakpoints:
                self.controller.state.breakpoints = saved
            if self.controller.state.breakpoints:
                bp_view = self.query_one("#breakpoint-view", BreakpointView)
                bp_view.update_breakpoints(self.controller.state.breakpoints)
            self._adopt_session()
            return
```

New worker next to `_start_session`:

```python
    @work(exclusive=True)
    async def _adopt_session(self) -> None:
        """Bring the adopted (already stopped) session into the UI.

        Pushes any saved breakpoints (the session was configured with
        none), then replays the recorded stop through the normal
        DapStopped pipeline so the code/stack/variable views populate
        exactly as they would for a live stop event.
        """
        try:
            await self.controller.push_all_breakpoints()
        except Exception:
            log.exception("push_all_breakpoints failed during adoption")
        stop = self._adopted_stop
        if stop is None and self.controller.state.current_thread_id is not None:
            stop = (
                self.controller.state.current_thread_id,
                self.controller.state.stop_reason or "pause",
                None,
                None,
            )
        if stop is not None:
            thread_id, reason, description, text = stop
            self.post_message(DapStopped(thread_id, reason, description, text))
```

(`DapStopped` is already imported in app.py for the message handlers; verify `state.stop_reason` exists via `grep -n "stop_reason" src/tdb/session/state.py` — if the attribute has another name, use that.)

- [ ] **Step 6: Route the quit paths**

`action_quit_debugger` — add at the top (before the `_is_quitting` guard):

```python
        if self._adopted:
            # Adopted sessions route EVERY quit path — Ctrl+Q included —
            # through the detach/terminate choice.
            self.action_confirm_quit()
            return
```

`action_confirm_quit` — add the adopted branch before the existing modal push:

```python
        if self._adopted:
            if isinstance(self.screen, _DetachQuitModal):
                return

            def on_dismiss_adopted(result: str | None) -> None:
                if result == "detach":
                    self.run_worker(self._detach_and_exit())
                elif result == "terminate":
                    self.run_worker(self._terminate_and_exit())

            self.push_screen(_DetachQuitModal(), callback=on_dismiss_adopted)
            return
```

New methods next to `action_quit_debugger`:

```python
async def _detach_and_exit(self) -> None:
    """Leave the debuggee running; run_mode resumes it after exit."""
    if self._is_quitting:
        return
    self._is_quitting = True
    self.recorder.record("quit", [])
    if self._program:
        program_key = str(Path(self._program).resolve())
        save_breakpoints(self.controller.state.breakpoints, program=program_key)
    self.detach_and_resume = True
    self.exit()


async def _terminate_and_exit(self) -> None:
    if self._is_quitting:
        return
    self._is_quitting = True
    self.recorder.record("quit", [])
    if self._program:
        program_key = str(Path(self._program).resolve())
        save_breakpoints(self.controller.state.breakpoints, program=program_key)
    await self.controller.stop()
    self.exit()
```

Import `_DetachQuitModal` wherever `_QuitConfirmModal` is imported in app.py. Exit-path audit checklist (verify each lands in `action_confirm_quit` or `action_quit_debugger`, both now adopted-aware): `q` binding (app.py:128), `ctrl+q` binding (app.py:127), code-view `DebugAction("quit")` (app.py:900-901), any menu quit entry (grep `"quit"` in `widgets/menu_bar.py` and app_handlers/).

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_app_adopted_session.py tests/unit/test_app_adapter_not_found.py tests/unit/test_open_file_gate.py -v`
Expected: PASS (existing app tests must not regress).

- [ ] **Step 8: Commit**

```bash
git add src/tdb/app.py src/tdb/widgets/modals.py tests/unit/test_app_adopted_session.py
git commit -m "feat: TdbApp adopted-session mode with detach/terminate quit dialog"
```

---

### Task 6: Run-mode loop, signal handling, TUI episodes

**Files:**
- Modify: `src/tdb/run_mode.py` (append the loop below `ConsoleRunHandler`)
- Test: `tests/integration/test_run_mode.py`

**Interfaces:**
- Consumes: Tasks 2-5 (`SwappableEventHandler`, `controller.pause` fallback, `adopted_session`, `TdbApp(adopted_*)`, `app.detach_and_resume`), `tdb._timeouts.DAP_INITIALIZED`, `AdapterNotFoundError` from `tdb.languages.base`, `SessionPhase` from `tdb.session.state`.
- Produces (Task 7's CLI dispatch calls this):
  - `async def run(program, args=None, cwd=None, just_my_code=True, python=None, sub_process=True, profile=None, config=None, tui_episode=None, on_session_ready=None) -> int` — returns the process exit code for tdb.
  - `tui_episode(controller, handler, console, config, program) -> Awaitable[bool]` — returns True to detach & resume, False to terminate. Default `_default_tui_episode` runs `TdbApp` via `run_async()`.
  - `on_session_ready(controller)` — called once after configuration (tests use it to observe the controller; the CLI leaves it None).

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/integration/test_run_mode.py
"""End-to-end run mode against a real debugpy session, in-process:
exit-code passthrough, output streaming, SIGUSR1 -> pause -> episode ->
detach -> resume -> second episode -> terminate."""

import asyncio
import os
import signal

import pytest

from tdb import run_mode
from tdb.persist import TdbConfig
from tdb.session.state import SessionPhase

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="signal-driven run mode tests are POSIX-only"
)

EXIT_SCRIPT = "import sys\nprint('bye')\nsys.exit(7)\n"
LOOP_SCRIPT = "import time\ni = 0\nwhile True:\n    i += 1\n    time.sleep(0.01)\n"


async def _wait_until(pred, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


async def test_exit_code_and_output_passthrough(tmp_path, capfd):
    p = tmp_path / "exit7.py"
    p.write_text(EXIT_SCRIPT)
    code = await run_mode.run(program=str(p), config=TdbConfig())
    assert code == 7
    assert "bye" in capfd.readouterr().out


async def test_signal_pause_episode_detach_and_terminate(tmp_path):
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    box = {}
    episodes = []

    def ready(controller):
        box["controller"] = controller

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        assert controller.state.phase is SessionPhase.STOPPED
        assert console.last_stop is not None
        # Episode 1 detaches; episode 2 terminates.
        return len(episodes) == 1

    async def pulses():
        await _wait_until(
            lambda: (
                box.get("controller") is not None
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)
        await _wait_until(
            lambda: (
                len(episodes) == 1
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)

    pulse_task = asyncio.ensure_future(pulses())
    try:
        code = await asyncio.wait_for(
            run_mode.run(
                program=str(p),
                config=TdbConfig(),
                tui_episode=fake_episode,
                on_session_ready=ready,
            ),
            timeout=90.0,
        )
    finally:
        pulse_task.cancel()
    assert episodes and len(episodes) == 2
    assert code == 0
    assert box["controller"].state.is_terminated
```

Check `SessionPhase` member names and the `state.is_terminated` /
`state.phase` attributes against `src/tdb/session/state.py` before
running; adjust names to what the state module actually exposes
(`grep -n "class SessionPhase\|is_terminated\|phase" src/tdb/session/state.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/integration/test_run_mode.py -v`
Expected: FAIL — `run_mode` has no `run`.

- [ ] **Step 3: Implement the loop**

Append to `src/tdb/run_mode.py`:

```python
import os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from tdb.session.controller import DebugController
from tdb.session.event_bus import SwappableEventHandler

if TYPE_CHECKING:
    from tdb.languages.base import LanguageProfile
    from tdb.persist import TdbConfig

TuiEpisode = Callable[
    [DebugController, SwappableEventHandler, ConsoleRunHandler, "TdbConfig", str],
    Awaitable[bool],
]

# Longer than the interactive pause timeout: nothing else is happening,
# and slow-to-stop debuggees (deep C calls) deserve the extra grace.
_PAUSE_TIMEOUT = 5.0


def _arm_signals(loop: asyncio.AbstractEventLoop, trigger: Callable[[], None]) -> list:
    """Route SIGINT (and SIGUSR1 on POSIX) to `trigger`.

    Returns the list of signals actually armed. Failure (non-main
    thread — embedded use, some test runners) degrades to "no signal
    interruption" rather than crashing run mode.
    """
    installed: list = []
    try:
        if os.name != "nt":
            for sig in (signal.SIGINT, signal.SIGUSR1):
                loop.add_signal_handler(sig, trigger)
                installed.append(sig)
        else:
            signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(trigger))
            installed.append(signal.SIGINT)
    except (ValueError, NotImplementedError, RuntimeError):
        log.warning("cannot install run-mode signal handlers", exc_info=True)
    return installed


def _disarm_signals(
    loop: asyncio.AbstractEventLoop, installed: list, *, ignore: bool
) -> None:
    """Remove run-mode handlers.

    ignore=True while a TUI episode owns the terminal: a stray SIGUSR1
    must be a no-op, not the default action (which kills the process).
    ignore=False on final exit: restore Python defaults.
    """
    for sig in installed:
        if os.name != "nt":
            try:
                loop.remove_signal_handler(sig)
            except (ValueError, RuntimeError):
                pass
        if ignore:
            handler = signal.SIG_IGN
        elif sig == signal.SIGINT:
            handler = signal.default_int_handler
        else:
            handler = signal.SIG_DFL
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


async def _wait_first(*events: asyncio.Event) -> None:
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


async def _default_tui_episode(
    controller: DebugController,
    handler: SwappableEventHandler,
    console: ConsoleRunHandler,
    config: "TdbConfig",
    program: str,
) -> bool:
    from tdb.app import TdbApp

    app = TdbApp(
        program=program,
        config=config,
        profile=controller.profile,
        adopted_controller=controller,
        adopted_handler=handler,
        adopted_stop=console.last_stop,
    )
    await app.run_async()
    return app.detach_and_resume


async def run(
    program: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    just_my_code: bool = True,
    python: str | None = None,
    sub_process: bool = True,
    profile: "LanguageProfile | None" = None,
    config: "TdbConfig | None" = None,
    tui_episode: TuiEpisode | None = None,
    on_session_ready: Callable[[DebugController], None] | None = None,
) -> int:
    """Run `program` headless; signals open TUI episodes. Returns tdb's
    exit code (the debuggee's when it exits during the run phase)."""
    from tdb._timeouts import DAP_INITIALIZED
    from tdb.languages.base import AdapterNotFoundError
    from tdb.persist import load_config

    if config is None:
        config = load_config()
    console = ConsoleRunHandler()
    handler = SwappableEventHandler(console)
    controller = DebugController(handler, profile=profile)
    controller.step_mode = config.step_mode
    controller.adopted_session = True  # restart is never offered in run mode

    try:
        await controller.start(
            program=program,
            args=args,
            cwd=cwd or str(Path.cwd()),
            stop_on_entry=False,
            just_my_code=just_my_code,
            python=python,
            sub_process=sub_process,
        )
    except AdapterNotFoundError as exc:
        print(f"tdb: {exc.hint}", file=sys.stderr)
        return 2

    await asyncio.wait_for(console.initialized.wait(), timeout=DAP_INITIALIZED)
    await controller.do_configure()
    if on_session_ready is not None:
        on_session_ready(controller)

    hint = "Ctrl-C" if os.name == "nt" else f"Ctrl-C or `kill -USR1 {os.getpid()}`"
    print(f"tdb: running {program} — {hint} opens the debugger", file=sys.stderr)

    loop = asyncio.get_running_loop()
    interrupt = asyncio.Event()
    episode = tui_episode or _default_tui_episode
    installed = _arm_signals(loop, interrupt.set)
    exit_code = 0
    try:
        while True:
            await _wait_first(console.exited, interrupt, console.stopped)
            if console.exited.is_set():
                exit_code = console.exit_code or 0
                break
            if interrupt.is_set() and not console.stopped.is_set():
                interrupt.clear()
                ok = await controller.pause(timeout=_PAUSE_TIMEOUT)
                if console.exited.is_set():
                    # Died between the signal and the pause landing.
                    exit_code = console.exit_code or 0
                    break
                if not ok:
                    print(
                        "tdb: pause requested — the program is blocked inside "
                        "a single call; the debugger opens when it returns",
                        file=sys.stderr,
                    )
                    continue
            # Reached on a landed pause, or on a spontaneous stop (a
            # breakpoint set during a previous episode).
            interrupt.clear()
            _disarm_signals(loop, installed, ignore=True)
            detach = await episode(controller, handler, console, config, program)
            handler.retarget(console)
            console.stopped.clear()
            if controller.state.is_terminated:
                break
            if not detach:
                await controller.stop()
                break
            await controller.continue_()
            installed = _arm_signals(loop, interrupt.set)
    finally:
        _disarm_signals(loop, installed, ignore=False)
    return exit_code
```

Note: `controller.start`'s keyword names must match its signature at
`src/tdb/session/controller.py:210` (app.py:450-459 shows the full call
with `terminal=` and `sub_process=`; run mode omits `terminal`). If
`sub_process` is not a `start` parameter for non-python profiles,
mirror exactly what `server/runner.py:85-92` passes.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_run_mode.py -v`
Expected: PASS. The second test takes tens of seconds (debugpy launch + two pause round-trips) — that is normal.

- [ ] **Step 5: Run the neighboring suites**

Run: `python -m pytest tests/unit -v -x -q` and `python -m pytest tests/integration/test_dap_session.py -v`
Expected: PASS — no regressions.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/run_mode.py tests/integration/test_run_mode.py
git commit -m "feat: run-mode loop with signal-triggered TUI episodes"
```

---

### Task 7: CLI `--run` flag, validation, dispatch

**Files:**
- Modify: `src/tdb/cli.py:30+` (build_parser), `:245-258` (`_apply_flag_implications`), `:537-586` (`parse_args`), `:589-631` (`main`), new `_run_run` helper
- Test: `tests/unit/test_cli_run_flag.py`

**Interfaces:**
- Consumes: `run_mode.run` (Task 6), `args.profile.capabilities.pause_while_running` (Task 1).
- Produces: `tdb --run PROG [ARGS...]` end-user entry point; `args.run: bool` on the parsed namespace.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_cli_run_flag.py
"""--run: headless execution until a signal opens the TUI. Long flag
only (-r is --remote-attach); incompatible with every mode that owns
the terminal or the session lifecycle differently; requires an adapter
that can pause a running debuggee."""

import pytest

from tdb.cli import parse_args


@pytest.fixture
def prog(tmp_path):
    p = tmp_path / "prog.py"
    p.write_text("print('hi')\n")
    return str(p)


def test_run_flag_parses_and_implies_no_stop_on_entry(prog):
    args = parse_args(["--run", prog])
    assert args.run is True
    assert args.stop_on_entry is False


def test_run_rejects_short_r_as_remote_attach(prog):
    # -r must still mean --remote-attach, never --run.
    args = parse_args(["-r", "5678"])
    assert args.remote_attach == "5678"
    assert args.run is False


@pytest.mark.parametrize(
    "extra",
    [
        ["-r", "5678"],
        ["-k", "3"],
        ["-t", "3"],
        ["--record", "out.json"],
        ["--server"],
        ["--headless"],
        ["--mcp"],
    ],
)
def test_run_conflicts(prog, extra, capsys):
    with pytest.raises(SystemExit):
        parse_args(["--run", prog] + extra)
    assert "--run cannot be combined with" in capsys.readouterr().err


def test_run_conflicts_with_replay(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--run", "--replay", "session.json"])
    assert "--run cannot be combined with" in capsys.readouterr().err


def test_run_requires_pause_capable_language(tmp_path, capsys):
    # cpp is the profile without pause_while_running until Task 9.
    prog = tmp_path / "prog.py"
    prog.write_text("print('hi')\n")
    with pytest.raises(SystemExit):
        parse_args(["--run", "--lang", "cpp", str(prog)])
    err = capsys.readouterr().err
    assert "cannot pause a running program" in err
```

Note on the last test: if Task 9 has already enabled cpp by the time
this runs, or the cpp profile errors earlier because gdb/lldb-dap is
missing on PATH, swap in any profile with
`pause_while_running=False` — build the same fake-profile pattern used
by `tests/unit/test_app_adapter_not_found.py::_missing_adapter_profile`
and monkeypatch `tdb.languages.registry.resolve` to return it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_cli_run_flag.py -v`
Expected: FAIL — unrecognized argument `--run`.

- [ ] **Step 3: Implement**

`build_parser` — add after the `--remote-attach` argument:

```python
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run the program without the TUI, at full speed, ignoring "
        "all breakpoints. Press Ctrl-C (or send SIGUSR1 on Unix) to "
        "pause it and open the debugger at the current line; quitting "
        "the debugger can detach and resume the program. For "
        "inspecting programs that appear to be hung.",
    )
```

`_apply_flag_implications` — add:

```python
    if args.run:
        args.stop_on_entry = False
```

`parse_args` — after `_apply_flag_implications(args)` add the conflict check (before the existing `--record`/`--replay` checks so `--run` conflicts win the error message):

```python
    if args.run:
        for flag, value in (
            ("-r/--remote-attach", args.remote_attach),
            ("-k/--breakpoint", args.breakpoint),
            ("-t/--to-line", args.to_line),
            ("--record", args.record),
            ("--replay", args.replay),
            ("--headless", args.headless),
            ("--server", args.server),
            ("--mcp", args.mcp),
            ("--terminal", args.terminal),
            ("--post-mortem", args.post_mortem),
        ):
            if value:
                parser.error(f"--run cannot be combined with {flag}")
```

(`--headless` is listed before `--server` because `--headless` implies `--server`; checking it first names the flag the user actually typed.)

After `_resolve_language(args, parser)`:

```python
    if args.run and not args.profile.capabilities.pause_while_running:
        parser.error(
            f"--run is not supported for {args.profile.id}: its debug "
            f"adapter cannot pause a running program"
        )
```

`main()` dispatch — insert before the `elif args.headless:` branch:

```python
    elif args.run:
        _run_run(args)
```

New helper next to `_run_headless`:

```python
def _run_run(args: argparse.Namespace) -> None:
    """Run headless until a signal opens the TUI (`--run`)."""
    import asyncio
    from tdb.persist import load_config
    from tdb.run_mode import run

    code = asyncio.run(
        run(
            program=args.program,
            args=args.args,
            cwd=args.cwd,
            just_my_code=not args.no_just_my_code,
            python=args.python,
            sub_process=not args.no_subprocess,
            profile=args.profile,
            config=load_config(),
        )
    )
    sys.exit(code)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_cli_run_flag.py tests/unit/test_cli.py tests/unit/test_cli_record_flags.py tests/unit/test_cli_breakpoint_install.py -v`
Expected: PASS.

- [ ] **Step 5: Smoke-test by hand**

Run: `python -c "import sys; sys.argv=['tdb','--run','--help']; " ` is not meaningful — instead run `python -m tdb --help | grep -A3 -- --run` and confirm the help text renders, then `python -m tdb --run tests/../test_targets/sleeper.py` from a terminal if `test_targets/sleeper.py` exists (Ctrl-C should open the TUI; `q`,`d` should detach). This manual check complements the automated integration test; note the result in the commit message body if anything surprising appears.

- [ ] **Step 6: Commit**

```bash
git add src/tdb/cli.py tests/unit/test_cli_run_flag.py
git commit -m "feat: add --run CLI flag with validation and dispatch"
```

---

### Task 8: tcsh `pause` support

**Files:**
- Modify: `src/tdb/adapters/tcsh/models.py:18-23` (StopReason)
- Modify: `src/tdb/adapters/tcsh/session.py:192` area (init state), `:780-820` (`_handle_probe`), new `request_pause` method
- Modify: `src/tdb/adapters/tcsh/server.py:128-144` (dispatch table), new `_pause` handler
- Modify: `src/tdb/languages/tcsh.py:101` (capability flip)
- Test: `tests/integration/test_tcsh_adapter.py` (append), `tests/unit/test_pause_while_running_capability.py` (extend)

**Interfaces:**
- Consumes: existing probe rendezvous — `_handle_probe` runs every statement while the script blocks on the control FIFO; `self._emit(SessionEvent("stopped", {...}))` and the `transport.release()` continue path.
- Produces: DAP `pause` request → `stopped` event with `reason == "pause"` at the next probe; `tcsh` profile `pause_while_running=True`.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/integration/test_tcsh_adapter.py` (reusing its `dap_client`, `tcsh_path` fixtures and helper functions):

```python
@pytest.mark.asyncio
async def test_pause_stops_running_loop(
    dap_client: DAPClient,
    tcsh_path: Path,
    tmp_path: Path,
) -> None:
    program = tmp_path / "spin.csh"
    program.write_text("set i = 0\nwhile (1)\n  @ i++\nend\n")
    await dap_client.initialize()
    await dap_client.launch(program, tcshPath=str(tcsh_path), stopOnEntry=False)
    await configure(dap_client)
    await asyncio.sleep(0.5)  # let the loop spin freely first

    response = await dap_client.request("pause", {"threadId": 1})
    assert response["success"] is True

    stopped = await dap_client.wait_for_event("stopped")
    assert stopped["body"]["reason"] == "pause"
    frames = await stack_frames(dap_client)
    assert frames[0]["source"]["path"].endswith("spin.csh")
```

Also add to `tests/unit/test_pause_while_running_capability.py`:

```python
def test_tcsh_supports_pause_while_running():
    from tdb.languages.tcsh import build_tcsh_profile

    assert build_tcsh_profile().capabilities.pause_while_running is True
```

(Adjust the builder import to the module's actual public name, same as Task 1.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/integration/test_tcsh_adapter.py::test_pause_stops_running_loop tests/unit/test_pause_while_running_capability.py -v`
Expected: FAIL — the adapter answers `pause` with `Request 'pause' is not supported`; capability False. (If tcsh isn't installed locally the integration test skips — its fixtures already gate on tcsh availability; rely on the unit failure and CI.)

- [ ] **Step 3: Implement**

`models.py` — add to `StopReason`:

```python
    PAUSE = "pause"
```

`session.py` — next to the `self._run_mode = RunMode.CONTINUE` initialization add:

```python
        self._pause_pending = False
```

New method near `continue_()`:

```python
    def request_pause(self) -> None:
        """Stop at the next probe regardless of run mode (DAP `pause`).

        The instrumented script rendezvouses with the adapter after
        every statement, so this needs no signal delivery — just a flag
        the next `_handle_probe` call consumes.
        """
        self._pause_pending = True
```

`_handle_probe` — make a pending pause take priority; change the reason chain to:

```python
        reason: StopReason | None = None
        if self._pause_pending:
            self._pause_pending = False
            reason = StopReason.PAUSE
        elif self._entry_pending and event_depth == 0:
            self._entry_pending = False
            reason = StopReason.ENTRY
        elif probe.id in self._breakpoint_probe_ids.get(probe.span.path, frozenset()):
            reason = StopReason.BREAKPOINT
        elif should_stop_at_probe(
            self._run_mode,
            self._step_start_depth,
            event_depth,
            False,
        ):
            reason = StopReason.STEP
```

`server.py` — add to `self._dispatch`:

```python
            "pause": self._pause,
```

and the handler next to `_continue`:

```python
    async def _pause(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        session.request_pause()
        return {}
```

(Match `_continue`'s exact signature/return convention — read it first; if it returns via a different shape, mirror it.)

`languages/tcsh.py` — `capabilities=ProfileCapabilities(pause_while_running=True),`

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/integration/test_tcsh_adapter.py tests/unit/test_pause_while_running_capability.py -v`
Expected: PASS (full tcsh adapter suite — the new reason value must not break golden tests; if `tests/unit/tcsh_golden/` fixtures enumerate StopReason values, update them).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/adapters/tcsh/models.py src/tdb/adapters/tcsh/session.py src/tdb/adapters/tcsh/server.py src/tdb/languages/tcsh.py tests/integration/test_tcsh_adapter.py tests/unit/test_pause_while_running_capability.py
git commit -m "feat: tcsh adapter pause support; enable --run for tcsh"
```

---

### Task 9: C/C++ pause verification

**Files:**
- Test: `tests/integration/test_cpp_pause.py`
- Modify (conditional): `src/tdb/languages/cpp.py:157`, `tests/unit/test_pause_while_running_capability.py`

**Interfaces:**
- Consumes: the session-level harness style of `tests/integration/test_cpp_session.py` / `test_gdb_session.py` (read both first — they show how a cpp debuggee is compiled and launched through `DebugController`, and how gdb/lldb-dap availability is skipped).
- Produces: evidence-based decision — `pause_while_running=True` on the cpp profile only if the test passes against at least lldb-dap.

- [ ] **Step 1: Write the verification test**

Model it on the launch test in `tests/integration/test_cpp_session.py` (fixtures, availability skips, compile step). The debuggee:

```c
/* spin.c */
#include <unistd.h>
int main(void) {
    volatile long i = 0;
    for (;;) { i++; usleep(1000); }
    return 0;
}
```

Test body (adapt fixture names to the existing cpp tests):

```python
async def test_pause_stops_running_cpp_loop(cpp_controller_running_spin):
    controller = cpp_controller_running_spin  # launched, NOT stopped
    ok = await controller.pause(timeout=10.0)
    assert ok is True
    await controller.fetch_stop_info()
    assert controller.state.stack_frames
```

- [ ] **Step 2: Run it against each installed cpp adapter**

Run: `python -m pytest tests/integration/test_cpp_pause.py -v` (the existing cpp tests' skip guards handle machines without gdb/lldb-dap).

- [ ] **Step 3: Decide per the spec (no partial enablement)**

- **Pass on lldb-dap (and gdb if installed):** set `capabilities=ProfileCapabilities(pause_while_running=True)` in `src/tdb/languages/cpp.py`, and add a cpp assertion to `tests/unit/test_pause_while_running_capability.py`. If Task 7's `test_run_requires_pause_capable_language` used `--lang cpp` as its unsupported example, switch that test to the fake-profile approach described there.
- **Fail or flaky:** leave the flag False, mark the new test `@pytest.mark.skip(reason="cpp adapters do not reliably honor pause while running; --run stays disabled for cpp")`, and keep the CLI error as the user experience.

- [ ] **Step 4: Run the affected suites**

Run: `python -m pytest tests/integration/test_cpp_pause.py tests/unit/test_pause_while_running_capability.py tests/unit/test_cli_run_flag.py -v`
Expected: PASS (with the decision applied consistently).

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_cpp_pause.py src/tdb/languages/cpp.py tests/unit/test_pause_while_running_capability.py tests/unit/test_cli_run_flag.py
git commit -m "test: verify cpp adapters' pause-while-running; set --run support accordingly"
```

(Adjust the message to state the actual outcome — enabled or left disabled.)

---

### Task 10: Windows Ctrl-C isolation for adapter subprocesses

**Files:**
- Modify: `src/tdb/dap/client.py:83-88`
- Test: `tests/unit/test_dap_client.py` (extend)

**Interfaces:**
- Consumes: the adapter spawn at `dap/client.py:83` (`asyncio.create_subprocess_exec(..., start_new_session=True)`).
- Produces: on Windows the adapter (and its debuggee children) are spawned with `CREATE_NEW_PROCESS_GROUP` so a console `CTRL_C_EVENT` reaches only tdb; POSIX behavior unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_dap_client.py` (follow its existing fake/spawn-capture style; if it has no spawn test, add a monkeypatch-based one):

```python
async def test_adapter_spawn_isolated_from_terminal_signals(monkeypatch):
    """POSIX: new session (setsid). Windows: new process group. Either
    way the adapter must not share the terminal's Ctrl-C delivery."""
    import tdb.dap.client as client_mod

    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop before real spawn")

    monkeypatch.setattr(client_mod.asyncio, "create_subprocess_exec", fake_exec)
    from tdb.languages.python import build_python_profile

    c = client_mod.DAPClient(build_python_profile().adapter)
    with pytest.raises(RuntimeError):
        await c.start()

    import os

    if os.name == "nt":
        import subprocess

        assert captured.get("creationflags") == subprocess.CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in captured
    else:
        assert captured.get("start_new_session") is True
```

(Verify `DAPClient.start()`'s actual name/signature — `grep -n "def start" src/tdb/dap/client.py` — and adapt the call; the assertion block is the point.)

- [ ] **Step 2: Run test to verify current behavior**

Run: `python -m pytest tests/unit/test_dap_client.py -v`
Expected on Linux: the new test PASSES already (`start_new_session=True` is present) — the change is Windows-only, so on POSIX this is a pin-the-behavior test. Confirm it fails only on the Windows branch logic by inspection.

- [ ] **Step 3: Implement**

At the spawn site in `src/tdb/dap/client.py`:

```python
        spawn_kwargs: dict = {}
        if os.name == "nt":
            import subprocess

            # A console CTRL_C_EVENT is delivered to every process in
            # the console's group; a new group keeps run-mode Ctrl-C
            # (and TUI-mode terminal signals generally) tdb-only.
            spawn_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # isolate from terminal so it can't interfere with TUI
            spawn_kwargs["start_new_session"] = True
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_kwargs,
        )
```

(Keep the existing positional/keyword arguments of the current call — only the isolation kwargs change. Add `import os` if the module lacks it.)

Audit (read, no code change expected): `src/tdb/adapters/bash/session.py:255-264` and the tcsh spawn in `src/tdb/adapters/tcsh/session.py` already use `start_new_session=True`/process groups and are POSIX-only adapters — note findings in the commit body.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_dap_client.py tests/integration/test_dap_session.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tdb/dap/client.py tests/unit/test_dap_client.py
git commit -m "fix: Windows adapter spawn uses CREATE_NEW_PROCESS_GROUP for Ctrl-C isolation"
```

---

### Task 11: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: final behavior from Tasks 1-10.

- [ ] **Step 1: Add a "Run mode (`--run`)" section to README.md**

Place it near the remote-attach / headless sections. Content to cover (write it in the README's existing voice, with a short example):

- Purpose: inspect programs that appear hung, without paying for a TUI or breakpoints up front.
- Usage: `tdb --run myprog.py args...`; output streams to the terminal; tdb exits with the program's exit code.
- Interrupting: Ctrl-C anywhere; `kill -USR1 <tdb pid>` from another terminal on Unix. The debuggee itself never receives these signals — its own SIGINT handlers are not disturbed.
- The TUI opens at the currently executing line; full debugging available (breakpoints, stepping, evaluate).
- Quitting: `d` detach & resume (interrupt again later; a breakpoint set during the episode also reopens the TUI when hit), `t` terminate.
- Supported languages: python, perl, bash, tcsh (plus cpp if Task 9 enabled it); others get a CLI error.
- Limitation: pausing is cooperative — a hang inside one blocking external call surfaces only when that call returns; tdb prints a notice and opens the TUI when the stop eventually lands.
- Incompatible flags: `-r`, `-k`/`-t`, `--record`, `--replay`, `--server`, `--headless`, `--mcp`, `--terminal`.

- [ ] **Step 2: Verify the README renders**

Run: `python -m tdb --doc-text | grep -A5 "Run mode"`
Expected: the new section appears, wrapped cleanly.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document --run mode and signal-triggered debugging"
```

---

## Final verification

- [ ] `python -m pytest tests/unit tests/integration -q` — full suite green.
- [ ] Manual smoke (POSIX): `python -m tdb --run <looping script>` → Ctrl-C opens TUI at the running line → set a breakpoint → `q`,`d` detaches and the program resumes → breakpoint reopens the TUI → `q`,`t` terminates.
- [ ] Manual smoke: `python -m tdb --run <script that exits 3>; echo $?` prints 3.
- [ ] Spec cross-check: every section of `docs/superpowers/specs/2026-08-15-run-mode-signal-tui-design.md` maps to a completed task.
