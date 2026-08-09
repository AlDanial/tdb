"""Contract tests for HandlerRef and the restart-aware swap behavior.

Pins three invariants from #6:

1. RpcHandlers reads `event_handler` through the ref — replacing the
   handler via `HandlerRef.set` is visible to existing handler closures
   immediately, without re-constructing RpcHandlers.

2. Live SSE subscribers migrate from the old handler to the new one,
   so /events connections opened before a restart keep receiving events.

3. `HandlerRef.set` broadcasts a `session_restart` event before any
   events from the new session start arriving, giving clients a clean
   boundary to reset their UI on.
"""

from __future__ import annotations

import asyncio

from tdb.server.event_handler import ServerEventHandler
from tdb.server.handlers import ControllerRef, HandlerRef, RpcHandlers
from tdb.session.controller import DebugController


# --- HandlerRef in isolation -------------------------------------------


def test_handler_ref_holds_initial_handler():
    h = ServerEventHandler()
    ref = HandlerRef(h)
    assert ref.h is h


def test_handler_ref_set_updates_reference():
    h1 = ServerEventHandler()
    h2 = ServerEventHandler()
    ref = HandlerRef(h1)
    ref.set(h2)
    assert ref.h is h2


def test_handler_ref_set_to_same_instance_is_a_noop():
    """Self-assignment must not corrupt subscriber state or emit a
    spurious session_restart."""
    h = ServerEventHandler()
    q = h.subscribe_sse()
    ref = HandlerRef(h)
    ref.set(h)
    assert ref.h is h
    assert q.empty()  # no session_restart broadcast


# --- Subscriber migration ----------------------------------------------


def test_subscribers_migrate_to_new_handler():
    h1 = ServerEventHandler()
    q = h1.subscribe_sse()
    ref = HandlerRef(h1)
    h2 = ServerEventHandler()
    ref.set(h2)

    # The new handler now owns the queue.
    assert q in h2._sse_subscribers
    # The old handler no longer references it (same list object after
    # transfer; old handler effectively orphaned).
    h1_subs = h1._sse_subscribers
    h2_subs = h2._sse_subscribers
    assert h1_subs is h2_subs


def test_session_restart_event_broadcast_on_set():
    h1 = ServerEventHandler()
    q = h1.subscribe_sse()
    ref = HandlerRef(h1)
    ref.set(ServerEventHandler())

    # Existing subscriber should have received a session_restart event
    # so the client can reset its UI.
    assert q.qsize() == 1
    msg = q.get_nowait()
    assert msg["event"] == "session_restart"


def test_post_swap_events_reach_old_subscribers():
    """Events emitted on the new handler must flow to subscribers that
    were attached to the old handler before the swap."""
    h1 = ServerEventHandler()
    q = h1.subscribe_sse()
    ref = HandlerRef(h1)

    h2 = ServerEventHandler()
    ref.set(h2)
    # Drain the session_restart marker.
    q.get_nowait()

    # Now fire a stop on the new handler.
    h2.on_stopped(thread_id=1, reason="breakpoint")
    msg = q.get_nowait()
    assert msg["event"] == "stopped"
    assert msg["data"]["reason"] == "breakpoint"


# --- RpcHandlers reads through the ref ---------------------------------


def test_rpc_handlers_event_handler_follows_ref_swap():
    """The whole point of HandlerRef: RpcHandlers, constructed once,
    must always see the current handler — no rebuilding required."""
    eh1 = ServerEventHandler()
    ref = HandlerRef(eh1)
    ctrl = DebugController(eh1)
    handlers = RpcHandlers(ControllerRef(ctrl), ref)

    assert handlers.event_handler is eh1

    eh2 = ServerEventHandler()
    ref.set(eh2)
    assert handlers.event_handler is eh2


def test_rpc_handlers_accepts_raw_handler_for_compat():
    """Existing call sites (tests, headless runner) pass a bare
    ServerEventHandler. RpcHandlers must auto-wrap it so the property
    contract still holds."""
    eh = ServerEventHandler()
    ctrl = DebugController(eh)
    handlers = RpcHandlers(ControllerRef(ctrl), eh)
    assert handlers.event_handler is eh


# --- Drain interaction --------------------------------------------------


async def test_restart_during_stopped_unblocks_waiters_via_handler_swap():
    """A subscriber waiting on the queue should be released by the
    session_restart event, even if no other event fires.
    """
    h1 = ServerEventHandler()
    q = h1.subscribe_sse()
    ref = HandlerRef(h1)

    async def reader():
        return await asyncio.wait_for(q.get(), timeout=1.0)

    task = asyncio.create_task(reader())
    # Yield once so the reader starts awaiting.
    await asyncio.sleep(0)
    ref.set(ServerEventHandler())
    msg = await task
    assert msg["event"] == "session_restart"


# --- Defensive: subscriber migration is idempotent in shape ------------


def test_set_then_set_keeps_subscribers_intact():
    h1 = ServerEventHandler()
    q = h1.subscribe_sse()
    ref = HandlerRef(h1)
    h2 = ServerEventHandler()
    h3 = ServerEventHandler()
    ref.set(h2)
    ref.set(h3)
    assert q in h3._sse_subscribers
    # Two session_restart events, one per swap.
    msgs = [q.get_nowait() for _ in range(q.qsize())]
    assert [m["event"] for m in msgs] == ["session_restart", "session_restart"]
