"""Regression: do_configure must not clobber a STOPPED phase.

In remote-attach mode (`tdb.breakpoint()`), the debuggee may already be
paused at the hook by the time `configurationDone` returns and
`_launch_future` resolves. The `_on_stopped` callback runs synchronously
from the DAP read loop and sets `phase=STOPPED` before `do_configure`
finishes. If `do_configure` then unconditionally transitions to RUNNING,
the UI shows "Running" with an empty Code View even though the program is
actually paused — and the user has no way to interact with it.

This file pins both directions:
  - normal launch path: do_configure ends in RUNNING (program is starting),
  - racy stopped-first path: do_configure leaves the existing STOPPED.
"""

from __future__ import annotations

import asyncio

from tdb.dap.messages import Event, Response
from tdb.dap.types import Capabilities
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


class _StubDAPClient:
    """Minimal DAP-client surface that do_configure exercises."""

    def __init__(self) -> None:
        self.events: dict = {}
        self.reverse_handlers: dict = {}
        self.capabilities = Capabilities()

    def on_event(self, name, fn):
        self.events[name] = fn

    def on_reverse_request(self, name, fn):
        self.reverse_handlers[name] = fn

    async def set_exception_breakpoints(self, filters):
        return None

    async def configuration_done(self):
        return None


def _make_controller():
    ctrl = DebugController(ServerEventHandler())
    ctrl.client = _StubDAPClient()  # type: ignore[assignment]
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(
        Response(
            seq=1,
            request_seq=1,
            command="launch",
            success=True,
            body={},
        )
    )
    ctrl._launch_future = fut
    return ctrl


async def test_do_configure_transitions_to_running_when_no_stop_yet():
    """Happy path: nothing has stopped yet, so we should end in RUNNING."""
    ctrl = _make_controller()
    assert ctrl.state.phase == SessionPhase.NOT_STARTED
    await ctrl.do_configure()
    assert ctrl.state.phase == SessionPhase.RUNNING


async def test_do_configure_preserves_stopped_phase_set_before_it_finishes():
    """tdb.breakpoint() race: a stopped event arrives while do_configure
    is mid-flight (or before it runs at all). do_configure must NOT
    overwrite STOPPED with RUNNING — that's the bug that left the UI
    showing 'Running' with an empty Code View when attaching to a program
    paused at tdb.breakpoint()."""
    ctrl = _make_controller()
    # Simulate the early stopped event that the DAP read loop dispatches
    # synchronously via _on_stopped before do_configure's await chain
    # gets to its final transition_to.
    ctrl._on_stopped(
        Event(
            seq=1,
            event="stopped",
            body={"reason": "breakpoint", "threadId": 1},
        )
    )
    assert ctrl.state.phase == SessionPhase.STOPPED

    await ctrl.do_configure()

    assert ctrl.state.phase == SessionPhase.STOPPED, (
        "do_configure clobbered STOPPED -> RUNNING; the UI would now show "
        "'Running' even though the debuggee is paused at tdb.breakpoint()."
    )


async def test_do_configure_preserves_terminated_phase():
    """Belt-and-suspenders: if anything terminal happened during configure
    (TERMINATED set by an early `terminated` event), we don't clobber that
    either. Only NOT_STARTED -> RUNNING is allowed."""
    ctrl = _make_controller()
    ctrl.state.transition_to(SessionPhase.TERMINATED)
    await ctrl.do_configure()
    assert ctrl.state.phase == SessionPhase.TERMINATED
