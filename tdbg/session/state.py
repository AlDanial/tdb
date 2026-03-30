"""Debug session state."""

from __future__ import annotations

from dataclasses import dataclass, field

from tdbg.dap.types import (
    Scope,
    SourceBreakpoint,
    StackFrame,
    Thread,
    Variable,
)


@dataclass
class DebugState:
    """Mutable state of a debug session."""

    # Breakpoints keyed by source file path
    breakpoints: dict[str, list[SourceBreakpoint]] = field(default_factory=dict)

    # Current thread/frame context
    threads: list[Thread] = field(default_factory=list)
    current_thread_id: int | None = None
    stack_frames: list[StackFrame] = field(default_factory=list)
    current_frame_id: int | None = None

    # Scopes and variables for the current frame
    scopes: list[Scope] = field(default_factory=list)
    variables: dict[int, list[Variable]] = field(default_factory=dict)

    # Session status
    is_ready: bool = False  # True after launch + configurationDone
    is_running: bool = False
    is_terminated: bool = False
    stop_reason: str | None = None

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
