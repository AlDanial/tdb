"""Goroutine state from stack shape — pure functions, no DAP.

A parked goroutine's top frames are runtime.gopark(+unlock); the
nearest recognizable caller names the park reason. `target_expr` is
the expression the collector evaluates IN the park frame to identify
the wait target: `c` (the *hchan argument of chanrecv/chansend) or
`addr` (the *uint32 semaphore of the sync semacquire family).
"""

from __future__ import annotations

from dataclasses import dataclass

from tdb.go_concurrency.models import GoroutineState

# Ordered: first match on the parked stack wins. (frame-name prefix,
# state, operation, target_expr)
_PARK_RULES: tuple[tuple[str, GoroutineState, str | None, str | None], ...] = (
    ("runtime.chanrecv", GoroutineState.CHAN_RECV, "recv", "c"),
    ("runtime.chansend", GoroutineState.CHAN_SEND, "send", "c"),
    ("runtime.selectgo", GoroutineState.SELECT, None, None),
    ("sync.runtime_SemacquireMutex", GoroutineState.MUTEX_WAIT, "mutex", "addr"),
    ("sync.runtime_SemacquireRWMutex", GoroutineState.MUTEX_WAIT, "mutex", "addr"),
    (
        "sync.runtime_SemacquireWaitGroup",
        GoroutineState.WAITGROUP_WAIT,
        "waitgroup",
        "addr",
    ),
    ("sync.runtime_Semacquire", GoroutineState.WAITGROUP_WAIT, "waitgroup", "addr"),
    ("time.Sleep", GoroutineState.SLEEP, None, None),
)
_SYSCALL_MARKERS = ("syscall.Syscall", "syscall.syscall", "runtime.netpoll")


@dataclass(frozen=True)
class Classification:
    state: GoroutineState
    park_frame_index: int | None  # index into the frame list, or None
    operation: str | None
    target_expr: str | None


def classify_stack(frame_names: list[str]) -> Classification:
    if not frame_names:
        return Classification(GoroutineState.UNKNOWN, None, None, None)
    parked = any(name.startswith("runtime.gopark") for name in frame_names)
    if parked:
        for prefix, state, operation, target in _PARK_RULES:
            for i, name in enumerate(frame_names):
                if name.startswith(prefix):
                    return Classification(state, i, operation, target)
        # Parked for a reason we don't model (netpoll, finalizer, GC…).
        return Classification(GoroutineState.RUNTIME, None, None, None)
    if any(name.startswith(_SYSCALL_MARKERS) for name in frame_names):
        return Classification(GoroutineState.SYSCALL, None, None, None)
    if all(name.startswith(("runtime.", "runtime/")) for name in frame_names):
        return Classification(GoroutineState.RUNTIME, None, None, None)
    return Classification(GoroutineState.RUNNING, None, None, None)
