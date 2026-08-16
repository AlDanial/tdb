"""Immutable data shared by source scanning and instrumentation."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class UnitKind(str, Enum):
    """How safely a logical source unit may be handled."""

    PROBEABLE = "probeable"
    STRUCTURAL = "structural"
    LABEL = "label"
    LITERAL_SOURCE = "literal_source"
    OPAQUE = "opaque"


class StopReason(str, Enum):
    """Why execution is paused at the current probe."""

    ENTRY = "entry"
    BREAKPOINT = "breakpoint"
    STEP = "step"
    PAUSE = "pause"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A 1-based, inclusive line span in an original source file."""

    path: Path
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class LogicalUnit:
    """A contiguous source fragment that occupies one stepping unit."""

    span: SourceSpan
    text: str
    kind: UnitKind
    source_target: str | None = None


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """The adapter's single logical tcsh thread."""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class StackFrame:
    """One active original-source frame at a stopped probe."""

    id: int
    name: str
    path: Path
    line: int
    end_line: int
