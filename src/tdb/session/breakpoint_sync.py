"""Reconcile tdb's breakpoint table with the native debugger's own list.

Native debuggers with a REPL (gdb via `gdb -i dap`) let the user set
breakpoints outside DAP — `b 83` in the Evaluate console — which tdb's
`state.breakpoints` never sees. `DebugController.sync_breakpoints_from_
debugger` asks the adapter for a listing command, hands its output to
`parse_breakpoint_listing`, and applies `reconcile` to the state.

Ownership matters: gdb's DAP layer only manages breakpoints it created,
so merely mirroring a CLI breakpoint into tdb would leave a stray gdb
copy behind the first time the user cleared it from the Breakpoints
View. `reconcile` therefore reports which CLI-created breakpoints to
delete in the debugger; the controller deletes them and re-pushes the
file over DAP so every breakpoint tdb shows is one it owns.

The listing format is tdb's own (the adapter's query command prints
it): a JSON array, one object per source breakpoint, with keys
  n        debugger-side breakpoint number
  path     absolute source path
  line     line number
  enabled  bool
  cond     condition string or null
  dap      true when the adapter's DAP layer created it
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from tdb.dap.types import SourceBreakpoint


@dataclass(frozen=True)
class DebuggerBreakpoint:
    number: int
    path: str
    line: int
    enabled: bool
    condition: str | None
    dap_owned: bool


@dataclass
class SyncPlan:
    """What the controller must do after `reconcile` mutated the state."""

    # Files whose breakpoint list changed and must be re-pushed over DAP.
    changed_paths: set[str] = field(default_factory=set)
    # Debugger-side numbers of CLI-created breakpoints tdb now owns (or
    # found redundant); delete these in the debugger before re-pushing.
    delete_numbers: list[int] = field(default_factory=list)


def parse_breakpoint_listing(text: str) -> list[DebuggerBreakpoint]:
    """Parse the query command's output. The JSON array is the last
    non-empty line; anything before it is debugger chatter. Raises
    ValueError when no such line parses."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("empty breakpoint listing")
    try:
        rows = json.loads(lines[-1])
    except json.JSONDecodeError as e:
        raise ValueError(f"unparseable breakpoint listing: {lines[-1]!r}") from e
    if not isinstance(rows, list):
        raise ValueError("breakpoint listing is not a JSON array")
    out: list[DebuggerBreakpoint] = []
    for row in rows:
        try:
            out.append(
                DebuggerBreakpoint(
                    number=int(row["n"]),
                    path=str(row["path"]),
                    line=int(row["line"]),
                    enabled=bool(row["enabled"]),
                    condition=row.get("cond") or None,
                    dap_owned=bool(row.get("dap", False)),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"malformed breakpoint row: {row!r}") from e
    return out


def reconcile(
    state_breakpoints: dict[str, list[SourceBreakpoint]],
    breakpoints_disabled: bool,
    debugger_bps: list[DebuggerBreakpoint],
) -> SyncPlan:
    """Bring `state_breakpoints` in line with the debugger's list, in place.

    Rules (keyed by (path, line)):
      - in debugger, not in tdb: adopt with the debugger's condition and
        enabled flag; a CLI-created one is scheduled for deletion so the
        re-push can recreate it under DAP ownership.
      - in tdb (enabled) but not in debugger: the user deleted it there;
        drop it. Skipped under Disable All, and always skipped for
        tdb-disabled breakpoints — neither is ever sent to the adapter,
        so their absence is expected.
      - in both: condition and enabled flag follow the debugger. A CLI
        duplicate of a line tdb already owns is just deleted.
    """
    plan = SyncPlan()
    seen: dict[tuple[str, int], DebuggerBreakpoint] = {}
    for dbp in debugger_bps:
        key = (dbp.path, dbp.line)
        if key in seen:
            # Two debugger breakpoints on one line: keep the DAP-owned
            # one as the survivor, delete the CLI extra.
            keep, extra = seen[key], dbp
            if extra.dap_owned and not keep.dap_owned:
                keep, extra = extra, keep
            seen[key] = keep
            if not extra.dap_owned:
                plan.delete_numbers.append(extra.number)
            continue
        seen[key] = dbp

    # Removals and updates for what tdb already has.
    for path in list(state_breakpoints):
        bps = state_breakpoints[path]
        kept: list[SourceBreakpoint] = []
        for bp in bps:
            dbp = seen.pop((path, bp.line), None)
            if dbp is None:
                if breakpoints_disabled or not bp.enabled:
                    kept.append(bp)
                else:
                    plan.changed_paths.add(path)
                continue
            if not dbp.dap_owned:
                # tdb has this line but gdb's copy is CLI-created (a
                # CLI duplicate whose DAP twin is gone, or the DAP one
                # was deleted by hand). Take it over.
                plan.delete_numbers.append(dbp.number)
                plan.changed_paths.add(path)
            if (bp.condition, bp.enabled) != (dbp.condition, dbp.enabled):
                bp.condition = dbp.condition
                bp.enabled = dbp.enabled
                plan.changed_paths.add(path)
            kept.append(bp)
        if kept:
            state_breakpoints[path] = kept
        else:
            del state_breakpoints[path]

    # Whatever is left in `seen` is new to tdb.
    for (path, line), dbp in seen.items():
        state_breakpoints.setdefault(path, []).append(
            SourceBreakpoint(line=line, condition=dbp.condition, enabled=dbp.enabled)
        )
        plan.changed_paths.add(path)
        if not dbp.dap_owned:
            plan.delete_numbers.append(dbp.number)

    plan.delete_numbers.sort()
    return plan
