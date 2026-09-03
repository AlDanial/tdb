"""Immutable goroutine observations and wait-graph results.

Mirrors rust_concurrency/models.py's discipline: frozen dataclasses,
str-enums, to_dict() for the RPC/MCP JSON surface. Deliberately no
owner/holder fields anywhere — Go mutexes don't record owners and the
analyzer must never pretend otherwise (spec).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Confidence(str, Enum):
    CONFIRMED = "confirmed"
    PROBABLE = "probable"


class GoroutineState(str, Enum):
    # RUNNING covers running-or-runnable: with the whole process
    # stopped, DAP can't distinguish the two.
    RUNNING = "running"
    CHAN_SEND = "chan_send"
    CHAN_RECV = "chan_recv"
    SELECT = "select"
    MUTEX_WAIT = "mutex_wait"
    WAITGROUP_WAIT = "waitgroup_wait"
    SLEEP = "sleep"
    SYSCALL = "syscall"
    RUNTIME = "runtime"
    UNKNOWN = "unknown"


class GoFindingKind(str, Enum):
    STUCK_CHANNEL = "stuck_channel"
    MUTEX_CONVOY = "mutex_convoy"
    LIKELY_LEAK = "likely_leak"


@dataclass(frozen=True)
class GoroutineInfo:
    thread_id: int  # DAP thread id (Delve: one thread per goroutine)
    goid: int | None  # parsed from "[Go N] ..." (None if unparseable)
    function: str  # display function from dlv's thread name
    state: GoroutineState
    operation: str | None  # "recv" | "send" | "mutex" | "waitgroup" | None
    resource_id: str | None  # wait-graph key, e.g. "chan:0xc000024180"
    frames: tuple[str, ...]  # frame names, innermost first (display only)
    is_runtime: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "goid": self.goid,
            "function": self.function,
            "state": self.state.value,
            "operation": self.operation,
            "resource_id": self.resource_id,
            "frames": list(self.frames),
            "is_runtime": self.is_runtime,
        }


@dataclass(frozen=True)
class GoResource:
    resource_id: str
    kind: str  # "channel" | "semaphore"
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"resource_id": self.resource_id, "kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class GoWaitEdge:
    thread_id: int
    resource_id: str
    operation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "resource_id": self.resource_id,
            "operation": self.operation,
        }


@dataclass(frozen=True)
class GoFinding:
    kind: GoFindingKind
    thread_ids: tuple[int, ...]
    summary: str
    confidence: Confidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "thread_ids": list(self.thread_ids),
            "summary": self.summary,
            "confidence": self.confidence.value,
        }


@dataclass(frozen=True)
class GoroutineSnapshot:
    goroutines: tuple[GoroutineInfo, ...]
    resources: tuple[GoResource, ...]
    edges: tuple[GoWaitEdge, ...]
    findings: tuple[GoFinding, ...]
    uncollected: int  # goroutines beyond the collection cap
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goroutines": [g.to_dict() for g in self.goroutines],
            "resources": [r.to_dict() for r in self.resources],
            "edges": [e.to_dict() for e in self.edges],
            "findings": [f.to_dict() for f in self.findings],
            "uncollected": self.uncollected,
            "warnings": list(self.warnings),
        }
