"""Unit tests for DebugController.pause() timeout behavior.

The pause path is fire-and-forget at the DAP level — we send the
request, then the debuggee suspends asynchronously via a `stopped`
event. For programs whose main thread is blocked outside Python (the
canonical case: a fully-deadlocked asyncio loop in epoll_wait),
debugpy never delivers the pause and the user sees no response.

These tests pin down the timeout-bool contract so the TUI/RPC can
distinguish "pause landed" from "pause vanished into the void".
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.dap.messages import Event
from tdb.session.controller import DebugController
from tdb.session.event_bus import DebugEventHandler
from tdb.session.state import SessionPhase


class _NoopHandler(DebugEventHandler):
    def on_initialized(self): ...
    def on_stopped(self, *a, **k): ...
    def on_continued(self): ...
    def on_terminated(self): ...
    def on_exited(self, *a, **k): ...
    def on_output(self, *a, **k): ...
    def on_external_terminal_started(self): ...


class _StubDAPClient:
    """Captures pause() calls so we can drive the test by hand."""
    def __init__(self) -> None:
        self.pause_calls: list[int] = []

    async def pause(self, thread_id: int) -> None:
        self.pause_calls.append(thread_id)

    async def threads(self) -> list:
        return []


def _make_controller(thread_id: int | None = 1) -> DebugController:
    ctrl = DebugController(_NoopHandler())
    ctrl.client = _StubDAPClient()
    ctrl.state.transition_to(SessionPhase.RUNNING)
    if thread_id is not None:
        ctrl.state.current_thread_id = thread_id
    return ctrl


def _stopped_event() -> Event:
    return Event(seq=1, event="stopped", body={"reason": "pause", "threadId": 1})


# --- Already in a terminal state -------------------------------------

async def test_pause_returns_false_when_terminated():
    ctrl = _make_controller()
    ctrl.state.transition_to(SessionPhase.TERMINATED)
    assert await ctrl.pause(timeout=0.1) is False
    assert ctrl.client.pause_calls == []  # no DAP request when terminal


async def test_pause_returns_true_when_already_stopped():
    """If the program is already paused, pause() is a no-op success."""
    ctrl = _make_controller()
    ctrl.state.transition_to(SessionPhase.STOPPED)
    assert await ctrl.pause(timeout=0.1) is True
    assert ctrl.client.pause_calls == []  # no redundant DAP request


async def test_pause_returns_false_when_no_thread_id():
    """Before stop-on-entry has fired, current_thread_id is None — we
    have no thread to target, so pause is a no-op false."""
    ctrl = _make_controller(thread_id=None)
    assert await ctrl.pause(timeout=0.1) is False
    assert ctrl.client.pause_calls == []


# --- Successful and failed timeouts -----------------------------------

async def test_pause_returns_true_when_stopped_event_arrives():
    """The successful path: send pause, simulate the stopped event,
    confirm pause() unblocks with True."""
    ctrl = _make_controller()

    async def fire_stopped_soon():
        await asyncio.sleep(0.01)
        ctrl._on_stopped(_stopped_event())

    asyncio.create_task(fire_stopped_soon())
    assert await ctrl.pause(timeout=1.0) is True
    assert ctrl.client.pause_calls == [1]


async def test_pause_returns_false_on_timeout():
    """The deadlocked-asyncio case: pause request goes out but no
    stopped event ever arrives. Must time out and return False."""
    ctrl = _make_controller()
    assert await ctrl.pause(timeout=0.05) is False
    assert ctrl.client.pause_calls == [1]  # request was sent


async def test_pause_returns_true_when_terminated_event_arrives():
    """If the program terminates while we're waiting (e.g. user hit
    Ctrl+C in the debuggee), the wait should still unblock so the
    caller can see is_terminated and react."""
    ctrl = _make_controller()

    async def fire_terminated_soon():
        await asyncio.sleep(0.01)
        ctrl._on_terminated(Event(seq=1, event="terminated", body={}))

    asyncio.create_task(fire_terminated_soon())
    # pause() returns True because _stopped_event was set; the caller
    # then checks state.is_terminated to decide what to do next.
    result = await ctrl.pause(timeout=1.0)
    assert result is True
    assert ctrl.state.is_terminated is True


# --- Stale event guard -------------------------------------------------

async def test_pause_clears_event_before_waiting():
    """A stale set() from a previous stop must not make pause() return
    True instantly without ever hitting the wire. We force-set the
    event to simulate that, then verify pause clears it before sending
    the request and waiting."""
    ctrl = _make_controller()
    ctrl._stopped_event.set()  # stale set from some earlier stop

    # No-one will fire a fresh stopped event, so this must time out.
    result = await ctrl.pause(timeout=0.05)
    assert result is False
    # And the DAP request did go out (proving pause didn't short-
    # circuit on the stale event).
    assert ctrl.client.pause_calls == [1]


# --- Continue clears the event ----------------------------------------

async def test_continued_event_clears_stopped_event():
    """After a continue, _stopped_event must be cleared so the next
    pause() actually waits for a fresh stopped event."""
    ctrl = _make_controller()
    ctrl._stopped_event.set()
    ctrl._on_continued(Event(seq=1, event="continued", body={}))
    assert ctrl._stopped_event.is_set() is False
