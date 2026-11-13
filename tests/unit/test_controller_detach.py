"""Unit tests for DebugController.strip_stops / interrupt_debuggee."""

from __future__ import annotations

import dataclasses
import os
import signal

import pytest

from tdb.dap.client import DAPError
from tdb.dap.messages import Event
from tdb.dap.types import SourceBreakpoint
from tdb.session.state import SessionPhase

from tests.unit.test_controller_actions import _FakeDAP, _make


def _with_bps(ctrl):
    ctrl.state.breakpoints["/a.py"] = [SourceBreakpoint(line=1)]
    ctrl.state.breakpoints["/b.py"] = [SourceBreakpoint(line=2)]


def _set_adapter(monkeypatch, ctrl, *, prep=(), route=None):
    adapter = ctrl.profile.adapter
    monkeypatch.setattr(adapter, "detach_prep_commands", lambda: tuple(prep))
    monkeypatch.setattr(
        adapter, "quirks", dataclasses.replace(adapter.quirks, interrupt_route=route)
    )


async def test_strip_stops_clears_source_function_exception_bps():
    ctrl, fake, _ = _make()
    _with_bps(ctrl)
    await ctrl.strip_stops()
    assert fake.calls_to("setBreakpoints") == [
        ("setBreakpoints", "/a.py", ()),
        ("setBreakpoints", "/b.py", ()),
    ]
    assert fake.calls_to("setFunctionBreakpoints") == [("setFunctionBreakpoints", ())]
    assert fake.calls_to("setExceptionBreakpoints") == [
        ("setExceptionBreakpoints", ())
    ]
    assert [bp.line for bp in ctrl.state.breakpoints["/a.py"]] == [1]
    assert [bp.line for bp in ctrl.state.breakpoints["/b.py"]] == [2]


async def test_strip_stops_reaches_child_clients():
    ctrl, _, _ = _make()
    _with_bps(ctrl)
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    await ctrl.strip_stops()
    assert len(child.calls_to("setBreakpoints")) == 2
    assert child.calls_to("setFunctionBreakpoints")
    assert child.calls_to("setExceptionBreakpoints")


async def test_strip_stops_survives_failing_request(monkeypatch):
    ctrl, fake, _ = _make()
    _set_adapter(monkeypatch, ctrl, prep=("x",))

    async def boom(filters):
        raise DAPError("nope")

    fake.set_exception_breakpoints = boom
    await ctrl.strip_stops()
    assert fake.calls_to("evaluate") == [("evaluate", "x", None)]


async def test_strip_stops_runs_detach_prep_commands(monkeypatch):
    ctrl, fake, _ = _make()
    _set_adapter(monkeypatch, ctrl, prep=("x", "y"))
    await ctrl.strip_stops()
    assert [c[1] for c in fake.calls_to("evaluate")] == ["x", "y"]


async def test_strip_stops_noop_when_terminated():
    ctrl, fake, _ = _make()
    _with_bps(ctrl)
    ctrl.state.phase = SessionPhase.TERMINATED
    assert ctrl.state.is_terminated
    await ctrl.strip_stops()
    assert fake.calls == []


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.pid = 1
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)


async def test_interrupt_uses_pipe_launcher():
    from tdb.session.terminal import PipeLauncher

    ctrl, _, _ = _make()
    launcher = PipeLauncher(lambda t, c: None)
    launcher._process = _FakeProc()
    ctrl._pipe_launcher = launcher
    assert await ctrl.interrupt_debuggee() is True
    expected = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
    assert launcher._process.signals == [expected]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")
async def test_interrupt_prefers_the_program_over_the_launcher(monkeypatch):
    from tdb.session.terminal import PipeLauncher

    ctrl, _, _ = _make()
    launcher = PipeLauncher(lambda t, c: None)
    launcher._process = _FakeProc()
    launcher._process.pid = 100
    ctrl._pipe_launcher = launcher
    ctrl._debuggee_pid = 4242
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert await ctrl.interrupt_debuggee() is True
    assert killed == [(4242, signal.SIGINT)]
    assert launcher._process.signals == []


async def test_interrupt_pipe_launcher_without_process_is_false():
    from tdb.session.terminal import PipeLauncher

    ctrl, _, _ = _make()
    ctrl._pipe_launcher = PipeLauncher(lambda t, c: None)
    assert await ctrl.interrupt_debuggee() is False


async def test_interrupt_pid_route_uses_process_event_pid(monkeypatch):
    ctrl, fake, _ = _make()
    _set_adapter(monkeypatch, ctrl, route="pid")
    ctrl._setup_event_handlers()
    for h in fake._event_handlers["process"]:
        h(Event(seq=1, event="process", body={"systemProcessId": 4242}))
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert await ctrl.interrupt_debuggee() is True
    assert killed == [(4242, signal.SIGINT)]


async def test_interrupt_request_route_sends_signal(monkeypatch):
    ctrl, fake, _ = _make()
    _set_adapter(monkeypatch, ctrl, route="request")
    sent = []

    async def send_signal(name="SIGINT"):
        sent.append(name)

    fake.send_signal = send_signal
    assert await ctrl.interrupt_debuggee() is True
    assert sent == ["SIGINT"]


async def test_interrupt_request_route_failure_returns_false(monkeypatch):
    ctrl, fake, _ = _make()
    _set_adapter(monkeypatch, ctrl, route="request")

    async def send_signal(name="SIGINT"):
        raise DAPError("no")

    fake.send_signal = send_signal
    assert await ctrl.interrupt_debuggee() is False


async def test_interrupt_without_route_returns_false(monkeypatch):
    ctrl, _, _ = _make()
    _set_adapter(monkeypatch, ctrl, route=None)
    assert await ctrl.interrupt_debuggee() is False


async def test_interrupt_pid_route_without_pid_returns_false(monkeypatch):
    ctrl, _, _ = _make()
    _set_adapter(monkeypatch, ctrl, route="pid")
    assert await ctrl.interrupt_debuggee() is False


async def test_interrupt_never_raises(monkeypatch):
    ctrl, _, _ = _make()
    _set_adapter(monkeypatch, ctrl, route="pid")
    ctrl._debuggee_pid = 99

    def boom(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", boom)
    assert await ctrl.interrupt_debuggee() is False


async def test_strip_stops_pending_run_to_cursor_does_not_rearm_bps():
    ctrl, fake, _ = _make()
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    ctrl.state.breakpoints["/a.py"] = [
        SourceBreakpoint(line=1),
        SourceBreakpoint(line=9),  # temp run-to-cursor bp
    ]
    ctrl._run_to_cursor_cleanup = ("/a.py", 9)
    await ctrl.strip_stops()
    for client in (fake, child):
        sent = [c for c in client.calls_to("setBreakpoints") if c[1] == "/a.py"]
        assert sent and sent[-1] == ("setBreakpoints", "/a.py", ())


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")
@pytest.mark.parametrize("bad", [0, 1, -1, -4242, True, False, "7", None])
async def test_untrustworthy_process_pid_is_never_stored_or_signalled(monkeypatch, bad):
    ctrl, fake, _ = _make()
    _set_adapter(monkeypatch, ctrl, route="pid")
    ctrl._setup_event_handlers()
    for h in fake._event_handlers["process"]:
        h(Event(seq=1, event="process", body={"systemProcessId": bad}))
    assert ctrl._debuggee_pid is None
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert await ctrl.interrupt_debuggee() is False
    # Even a bad value planted directly is refused right before os.kill.
    ctrl._debuggee_pid = bad if isinstance(bad, int) else 0
    assert await ctrl.interrupt_debuggee() is False
    assert killed == []


@pytest.mark.parametrize("name", ["exited", "terminated"])
async def test_debuggee_pid_cleared_when_session_ends(monkeypatch, name):
    ctrl, fake, _ = _make()
    _set_adapter(monkeypatch, ctrl, route="pid")
    ctrl._setup_event_handlers()
    for h in fake._event_handlers["process"]:
        h(Event(seq=1, event="process", body={"systemProcessId": 4242}))
    assert ctrl._debuggee_pid == 4242
    for h in fake._event_handlers[name]:
        h(Event(seq=2, event=name, body={"exitCode": 0}))
    assert ctrl._debuggee_pid is None
