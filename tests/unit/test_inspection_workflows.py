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
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


class _StubMenuBar:
    def __init__(self) -> None:
        self.labels: dict[str, str] = {}

    def update_action_label(self, action_id: str, text: str) -> None:
        self.labels[action_id] = text


class _StubApp:
    """The minimum surface InspectionWorkflows reaches for."""

    def __init__(self) -> None:
        self.controller = DebugController(ServerEventHandler())
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


def _wf() -> tuple[InspectionWorkflows, _StubApp]:
    app = _StubApp()
    return InspectionWorkflows(app), app


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
