"""In-process unit tests for McpSession.

McpSession is the third in-process consumer of RpcHandlers (after the
TUI and the FastAPI/HTTP server). These tests pin its responsibilities:

  - Before launch/attach: tool calls surface a clear "no session"
    error instead of an AttributeError.
  - When a session is active: `_call` dispatches through the same
    RpcHandlers.dispatch_table() the HTTP server uses, including the
    NO_LOCK_ACTIONS bypass (so `pause` can interrupt a long step).
  - `quit` is idempotent and lets a fresh launch/attach happen
    afterward in the same MCP process.

We avoid spawning a debugpy subprocess by stitching a fake DAPClient
+ controller into the McpSession after construction — same pattern as
test_rpc_handlers.py.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.dap.types import SourceBreakpoint, Thread
from tdb.mcp.session import McpSession
from tdb.server.event_handler import ServerEventHandler
from tdb.server.handlers import ControllerRef, RpcHandlers
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


# --- Fakes / helpers ----------------------------------------------------


class _FakeDAPClient:
    """Minimal surface — same shape as test_rpc_handlers._FakeDAPClient."""

    def __init__(self) -> None:
        self.set_breakpoints_calls: list[tuple[str, list[SourceBreakpoint]]] = []
        self._threads: list[Thread] = []

    async def set_breakpoints(self, source_path, breakpoints):
        self.set_breakpoints_calls.append((source_path, list(breakpoints)))
        return []

    async def threads(self) -> list[Thread]:
        return list(self._threads)


def _attach_fake_session(sess: McpSession) -> None:
    """Pretend launch() completed: stitch in a fake controller +
    handlers so `_call` can dispatch without a real debug session."""
    eh = ServerEventHandler()
    controller = DebugController(eh)
    controller.client = _FakeDAPClient()
    controller.state.transition_to(SessionPhase.STOPPED)
    sess._controller = controller
    sess._handler = eh
    sess._handlers = RpcHandlers(ControllerRef(controller), eh)


# --- No-session guard ---------------------------------------------------


async def test_call_before_launch_returns_no_session_error():
    sess = McpSession()
    rsp = await sess._call("status", [])
    assert rsp.success is False
    assert "no active debug session" in rsp.value.lower()
    # Recovery hint must name BOTH lifecycle tools so the agent isn't
    # left guessing which one to use.
    assert "debug_launch" in rsp.value
    assert "debug_attach" in rsp.value


async def test_is_active_false_before_launch():
    sess = McpSession()
    assert sess.is_active is False


async def test_quit_is_idempotent_with_no_session():
    sess = McpSession()
    msg = await sess.quit()
    assert "no active" in msg.lower()


# --- Dispatch + lock policy ---------------------------------------------


async def test_call_dispatches_through_handlers():
    sess = McpSession()
    _attach_fake_session(sess)
    rsp = await sess._call("status", [])
    assert rsp.success is True
    assert "stopped" in rsp.value or "unknown" in rsp.value


async def test_call_rejects_unknown_action():
    sess = McpSession()
    _attach_fake_session(sess)
    rsp = await sess._call("bogus", [])
    assert rsp.success is False
    assert "unknown action" in rsp.value.lower()


async def test_pause_bypasses_session_lock():
    """Regression mirror of the HTTP dispatcher test: hold the lock
    (simulating an in-flight `control(action="continue")`), then call
    `pause` and a non-bypassed action concurrently. Pause must come
    back; the other must block on the lock — that's the proof the
    bypass is what made pause go through."""
    sess = McpSession()
    _attach_fake_session(sess)
    assert sess._handlers is not None

    async with sess._handlers.session_lock:
        pause_rsp = await asyncio.wait_for(sess._call("pause", []), timeout=1.0)
        assert pause_rsp.success is True
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sess._call("status", []), timeout=0.2)


async def test_call_serializes_non_bypassed_actions():
    """Two `status` calls in flight must serialize through session_lock.
    The second one waits until the first releases — proves we still
    hold the lock for everything outside NO_LOCK_ACTIONS."""
    sess = McpSession()
    _attach_fake_session(sess)
    assert sess._handlers is not None

    order: list[str] = []
    gate = asyncio.Event()

    original_status = sess._handlers.action_status

    async def _slow_status(params):
        order.append("start")
        await gate.wait()
        order.append("end")
        return await original_status(params)

    sess._handlers.action_status = _slow_status  # type: ignore[assignment]

    t1 = asyncio.create_task(sess._call("status", []))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(sess._call("status", []))
    await asyncio.sleep(0.01)
    # Only the first call should be inside the slow handler.
    assert order == ["start"]
    gate.set()
    await asyncio.gather(t1, t2)
    # Both calls ran; serialization preserved (no interleaving).
    assert order == ["start", "end", "start", "end"]


# --- is_active transitions ---------------------------------------------


async def test_is_active_true_after_fake_attach():
    sess = McpSession()
    _attach_fake_session(sess)
    assert sess.is_active is True


async def test_is_active_false_after_terminated():
    sess = McpSession()
    _attach_fake_session(sess)
    assert sess._controller is not None
    sess._controller.state.transition_to(SessionPhase.TERMINATED)
    assert sess.is_active is False


async def test_launch_rejects_when_already_active():
    sess = McpSession()
    _attach_fake_session(sess)
    msg = await sess.launch(program="anything.py")
    assert "already active" in msg.lower()


async def test_attach_rejects_when_already_active():
    sess = McpSession()
    _attach_fake_session(sess)
    msg = await sess.attach(host="localhost", port=15678)
    assert "already active" in msg.lower()
