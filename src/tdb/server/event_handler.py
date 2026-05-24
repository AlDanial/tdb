"""Server event handler: captures DAP events for RPC response building."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone

from tdb.session.event_bus import DebugEventHandler

log = logging.getLogger(__name__)


class ServerEventHandler:
    """Implements DebugEventHandler for the headless/server mode.

    Provides asyncio synchronization primitives so RPC handlers can
    await events like "stopped" or "terminated".
    """

    def __init__(self) -> None:
        # Fired when debugpy sends 'initialized' — server uses this to
        # know when to send configurationDone.
        self.initialized_event = asyncio.Event()

        # Fired when the debuggee stops (breakpoint, step, exception).
        # RPC step/continue handlers await this before responding.
        self.stopped_event = asyncio.Event()

        # Fired when the debuggee terminates.
        self.terminated_event = asyncio.Event()

        # Last stop details for RPC responses.
        self.last_stop_thread_id: int | None = None
        self.last_stop_reason: str | None = None
        self.last_stop_description: str | None = None
        self.last_stop_text: str | None = None

        # Captured stdout/stderr output since last drain.
        self._output_buffer: deque[str] = deque(maxlen=10_000)

        # Exit code from exited event.
        self.exit_code: int | None = None

        # SSE subscribers: each is an asyncio.Queue that receives event dicts.
        self._sse_subscribers: list[asyncio.Queue[dict]] = []

    # --- DebugEventHandler protocol ---

    def on_initialized(self) -> None:
        log.info("Server: initialized")
        self.initialized_event.set()
        self._broadcast_sse("initialized", {})

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        log.info("Server: stopped thread=%s reason=%s", thread_id, reason)
        self.last_stop_thread_id = thread_id
        self.last_stop_reason = reason
        self.last_stop_description = description
        self.last_stop_text = text
        self.stopped_event.set()
        self._broadcast_sse(
            "stopped",
            {
                "thread_id": thread_id,
                "reason": reason,
                "description": description,
                "text": text,
            },
        )

    def on_continued(self) -> None:
        log.info("Server: continued")
        self.stopped_event.clear()
        self._broadcast_sse("continued", {})

    def on_terminated(self) -> None:
        log.info("Server: terminated")
        self.terminated_event.set()
        # Also set stopped_event so any waiters unblock
        self.stopped_event.set()
        self._broadcast_sse("terminated", {})

    def on_exited(self, exit_code: int) -> None:
        log.info("Server: exited code=%d", exit_code)
        self.exit_code = exit_code
        self._broadcast_sse("exited", {"exit_code": exit_code})

    def on_output(self, text: str, category: str) -> None:
        if category in ("stdout", "stderr", "console"):
            self._output_buffer.append(text)
            self._broadcast_sse("output", {"text": text, "category": category})

    def on_external_terminal_started(self) -> None:
        log.info("Server: external terminal started")
        self._broadcast_sse("external_terminal_started", {})

    # --- Helpers for RPC handlers ---

    def drain_output(self) -> str:
        """Return and clear all captured output since last drain."""
        result = "".join(self._output_buffer)
        self._output_buffer.clear()
        return result

    def peek_output(self) -> str:
        """Return captured output without clearing the buffer."""
        return "".join(self._output_buffer)

    async def wait_for_stop(self, timeout: float = 30.0) -> bool:
        """Wait until the debuggee stops. Returns True if stopped, False on timeout."""
        try:
            await asyncio.wait_for(self.stopped_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def reset_for_continue(self) -> None:
        """Clear the stopped event before issuing a continue/step."""
        self.stopped_event.clear()

    # --- SSE support ---

    def subscribe_sse(self) -> asyncio.Queue[dict]:
        """Create a new SSE subscription queue."""
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue[dict]) -> None:
        """Remove an SSE subscription queue."""
        try:
            self._sse_subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast_sse(self, event: str, data: dict) -> None:
        """Push an event to all SSE subscribers (non-blocking)."""
        msg = {
            "event": event,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        dead: list[asyncio.Queue[dict]] = []
        for q in self._sse_subscribers:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        # Drop subscribers that can't keep up
        for q in dead:
            self._sse_subscribers.remove(q)
