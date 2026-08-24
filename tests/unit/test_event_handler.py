"""Unit tests for tdb.server.event_handler.ServerEventHandler."""

from __future__ import annotations

import asyncio

import pytest

from tdb.app_handlers.dap_events import DapEventCoordinator
from tdb.app_handlers.ui_panels import UIPanels
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


def test_initialized_event_set_on_callback():
    h = ServerEventHandler()
    assert not h.initialized_event.is_set()
    h.on_initialized()
    assert h.initialized_event.is_set()


def test_on_stopped_records_details_and_sets_event():
    h = ServerEventHandler()
    h.on_stopped(thread_id=7, reason="breakpoint", description="hit", text="x>0")
    assert h.last_stop_thread_id == 7
    assert h.last_stop_reason == "breakpoint"
    assert h.last_stop_description == "hit"
    assert h.last_stop_text == "x>0"
    assert h.stopped_event.is_set()


def test_on_continued_clears_stopped_event():
    h = ServerEventHandler()
    h.on_stopped(1, "step")
    assert h.stopped_event.is_set()
    h.on_continued()
    assert not h.stopped_event.is_set()


def test_on_terminated_unblocks_stop_waiters():
    h = ServerEventHandler()
    h.on_terminated()
    assert h.terminated_event.is_set()
    # Stopped is also set so any code awaiting wait_for_stop() resolves.
    assert h.stopped_event.is_set()


def test_on_exited_records_code():
    h = ServerEventHandler()
    h.on_exited(42)
    assert h.exit_code == 42


def test_drain_output_clears_buffer():
    h = ServerEventHandler()
    h.on_output("hello\n", "stdout")
    h.on_output("world\n", "stderr")
    assert h.peek_output() == "hello\nworld\n"
    assert h.drain_output() == "hello\nworld\n"
    assert h.peek_output() == ""


def test_on_output_drops_unknown_categories():
    h = ServerEventHandler()
    h.on_output("ignored", "telemetry")
    assert h.peek_output() == ""


async def test_wait_for_stop_returns_true_when_set():
    h = ServerEventHandler()

    async def trigger():
        await asyncio.sleep(0)
        h.on_stopped(1, "breakpoint")

    asyncio.create_task(trigger())
    assert await h.wait_for_stop(timeout=1.0) is True


async def test_wait_for_stop_returns_false_on_timeout():
    h = ServerEventHandler()
    assert await h.wait_for_stop(timeout=0.05) is False


def test_reset_for_continue_clears_stop():
    h = ServerEventHandler()
    h.on_stopped(1, "step")
    h.reset_for_continue()
    assert not h.stopped_event.is_set()


def test_sse_subscribe_receives_event():
    h = ServerEventHandler()
    q = h.subscribe_sse()
    h.on_initialized()
    msg = q.get_nowait()
    assert msg["event"] == "initialized"
    assert "timestamp" in msg


def test_sse_unsubscribe():
    h = ServerEventHandler()
    q = h.subscribe_sse()
    h.unsubscribe_sse(q)
    h.on_initialized()
    with pytest.raises(asyncio.QueueEmpty):
        q.get_nowait()


def test_continued_dismisses_rust_workspace():
    """A continued/step event makes a captured Rust snapshot stale."""
    class _App:
        def __init__(self) -> None:
            self.controller = DebugController(ServerEventHandler())
            self._stderr_buffer: list[str] = []
            self.panels = UIPanels()
            self.ui_state_updates = 0

        def _update_ui_state(self) -> None:
            self.ui_state_updates += 1

    class _Modal:
        def __init__(self) -> None:
            self.dismiss_calls = 0

        def dismiss(self) -> None:
            self.dismiss_calls += 1

    app = _App()
    modal = _Modal()
    app.panels.rust_concurrency = modal  # type: ignore[assignment]
    dap_handler = DapEventCoordinator(app)  # type: ignore[arg-type]

    dap_handler.on_continued()

    assert modal.dismiss_calls == 1
    assert app.panels.rust_concurrency is None
    assert app.ui_state_updates == 1


def test_exited_dismisses_rust_workspace_without_terminated_event():
    """Exited-only adapters must not retain stale Rust snapshot screens."""
    class _App:
        def __init__(self) -> None:
            self.controller = DebugController(ServerEventHandler())
            self._stderr_buffer: list[str] = []
            self.panels = UIPanels()

        def query_one(self, selector, _type=None):
            raise AssertionError("console output is irrelevant to lifecycle cleanup")

    class _Modal:
        def __init__(self) -> None:
            self.dismiss_calls = 0

        def dismiss(self) -> None:
            self.dismiss_calls += 1

    app = _App()
    modal = _Modal()
    app.panels.rust_concurrency = modal  # type: ignore[assignment]

    DapEventCoordinator(app).on_exited(0)  # type: ignore[arg-type]

    assert modal.dismiss_calls == 1
    assert app.panels.rust_concurrency is None
