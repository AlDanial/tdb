"""Bounded goroutine collection over plain DAP — no injected probes.

Delve surfaces every goroutine as a DAP thread named
"[Go <goid>] <function>" ("* " prefix on the current one), so the
collector is: threads -> per-goroutine stackTrace -> classify ->
(for channel/semaphore parks) one frame-scoped evaluate to identify
the wait target. Every per-goroutine failure degrades that entry, not
the snapshot (spec's fail-soft rule).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from tdb.go_concurrency.analyzer import analyze
from tdb.go_concurrency.classifier import classify_stack
from tdb.go_concurrency.models import GoroutineInfo, GoroutineSnapshot, GoroutineState
from tdb.session.errors import SessionGateError
from tdb.session.state import SessionPhase

log = logging.getLogger(__name__)

_DLV_THREAD_RE = re.compile(r"^\*?\s*\[Go (\d+)\]\s*(.*)$")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")


class GoConcurrencyCollector:
    def __init__(self, *, max_goroutines: int = 150, max_frames: int = 64) -> None:
        self.max_goroutines = max_goroutines
        self.max_frames = max_frames

    @staticmethod
    def _gate(controller: Any) -> None:
        state = controller.state
        if state.is_terminated:
            raise SessionGateError("terminated")
        if state.phase is not SessionPhase.STOPPED:
            raise SessionGateError("running")

    async def collect(self, controller: Any) -> GoroutineSnapshot:
        self._gate(controller)
        client = controller.client
        threads = await client.threads()

        # Cap with user goroutines first: runtime-named entries sort last.
        def runtime_last(t: Any) -> tuple[int, int]:
            m = _DLV_THREAD_RE.match(t.name or "")
            func = m.group(2) if m else (t.name or "")
            return (1 if func.startswith(("runtime.", "runtime/")) else 0, t.id)

        ordered = sorted(threads, key=runtime_last)
        selected = ordered[: self.max_goroutines]
        uncollected = len(ordered) - len(selected)

        goroutines: list[GoroutineInfo] = []
        warnings: list[str] = []
        for t in selected:
            self._gate(controller)  # a resume mid-collection aborts cleanly
            m = _DLV_THREAD_RE.match(t.name or "")
            goid = int(m.group(1)) if m else None
            function = (m.group(2) if m else t.name or "").strip()
            try:
                frames = await client.stack_trace(t.id, levels=self.max_frames)
            except Exception:
                log.debug("goroutine %s: stack fetch failed", t.id)
                warnings.append(f"goroutine {goid or t.id}: stack unavailable")
                goroutines.append(
                    GoroutineInfo(
                        t.id,
                        goid,
                        function,
                        GoroutineState.UNKNOWN,
                        None,
                        None,
                        (),
                        False,
                    )
                )
                continue
            names = [f.name for f in frames]
            c = classify_stack(names)
            resource_id: str | None = None
            if c.target_expr is not None and c.park_frame_index is not None:
                resource_id = await self._resolve_target(
                    client, frames[c.park_frame_index].id, c.target_expr, c.operation
                )
            goroutines.append(
                GoroutineInfo(
                    thread_id=t.id,
                    goid=goid,
                    function=function,
                    state=c.state,
                    operation=c.operation,
                    resource_id=resource_id,
                    frames=tuple(names),
                    is_runtime=c.state is GoroutineState.RUNTIME,
                )
            )
        return analyze(goroutines, uncollected, tuple(warnings))

    @staticmethod
    async def _resolve_target(
        client: Any, frame_id: int, expr: str, operation: str | None
    ) -> str | None:
        """Evaluate the park frame's channel/semaphore argument and turn
        it into a stable resource key. Prefers the DAP memoryReference;
        falls back to any address literal in the printed value."""
        try:
            body = await client.evaluate_raw(expr, frame_id=frame_id, context="watch")
        except Exception:
            log.debug("target eval %r in frame %s failed", expr, frame_id)
            return None
        addr = body.get("memoryReference")
        if not addr:
            m = _ADDR_RE.search(body.get("result", ""))
            addr = m.group(0) if m else None
        if not addr:
            return None
        prefix = "chan" if operation in ("recv", "send") else "sem"
        return f"{prefix}:{addr}"
