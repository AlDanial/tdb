"""Statement-granularity stepping loop.

`tdb` supports two step granularities:
  - "line": debugpy's native per-line trace events.
  - "statement" (default): a single user-issued `n` / `s` ends on the
    next *logical statement*, even if the statement under the cursor
    spans multiple lines (e.g. a multi-line `await gather(...)` call).

This module is the brain of the "statement" mode. It runs alongside
the controller's `step_over` / `step_in` flow:

  - `begin(kind)` is called *before* the controller fires the DAP
    next/stepIn. It records (source, line range, frame count) so we
    know what we started inside.

  - After every `stopped` event, the controller calls `maybe_continue()`.
    If we're still inside the original statement (same file, line still
    in range, didn't descend into a deeper frame for step-in), the
    stepper fires another DAP step via the `issue_step` callback and
    returns True so the controller suppresses UI repaint until the
    next stop.

  - A safety cap (`MAX_ITERATIONS`) bounds the loop in pathological
    cases (e.g. an infinite-loop comprehension on one source line).

Decoupled from DAPClient via the `issue_step` callback so the stepper
doesn't need to know which client is active or how `next` / `stepIn`
are wired. Lives outside DebugController to keep the controller's body
focused on lifecycle + dispatching, not the iteration state machine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

from tdb.session.state import DebugState, SessionPhase
from tdb.source_analysis import find_step_unit

log = logging.getLogger(__name__)


class StatementStepper:
    """Drives the multi-step loop that implements statement granularity."""

    MAX_ITERATIONS = 40
    """Safety cap. A pathological one-line infinite loop would otherwise
    spin DAP steps until the user kills tdb."""

    def __init__(
        self,
        state: DebugState,
        issue_step: Callable[[str], Awaitable[None]],
        ensure_stack_loaded: Callable[[], Awaitable[None]],
        compute_units: Callable[[str], list[tuple[int, int]]] | None = None,
    ) -> None:
        """
        Args:
            state: The DebugState — read by begin/maybe_continue to find
                the current source/line/thread, write to transition phase
                before re-issuing a step.
            issue_step: Callable invoked with `"next"` or `"stepIn"` to
                fire a DAP step request against the active thread. The
                controller's wrapper handles which client to talk to.
            ensure_stack_loaded: Callable that fetches threads + stack
                frames if `state.stack_frames` is empty (the headless
                RPC server skips this between stops). Called only by
                `begin` when needed.
            compute_units: Language-specific callable that parses source
                text into a list of (start_line, end_line) step units.
                None means the active language profile has no source
                model to derive statement units from, so statement mode
                is impossible and this stepper is locked to "line".
        """
        self._state = state
        self._issue_step = issue_step
        self._ensure_stack_loaded = ensure_stack_loaded
        self._compute_units = compute_units
        # No source model for this language -> statement mode impossible.
        self.mode: str = "statement" if compute_units is not None else "line"
        # When set, tracks an in-flight statement step:
        #   {"kind": "next"|"stepIn", "source_path": str,
        #    "range": (start_line, end_line), "frame_count": int,
        #    "iterations": int}
        # Cleared when execution leaves the range, hits a breakpoint, or
        # the safety cap is exceeded.
        self._pending: dict | None = None
        # source_path → list[(start, end)] step units. Cached because
        # we parse AST per file, but ask repeatedly during the loop.
        self._unit_cache: dict[str, list[tuple[int, int]]] = {}

    def set_mode(self, mode: str) -> None:
        """Switch step granularity. Clears any pending statement-step."""
        if self._compute_units is None:
            mode = "line"
        if mode not in ("statement", "line"):
            raise ValueError(
                f"mode must be 'statement' or 'line', got {mode!r}",
            )
        self.mode = mode
        self._pending = None

    def cancel(self) -> None:
        """Drop any pending statement-step (e.g., user did step-out)."""
        self._pending = None

    async def begin(self, kind: str) -> None:
        """Record the source statement currently under the cursor so the
        next stop can decide whether to keep stepping.

        No-op when `mode == "line"` or we can't pin down the current
        source/line (e.g., library code with no source path).
        """
        self._pending = None
        if self.mode != "statement":
            return
        if not self._state.stack_frames and self._state.current_thread_id is not None:
            try:
                await self._ensure_stack_loaded()
            except Exception:
                log.exception("ensure_stack_loaded failed in begin")
                return
        source_path = self._state.get_current_source_path()
        line = self._state.get_current_line()
        if source_path is None or line is None:
            return
        units = self._get_step_units(source_path)
        rng = find_step_unit(line, units)
        if rng is None:
            return
        self._pending = {
            "kind": kind,
            "source_path": source_path,
            "range": rng,
            "frame_count": len(self._state.stack_frames),
            "iterations": 0,
        }

    async def maybe_continue(self) -> bool:
        """If a statement-step is in flight and we're still inside its
        range, fire another DAP step. Returns True if a step was issued
        (caller should suppress UI refresh until the next stop).
        """
        pending = self._pending
        if pending is None:
            return False
        # Breakpoint / exception / pause: hand control to the user.
        if self._state.stop_reason not in ("step", None):
            self._pending = None
            return False
        pending["iterations"] += 1
        if pending["iterations"] > self.MAX_ITERATIONS:
            log.warning(
                "Statement-step hit %d-iteration safety cap; stopping.",
                self.MAX_ITERATIONS,
            )
            self._pending = None
            return False
        cur_source = self._state.get_current_source_path()
        cur_line = self._state.get_current_line()
        if cur_source is None or cur_line is None:
            self._pending = None
            return False
        # Stepped out (or into) a different file ⇒ different statement.
        if cur_source != pending["source_path"]:
            self._pending = None
            return False
        # For step-in, a deeper call stack means we entered a function.
        # Stop there so the user sees the new frame, even if its first
        # line happens to fall in the caller's statement range (rare).
        if (
            pending["kind"] == "stepIn"
            and len(self._state.stack_frames) > pending["frame_count"]
        ):
            self._pending = None
            return False
        s, e = pending["range"]
        if not (s <= cur_line <= e):
            self._pending = None
            return False
        # Still inside the original statement → issue another step.
        if self._state.current_thread_id is None:
            self._pending = None
            return False
        self._state.transition_to(SessionPhase.RUNNING)
        self._state.clear_frame_data()
        await self._issue_step(pending["kind"])
        return True

    def _get_step_units(self, source_path: str) -> list[tuple[int, int]]:
        """Read + parse the source file, caching the unit list per path."""
        cached = self._unit_cache.get(source_path)
        if cached is not None:
            return cached
        try:
            text = Path(source_path).read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            self._unit_cache[source_path] = []
            return []
        # _compute_units is only ever consulted from statement mode, and
        # `mode` cannot be "statement" unless _compute_units is not None
        # (see __init__ / set_mode).
        units = self._compute_units(text)
        self._unit_cache[source_path] = units
        return units
