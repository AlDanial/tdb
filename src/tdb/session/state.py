"""Debug session state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from tdb.dap.types import (
    Scope,
    SourceBreakpoint,
    StackFrame,
    Thread,
    Variable,
)


class SessionPhase(Enum):
    """Lifecycle phase of the debug session.

    Replaces the loose collection of `is_ready` / `is_running` /
    `is_terminated` / `is_post_mortem` booleans that used to be
    independently mutable. The boolean fields are still exposed as
    read-only properties on `DebugState` so existing call sites don't
    have to change at once, but the only way to *mutate* phase is via
    `DebugState.transition_to(...)`.
    """

    NOT_STARTED = auto()
    """Pre-launch. No DAP session yet."""

    RUNNING = auto()
    """Debuggee is executing — no stack/variables to inspect."""

    STOPPED = auto()
    """Debuggee paused at breakpoint, step, exception, or pause."""

    TERMINATED = auto()
    """Session ended (debuggee exited or DAP terminated event)."""

    POST_MORTEM = auto()
    """Frozen snapshot: no live DAP, frames+variables pre-populated."""


@dataclass
class DebugState:
    """Mutable state of a debug session."""

    # Breakpoints keyed by source file path
    breakpoints: dict[str, list[SourceBreakpoint]] = field(default_factory=dict)
    breakpoints_disabled: bool = False

    # Current thread/frame context
    threads: list[Thread] = field(default_factory=list)
    current_thread_id: int | None = None
    stack_frames: list[StackFrame] = field(default_factory=list)
    current_frame_id: int | None = None

    # Scopes and variables for the current frame
    scopes: list[Scope] = field(default_factory=list)
    variables: dict[int, list[Variable]] = field(default_factory=dict)

    # Lifecycle phase. The single source of truth for is_running /
    # is_terminated / etc. — those are derived. Mutate only through
    # `transition_to(...)`.
    phase: SessionPhase = SessionPhase.NOT_STARTED

    # Reason for the last stop (breakpoint / step / exception / pause / etc.).
    stop_reason: str | None = None

    # Per-frame scope lists, keyed by frame.id. Populated only in post-mortem.
    frame_scopes: dict[int, list[Scope]] = field(default_factory=dict)

    # --- State transitions ---------------------------------------------

    def transition_to(self, phase: SessionPhase) -> None:
        """Move the session into a new phase.

        Centralizing transitions here lets us add invariant checks and
        side-effects (e.g., clearing stale frame data on RUNNING) in one
        place. For now the body is intentionally minimal — we just set
        the field. Auxiliary cleanup (`clear_frame_data`) is still the
        caller's responsibility because some transitions don't want it
        (e.g., post-mortem load wants the frame data preserved).
        """
        self.phase = phase

    # --- Lifecycle properties (derived from phase) ---------------------
    # These are read-only by design — DO NOT add setters. Production code
    # mutates via transition_to(); tests can also use transition_to or
    # set state.phase directly.

    @property
    def is_ready(self) -> bool:
        """True once `do_configure` has unblocked the launch (phase has
        moved past NOT_STARTED). Used to gate breakpoint pushes against
        a not-yet-started session."""
        return self.phase != SessionPhase.NOT_STARTED

    @property
    def is_running(self) -> bool:
        return self.phase == SessionPhase.RUNNING

    @property
    def is_terminated(self) -> bool:
        """True for both natural termination AND post-mortem snapshots,
        because in both cases there is no live DAP to send to. The
        existing 17 guard sites check this to skip outbound DAP — that
        invariant is preserved."""
        return self.phase in (SessionPhase.TERMINATED, SessionPhase.POST_MORTEM)

    @property
    def is_post_mortem(self) -> bool:
        return self.phase == SessionPhase.POST_MORTEM

    # --- Capability properties (composite) -----------------------------
    # Prefer these over reading the raw booleans where possible — they
    # name the *question being asked* rather than the implementation.

    @property
    def can_send_dap(self) -> bool:
        """True when sending an outbound DAP request is meaningful.

        False during NOT_STARTED (no session yet), TERMINATED (session
        gone), and POST_MORTEM (no live debuggee). The breakpoint-push
        guard sites all reduce to this.
        """
        return self.phase in (SessionPhase.RUNNING, SessionPhase.STOPPED)

    @property
    def can_step(self) -> bool:
        """True when a step / continue / pause / evaluate request can be
        issued. Requires the debuggee to be paused."""
        return self.phase == SessionPhase.STOPPED

    @property
    def can_evaluate(self) -> bool:
        """True when the Evaluate console can be invoked.

        Same predicate as `can_step` for live sessions; post-mortem
        evaluation is a separate path that doesn't go through this
        property because it doesn't talk to DAP.
        """
        return self.phase == SessionPhase.STOPPED

    # --- Existing helper methods ---------------------------------------

    def clear_frame_data(self) -> None:
        self.stack_frames.clear()
        self.scopes.clear()
        self.variables.clear()
        self.current_frame_id = None

    def get_current_source_path(self) -> str | None:
        for frame in self.stack_frames:
            if frame.id == self.current_frame_id and frame.source:
                return frame.source.path
        return None

    def get_current_line(self) -> int | None:
        for frame in self.stack_frames:
            if frame.id == self.current_frame_id:
                return frame.line
        return None

    def get_stop_location(self) -> tuple[str, int]:
        """Return (location_string, line) for the actual stop point (top of stack).

        Always returns a location, even for library code without source paths.
        """
        if self.stack_frames:
            top = self.stack_frames[0]
            if top.source and top.source.path:
                return (top.source.path, top.line)
            return (top.name, top.line)

        return ("unknown", 0)
