"""Run mode pauses a debuggee that has never stopped (current_thread_id
is None), pushes breakpoints when the TUI adopts a live session, and
never offers restart for adopted sessions."""

import asyncio

import pytest

from tdb.dap.types import StackFrame, Thread
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


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
    from tdb.dap.types import SourceBreakpoint

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


async def test_resolve_frame_falls_back_to_live_top_frame_when_no_stack(
    controller, monkeypatch
):
    """Headless run-mode examine pauses the debuggee without a TUI ever
    fetching a stack, so state.current_frame_id stays None. evaluate must
    still resolve a real frame from the stopped thread."""
    controller.state.enter_stop(7, "pause")
    assert controller.state.current_frame_id is None
    seen = []

    async def fake_stack_trace(thread_id, **kw):
        seen.append(thread_id)
        return [StackFrame(id=99, name="f")]

    async def fake_threads():
        raise AssertionError("threads() should not be queried; thread is known")

    monkeypatch.setattr(controller.client, "stack_trace", fake_stack_trace)
    monkeypatch.setattr(controller.client, "threads", fake_threads)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result == 99
    assert seen == [7]


async def test_resolve_frame_queries_threads_when_no_thread_known(
    controller, monkeypatch
):
    controller.state.transition_to(SessionPhase.STOPPED)
    assert controller.state.current_thread_id is None
    seen = []

    async def fake_threads():
        return [Thread(id=3, name="MainThread")]

    async def fake_stack_trace(thread_id, **kw):
        seen.append(thread_id)
        return [StackFrame(id=42, name="g")]

    monkeypatch.setattr(controller.client, "threads", fake_threads)
    monkeypatch.setattr(controller.client, "stack_trace", fake_stack_trace)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result == 42
    assert seen == [3]


async def test_resolve_frame_returns_none_when_not_started(controller, monkeypatch):
    """Nothing to resolve against unless the session is STOPPED; a
    NOT_STARTED (or RUNNING) controller must return None without making
    any DAP requests."""
    assert controller.state.phase is SessionPhase.NOT_STARTED

    async def fake_threads():
        raise AssertionError("must not be called")

    async def fake_stack_trace(thread_id, **kw):
        raise AssertionError("must not be called")

    monkeypatch.setattr(controller.client, "threads", fake_threads)
    monkeypatch.setattr(controller.client, "stack_trace", fake_stack_trace)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result is None


async def test_resolve_frame_returns_none_when_lookup_fails(controller, monkeypatch):
    assert controller.state.current_thread_id is None

    async def fake_threads():
        raise RuntimeError("adapter gone")

    monkeypatch.setattr(controller.client, "threads", fake_threads)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result is None


async def test_resolve_frame_prefers_cached_frame(controller, monkeypatch):
    controller.state.set_stack([StackFrame(id=5, name="h")], current_frame_id=5)

    async def fake_stack_trace(thread_id, **kw):
        raise AssertionError("should not fetch a live frame; a cached one exists")

    monkeypatch.setattr(controller.client, "stack_trace", fake_stack_trace)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result == 5


async def test_resolve_frame_returns_none_while_running(controller, monkeypatch):
    """The TUI's evaluate console and completions call this on every
    keystroke, including while the debuggee is RUNNING (the cached frame
    id is cleared on every continue). There is nothing to resolve against
    while running, and issuing threads()/stackTrace() round-trips in that
    state would block on a 30s DAP timeout for no benefit."""
    controller.state.transition_to(SessionPhase.RUNNING)

    async def fake_threads():
        raise AssertionError("must not be called")

    async def fake_stack_trace(thread_id, **kw):
        raise AssertionError("must not be called")

    monkeypatch.setattr(controller.client, "threads", fake_threads)
    monkeypatch.setattr(controller.client, "stack_trace", fake_stack_trace)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result is None


async def test_resolve_frame_returns_none_when_no_threads(controller, monkeypatch):
    assert controller.state.current_thread_id is None

    async def fake_threads():
        return []

    monkeypatch.setattr(controller.client, "threads", fake_threads)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result is None


async def test_resolve_frame_returns_none_when_stack_trace_fails(
    controller, monkeypatch
):
    controller.state.enter_stop(7, "pause")
    assert controller.state.current_frame_id is None

    async def fake_stack_trace(thread_id, **kw):
        raise RuntimeError("adapter gone")

    monkeypatch.setattr(controller.client, "stack_trace", fake_stack_trace)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result is None


async def test_resolve_frame_returns_none_when_stack_trace_empty(
    controller, monkeypatch
):
    controller.state.enter_stop(7, "pause")
    assert controller.state.current_frame_id is None

    async def fake_stack_trace(thread_id, **kw):
        return []

    monkeypatch.setattr(controller.client, "stack_trace", fake_stack_trace)
    result = await controller.resolve_evaluate_frame_id(controller.client)
    assert result is None
