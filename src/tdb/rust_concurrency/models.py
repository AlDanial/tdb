"""Immutable observations and graph results for Rust concurrency inspection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Confidence(str, Enum):
    """How strongly the debugger evidence supports a relationship."""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    UNKNOWN = "unknown"


class ThreadState(str, Enum):
    """Debugger-visible execution state for one Rust thread."""

    BLOCKED = "blocked"
    RUNNING = "running"
    UNKNOWN = "unknown"


class PrimitiveKind(str, Enum):
    """Supported Rust standard-library synchronization primitive classes."""

    THREAD = "thread"
    MUTEX = "mutex"
    RWLOCK = "rwlock"
    CONDVAR = "condvar"
    CHANNEL = "channel"
    PARKER = "parker"


class FindingKind(str, Enum):
    """Kinds of deadlock and stalled-program findings."""

    CONFIRMED_DEADLOCK = "confirmed_deadlock"
    SUSPECTED_CYCLE = "suspected_cycle"
    WHOLE_PROGRAM_STALL = "whole_program_stall"


@dataclass(frozen=True)
class Evidence:
    confidence: Confidence
    source: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "confidence": self.confidence.value,
            "source": self.source,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RawVariable:
    name: str
    value: str
    type_name: str
    evaluate_name: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "value": self.value,
            "type_name": self.type_name,
            "evaluate_name": self.evaluate_name,
        }


@dataclass(frozen=True)
class RawFrame:
    frame_id: int
    name: str
    source_path: str | None
    line: int
    variables: tuple[RawVariable, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "name": self.name,
            "source_path": self.source_path,
            "line": self.line,
            "variables": [variable.to_dict() for variable in self.variables],
        }


@dataclass(frozen=True)
class RawThread:
    thread_id: int
    name: str
    frames: tuple[RawFrame, ...]
    os_thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "name": self.name,
            "frames": [frame.to_dict() for frame in self.frames],
            "os_thread_id": self.os_thread_id,
        }


@dataclass(frozen=True)
class RawSnapshot:
    adapter: str
    platform: str
    rust_version: str | None
    threads: tuple[RawThread, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "platform": self.platform,
            "rust_version": self.rust_version,
            "threads": [thread.to_dict() for thread in self.threads],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ProbeThread:
    dap_thread_hint: str
    os_thread_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "dap_thread_hint": self.dap_thread_hint,
            "os_thread_id": self.os_thread_id,
        }


@dataclass(frozen=True)
class ProbePrimitiveState:
    primitive_id: str
    owner_os_thread_ids: tuple[str, ...]
    raw_state: str
    evidence: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "primitive_id": self.primitive_id,
            "owner_os_thread_ids": list(self.owner_os_thread_ids),
            "raw_state": self.raw_state,
            "evidence": [evidence.to_dict() for evidence in self.evidence],
        }


@dataclass(frozen=True)
class ProbeResult:
    rust_version: str | None
    threads: tuple[ProbeThread, ...] = ()
    primitive_states: tuple[ProbePrimitiveState, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rust_version": self.rust_version,
            "threads": [thread.to_dict() for thread in self.threads],
            "primitive_states": [
                primitive_state.to_dict() for primitive_state in self.primitive_states
            ],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class WaitEdge:
    waiter_thread_id: int
    primitive_id: str
    owner_thread_id: int | None
    operation: str
    evidence: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "waiter_thread_id": self.waiter_thread_id,
            "primitive_id": self.primitive_id,
            "owner_thread_id": self.owner_thread_id,
            "operation": self.operation,
            "evidence": [evidence.to_dict() for evidence in self.evidence],
        }


@dataclass(frozen=True)
class Primitive:
    primitive_id: str
    kind: PrimitiveKind
    address: str | None
    label: str
    evidence: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "primitive_id": self.primitive_id,
            "kind": self.kind.value,
            "address": self.address,
            "label": self.label,
            "evidence": [evidence.to_dict() for evidence in self.evidence],
        }


@dataclass(frozen=True)
class ThreadAnalysis:
    thread_id: int
    name: str
    state: ThreadState
    wait: WaitEdge | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "name": self.name,
            "state": self.state.value,
            "wait": self.wait.to_dict() if self.wait is not None else None,
        }


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    thread_ids: tuple[int, ...]
    summary: str
    evidence_gaps: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "thread_ids": list(self.thread_ids),
            "summary": self.summary,
            "evidence_gaps": list(self.evidence_gaps),
        }


@dataclass(frozen=True)
class ConcurrencySnapshot:
    rust_version: str | None
    adapter: str
    platform: str
    threads: tuple[ThreadAnalysis, ...]
    primitives: tuple[Primitive, ...]
    edges: tuple[WaitEdge, ...]
    confirmed_deadlocks: tuple[Finding, ...]
    suspected_stalls: tuple[Finding, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the stable JSON-ready public concurrency snapshot schema."""
        return {
            "rust_version": self.rust_version,
            "adapter": self.adapter,
            "platform": self.platform,
            "threads": [thread.to_dict() for thread in self.threads],
            "primitives": [primitive.to_dict() for primitive in self.primitives],
            "edges": [edge.to_dict() for edge in self.edges],
            "confirmed_deadlocks": [
                finding.to_dict() for finding in self.confirmed_deadlocks
            ],
            "suspected_stalls": [
                finding.to_dict() for finding in self.suspected_stalls
            ],
            "warnings": list(self.warnings),
        }
