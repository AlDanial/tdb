"""Unit tests pinning down state propagation on DAP termination events.

Regression: the DAP `terminated` and `exited` events used to delegate
to the per-app event handler without updating `state.is_terminated`.
That left headless-mode RPC guards (and any other code reading state
between the terminated event and an explicit `controller.stop()`) with
a stale view of the world. These tests cover both the producer
(controller) and the all-paths audit list.
"""

from __future__ import annotations

from tdb.dap.messages import Event
from tdb.session.controller import DebugController
from tdb.session.event_bus import DebugEventHandler
from tdb.session.state import SessionPhase


class _RecordingHandler(DebugEventHandler):
    def __init__(self):
        self.terminated = 0
        self.exit_code: int | None = None
        self.continued = 0
        self.stopped = 0

    def on_initialized(self): ...
    def on_stopped(self, *a, **k):
        self.stopped += 1

    def on_continued(self):
        self.continued += 1

    def on_terminated(self):
        self.terminated += 1

    def on_exited(self, exit_code: int):
        self.exit_code = exit_code

    def on_output(self, *a, **k): ...
    def on_external_terminal_started(self): ...


def _terminated_event() -> Event:
    return Event(seq=1, event="terminated", body={})


def _exited_event(code: int = 0) -> Event:
    return Event(seq=2, event="exited", body={"exitCode": code})


def test_on_terminated_sets_is_terminated_and_clears_running():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl.state.transition_to(SessionPhase.RUNNING)
    assert ctrl.state.is_terminated is False

    ctrl._on_terminated(_terminated_event())

    assert ctrl.state.is_terminated is True
    assert ctrl.state.is_running is False
    # Handler delegation is preserved — downstream consumers (TUI etc.)
    # still get notified.
    assert h.terminated == 1


def test_on_exited_sets_is_terminated_and_records_exit_code():
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl.state.transition_to(SessionPhase.RUNNING)

    ctrl._on_exited(_exited_event(code=7))

    assert ctrl.state.is_terminated is True
    assert ctrl.state.is_running is False
    assert h.exit_code == 7


def test_either_event_alone_is_sufficient():
    """debugpy can emit `exited` without `terminated` (and vice versa) on
    abrupt failures. Either one alone must put the controller in the
    right state.
    """
    h = _RecordingHandler()
    ctrl = DebugController(h)
    ctrl._on_exited(_exited_event(code=1))
    assert ctrl.state.is_terminated is True

    ctrl2 = DebugController(_RecordingHandler())
    ctrl2._on_terminated(_terminated_event())
    assert ctrl2.state.is_terminated is True


def test_breakpoint_methods_become_no_ops_after_termination():
    """All controller methods that conditionally talk to debugpy guard on
    is_terminated. Verify the guards engage after _on_terminated fires.
    The guards short-circuit BEFORE touching self.client, which we leave
    as the default unconnected DAPClient — any DAP send would raise.
    """
    import asyncio

    h = _RecordingHandler()
    ctrl = DebugController(h)
    # Pretend the session was ready before termination so the guards
    # would otherwise try to send (`is_ready and not is_terminated`).
    ctrl.state.transition_to(SessionPhase.STOPPED)
    ctrl._on_terminated(_terminated_event())

    # These should all return without raising — the guards skip the DAP send.
    asyncio.run(ctrl.add_breakpoint("/x.py", 5))
    asyncio.run(ctrl.remove_breakpoint("/x.py", 5))
    asyncio.run(ctrl.toggle_breakpoint("/x.py", 5))
    asyncio.run(ctrl.disable_all_breakpoints())
    asyncio.run(ctrl.enable_all_breakpoints())
    asyncio.run(ctrl.clear_all_breakpoints())
