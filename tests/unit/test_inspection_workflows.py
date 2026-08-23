"""Unit tests for the InspectionWorkflows collaborator (#1c split).

These tests don't spin up the full Textual app — they construct an
InspectionWorkflows with a stub `app` whose `controller`, `notify`,
and `query_one` are recorded for later assertion. This proves the
state guards and method boundaries work without paying for a TUI.
"""

from __future__ import annotations

from types import SimpleNamespace

from tdb.app_handlers.inspection import InspectionWorkflows
from tdb.app_handlers.ui_panels import UIPanels
from tdb.dap.types import Thread
from tdb.languages.rust import build_rust_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.inspect_service import SessionGateError
from tdb.session.state import SessionPhase
from tdb.widgets.rust_concurrency_modal import RustConcurrencyModal
from tdb.widgets.threads_modal import ThreadsModal
from tests.unit.test_rust_concurrency_modal import sample_snapshot


class _StubMenuBar:
    def __init__(self) -> None:
        self.labels: dict[str, str] = {}

    def update_action_label(self, action_id: str, text: str) -> None:
        self.labels[action_id] = text


class _StubApp:
    """The minimum surface InspectionWorkflows reaches for."""

    def __init__(self, profile=None) -> None:
        self.controller = DebugController(ServerEventHandler(), profile=profile)
        # Start the stub session in STOPPED so guards behave like a
        # paused-and-ready debugger; tests flip to RUNNING/TERMINATED as
        # they need to exercise specific guards.
        self.controller.state.transition_to(SessionPhase.STOPPED)
        self._menu_bar = _StubMenuBar()
        self.notifications: list[tuple[str, str]] = []
        self.pushed_screens: list[object] = []
        self.panels = UIPanels()

    def query_one(self, selector, _type=None):
        if selector == "#menu-bar":
            return self._menu_bar
        raise AssertionError(f"unexpected query_one({selector!r})")

    def notify(self, msg, *, title="", severity="information"):
        self.notifications.append((title, msg))

    def push_screen(self, screen, callback=None):
        self.pushed_screens.append(screen)


def _wf(profile=None) -> tuple[InspectionWorkflows, _StubApp]:
    app = _StubApp(profile=profile)
    return InspectionWorkflows(app), app


async def test_rust_threads_action_opens_concurrency_workspace(monkeypatch):
    """Rust's advertised concurrency capability must bypass ThreadsModal."""
    workflow, app = _wf(profile=build_rust_profile(adapter="lldb-dap"))
    snapshot = sample_snapshot()

    async def collect_rust_concurrency():
        return snapshot

    monkeypatch.setattr(
        workflow._svc,
        "collect_rust_concurrency",
        collect_rust_concurrency,
    )

    await workflow.open_threads()

    assert isinstance(app.panels.rust_concurrency, RustConcurrencyModal)
    assert app.panels.threads is None


async def test_non_rust_threads_action_keeps_generic_threads_modal(monkeypatch):
    """A capability check, not a binary heuristic, preserves every fallback profile."""
    workflow, app = _wf()

    async def list_threads():
        return [Thread(id=1, name="MainThread")]

    monkeypatch.setattr(workflow._svc, "list_threads", list_threads)

    await workflow.open_threads()

    assert isinstance(app.panels.threads, ThreadsModal)
    assert app.panels.rust_concurrency is None


# --- State guards ------------------------------------------------------


async def test_open_async_tasks_rejects_terminated():
    wf, app = _wf()
    app.controller.state.transition_to(SessionPhase.TERMINATED)
    await wf.open_async_tasks()
    assert app.notifications and "terminated" in app.notifications[0][1].lower()
    assert app.pushed_screens == []


async def test_open_async_tasks_rejects_running():
    wf, app = _wf()
    app.controller.state.transition_to(SessionPhase.RUNNING)
    await wf.open_async_tasks()
    assert app.notifications and "running" in app.notifications[0][1].lower()


async def test_open_threads_rejects_terminated():
    wf, app = _wf()
    app.controller.state.transition_to(SessionPhase.TERMINATED)
    await wf.open_threads()
    assert app.notifications and "terminated" in app.notifications[0][1].lower()


async def test_fetch_async_task_count_is_a_no_op_when_terminated():
    wf, app = _wf()
    app.controller.state.transition_to(SessionPhase.TERMINATED)
    await wf.fetch_async_task_count()
    # Menu bar must NOT have been touched — no DAP eval happened.
    assert app._menu_bar.labels == {}


async def test_fetch_process_count_is_a_no_op_when_running():
    wf, app = _wf()
    app.controller.state.transition_to(SessionPhase.RUNNING)
    await wf.fetch_process_count()
    assert app._menu_bar.labels == {}


# --- Capability gate (task_inspection) -----------------------------------
#
# Both count-badge fetchers fire on every `stopped` event regardless of
# profile (see app_handlers/dap_events.py). For a profile that doesn't
# support task inspection (e.g. cpp), they must not send a Python-only
# evaluate expression against the debuggee.


async def test_fetch_async_task_count_noop_for_gated_profile():
    from tests.unit.test_controller_actions import _bare_profile

    wf, app = _wf(profile=_bare_profile())

    calls = []

    async def _fake_evaluate_on_parent(expr):
        calls.append(expr)
        return "0"

    app.controller.evaluate_on_parent = _fake_evaluate_on_parent

    await wf.fetch_async_task_count()

    assert calls == []
    assert app._menu_bar.labels == {}


async def test_fetch_process_count_noop_for_gated_profile():
    from tests.unit.test_controller_actions import _bare_profile

    wf, app = _wf(profile=_bare_profile())

    calls = []

    async def _fake_evaluate_on_parent(expr):
        calls.append(expr)
        return "0"

    app.controller.evaluate_on_parent = _fake_evaluate_on_parent

    await wf.fetch_process_count()

    assert calls == []
    assert app._menu_bar.labels == {}


# --- Thread-count label thresholding (no DAP needed) -------------------


def test_update_thread_count_below_threshold():
    """1 thread → menu shows just 'Threads' without a count."""
    wf, app = _wf()
    # state.threads is a list of Thread objects; we only need len() here.
    app.controller.state.threads = [SimpleNamespace(id=1, name="Main")]
    wf.update_thread_count()
    assert app._menu_bar.labels["threads-label"] == "Threads"


def test_update_thread_count_at_or_above_threshold():
    wf, app = _wf()
    app.controller.state.threads = [
        SimpleNamespace(id=1, name="Main"),
        SimpleNamespace(id=2, name="Worker"),
        SimpleNamespace(id=3, name="Helper"),
    ]
    wf.update_thread_count()
    assert app._menu_bar.labels["threads-label"] == "Threads (3)"


# --- Process-modal open guard -----------------------------------------


def test_open_processes_modal_rejected_when_terminated_returns_false():
    wf, app = _wf()
    app.controller.state.transition_to(SessionPhase.TERMINATED)
    assert wf.open_processes_modal() is False
    assert app.pushed_screens == []
    assert app.notifications  # toast was shown


def test_open_processes_modal_returns_true_when_runnable():
    wf, app = _wf()
    assert wf.open_processes_modal() is True
    assert len(app.pushed_screens) == 1
    # Modal is now registered in the UIPanels holder for cross-handler access.
    assert app.panels.processes is not None


# --- Unsupported-profile double-toast regression -----------------------


class _StubProcessesModal:
    """Records dismiss()/update_processes() calls without needing a
    real Textual screen (ProcessesModal.dismiss() requires a mounted
    app; the worker-branch logic under test doesn't care which modal
    type it's holding)."""

    def __init__(self) -> None:
        self.dismissed_with: object = "not-called"
        self.updated_with: object = "not-called"

    def dismiss(self, result: object = None) -> None:
        self.dismissed_with = result

    def update_processes(self, processes) -> None:
        self.updated_with = processes


async def test_get_processes_returns_none_for_unsupported_profile(monkeypatch):
    """get_processes() must distinguish 'unsupported' (None) from
    'genuinely no child processes' ([]) so callers can react
    differently."""
    wf, app = _wf()

    async def fake_collect_processes():
        raise SessionGateError("unsupported")

    monkeypatch.setattr(wf._svc, "collect_processes", fake_collect_processes)

    result = await wf.get_processes()

    assert result is None
    assert len(app.notifications) == 1
    assert "not available" in app.notifications[0][1].lower()


async def test_open_processes_worker_single_toast_when_unsupported(monkeypatch):
    """Regression: previously get_processes() notified 'Not available
    for <lang>' AND open_processes_worker() notified 'No extra
    processes' — two contradictory toasts. Only the warning should
    fire, and the modal should still be dismissed quietly."""
    wf, app = _wf()
    modal = _StubProcessesModal()
    app.panels.processes = modal

    async def fake_collect_processes():
        raise SessionGateError("unsupported")

    monkeypatch.setattr(wf._svc, "collect_processes", fake_collect_processes)

    await wf.open_processes_worker()

    assert modal.dismissed_with is None
    assert len(app.notifications) == 1
    title, msg = app.notifications[0]
    assert title == "Processes"
    assert "not available" in msg.lower()
