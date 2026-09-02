"""tests/unit/test_go_workflows.py

open_threads dispatch: a Go profile routes to the goroutine workspace,
and falls back to the generic thread list when snapshot collection
fails. Uses the same App-stubbing style as the existing inspection
workflow tests (see tests/unit/test_* for ThreadsModal workflows; if
none exists, this stub-based approach stands alone).
"""

import pytest

from tdb.app_handlers.inspection import InspectionWorkflows
from tdb.session.inspect_service import SessionGateError


class _Caps:
    concurrency_inspection = "go"
    classify_threads = None
    task_inspection = False


class _Profile:
    capabilities = _Caps()
    display_name = "Go"

    class presentation:
        frame_name = None


class _State:
    is_terminated = False
    is_running = False
    current_thread_id = 1
    threads = []


class _Ctrl:
    profile = _Profile()
    state = _State()


class _App:
    def __init__(self):
        self.controller = _Ctrl()
        self.pushed = []
        self.notifications = []

        class _Panels:
            goroutines = None
            threads = None

        self.panels = _Panels()

    def push_screen(self, modal, callback=None):
        self.pushed.append(modal)

    def notify(self, *a, **k):
        self.notifications.append(a)


@pytest.mark.asyncio
async def test_open_threads_routes_to_goroutines(monkeypatch):
    app = _App()
    wf = InspectionWorkflows(app)

    from tdb.go_concurrency.models import GoroutineSnapshot

    snap = GoroutineSnapshot((), (), (), (), 0, ())

    async def fake_collect():
        return snap

    monkeypatch.setattr(wf._svc, "collect_go_concurrency", fake_collect)
    await wf.open_threads()
    assert len(app.pushed) == 1
    from tdb.widgets.goroutines_modal import GoroutinesModal

    assert isinstance(app.pushed[0], GoroutinesModal)
    assert app.panels.goroutines is app.pushed[0]


@pytest.mark.asyncio
async def test_open_threads_falls_back_when_snapshot_fails(monkeypatch):
    app = _App()
    wf = InspectionWorkflows(app)

    async def boom():
        raise RuntimeError("collector broke")

    async def fake_list_threads():
        return []  # fallback path: "No threads found" notification

    monkeypatch.setattr(wf._svc, "collect_go_concurrency", boom)
    monkeypatch.setattr(wf._svc, "list_threads", fake_list_threads)
    await wf.open_threads()
    assert app.pushed == []  # nothing opened
    assert app.notifications  # but the user heard about it
