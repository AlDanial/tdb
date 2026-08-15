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
