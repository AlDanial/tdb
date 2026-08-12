"""Breakpoint binding against original-source instrumentation probes."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tdb.adapters.tcsh.instrumenter import SourceMap


@dataclass(frozen=True, slots=True)
class BoundBreakpoint:
    """A client breakpoint resolved to a safe, executable source location."""

    requested_line: int
    verified: bool
    line: int | None
    probe_id: int | None
    message: str | None


def bind_breakpoints(
    source_map: SourceMap, path: Path, requested_lines: Sequence[int]
) -> tuple[BoundBreakpoint, ...]:
    """Bind each requested line to the first safe statement at or after it."""

    probes = source_map.for_path(path.resolve(strict=False))
    bound: list[BoundBreakpoint] = []
    for requested_line in requested_lines:
        probe = next((item for item in probes if item.span.start_line >= requested_line), None)
        if probe is None:
            bound.append(
                BoundBreakpoint(
                    requested_line,
                    False,
                    None,
                    None,
                    "No safe executable statement at or after this line",
                )
            )
        else:
            bound.append(BoundBreakpoint(requested_line, True, probe.span.start_line, probe.id, None))
    return tuple(bound)
