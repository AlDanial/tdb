"""On-disk cache for the Processes modal.

Opening the Processes modal in a multi-process debug session is
expensive: each visible child requires DAP threads + stack_trace +
scopes + variables round-trips against its own DAPClient. Closing the
modal and re-opening within the *same stopped episode* repaints exactly
the same data, so we serialize the modal's accumulated detail on dismiss
and restore from it on open.

Lifecycle:
  - controller._on_continued (and controller.stop) clears the
    snapshot — any subsequent step/continue invalidates it.
  - The snapshot is kept in memory, as a JSON string, so each load()
    returns fresh objects. It used to be a predictably named file in the
    shared temp dir, which another local user could pre-create as a
    symlink (clobbering a file on save) or fill with forged data.
"""

from __future__ import annotations

import json
import logging

from tdb.dap.types import Scope, Source, StackFrame, Variable
from tdb.inspection import ProcessInfo

log = logging.getLogger(__name__)


_snapshot: str | None = None


# --- Serialization helpers -------------------------------------------
# Snake_case throughout — this is an internal format, not DAP wire.


def _proc_to_dict(p: ProcessInfo) -> dict:
    return {
        "name": p.name,
        "pid": p.pid,
        "alive": p.alive,
        "exitcode": p.exitcode,
        "daemon": p.daemon,
        "target": p.target,
        "args": p.args,
        "kwargs": p.kwargs,
        "start_method": p.start_method,
    }


def _proc_from_dict(d: dict) -> ProcessInfo:
    return ProcessInfo(
        name=d["name"],
        pid=d.get("pid"),
        alive=d.get("alive", False),
        exitcode=d.get("exitcode"),
        daemon=d.get("daemon", False),
        target=d.get("target", ""),
        args=d.get("args", ""),
        kwargs=d.get("kwargs", ""),
        start_method=d.get("start_method", ""),
    )


def _source_to_dict(s: Source | None) -> dict | None:
    if s is None:
        return None
    return {
        "path": s.path,
        "name": s.name,
        "source_reference": s.source_reference,
    }


def _source_from_dict(d: dict | None) -> Source | None:
    if d is None:
        return None
    return Source(
        path=d.get("path"),
        name=d.get("name"),
        source_reference=d.get("source_reference", 0),
    )


def _frame_to_dict(f: StackFrame) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "line": f.line,
        "column": f.column,
        "source": _source_to_dict(f.source),
    }


def _frame_from_dict(d: dict) -> StackFrame:
    return StackFrame(
        id=d["id"],
        name=d["name"],
        line=d.get("line", 0),
        column=d.get("column", 0),
        source=_source_from_dict(d.get("source")),
    )


def _scope_to_dict(s: Scope) -> dict:
    return {
        "name": s.name,
        "variables_reference": s.variables_reference,
        "named_variables": s.named_variables,
        "indexed_variables": s.indexed_variables,
        "expensive": s.expensive,
    }


def _scope_from_dict(d: dict) -> Scope:
    return Scope(
        name=d["name"],
        variables_reference=d["variables_reference"],
        named_variables=d.get("named_variables", 0),
        indexed_variables=d.get("indexed_variables", 0),
        expensive=d.get("expensive", False),
    )


def _var_to_dict(v: Variable) -> dict:
    return {
        "name": v.name,
        "value": v.value,
        "type": v.type,
        "variables_reference": v.variables_reference,
        "named_variables": v.named_variables,
        "indexed_variables": v.indexed_variables,
        "evaluate_name": v.evaluate_name,
    }


def _var_from_dict(d: dict) -> Variable:
    return Variable(
        name=d["name"],
        value=d["value"],
        type=d.get("type", ""),
        variables_reference=d.get("variables_reference", 0),
        named_variables=d.get("named_variables", 0),
        indexed_variables=d.get("indexed_variables", 0),
        evaluate_name=d.get("evaluate_name"),
    )


# --- Public API ------------------------------------------------------


def save(
    processes: list[ProcessInfo],
    details: dict[int, dict],
    current_pid: int | None,
) -> None:
    """Serialize the Processes modal snapshot (kept in memory).

    `details` is keyed by pid; each value is the same shape that
    ProcessesModal.show_process_detail receives:
      {"frames": list[StackFrame],
       "scopes": list[Scope],
       "variables": dict[int, list[Variable]]}.

    Best-effort: write failures are logged and swallowed.
    """
    payload = {
        "processes": [_proc_to_dict(p) for p in processes],
        "details": {
            str(pid): {
                "frames": [_frame_to_dict(f) for f in d["frames"]],
                "scopes": [_scope_to_dict(s) for s in d["scopes"]],
                "variables": {
                    str(ref): [_var_to_dict(v) for v in vs]
                    for ref, vs in d["variables"].items()
                },
            }
            for pid, d in details.items()
        },
        "current_pid": current_pid,
    }
    global _snapshot
    _snapshot = json.dumps(payload)


def load() -> dict | None:
    """Return the cached snapshot or None if missing/corrupt.

    Shape:
      {"processes": list[ProcessInfo],
       "details": {pid: {"frames": [...], "scopes": [...],
                         "variables": {ref: [Variable]}}},
       "current_pid": int | None}
    """
    if _snapshot is None:
        return None
    try:
        raw = json.loads(_snapshot)
    except json.JSONDecodeError:
        log.exception("Processes cache snapshot is corrupt")
        return None
    try:
        processes = [_proc_from_dict(p) for p in raw["processes"]]
        details: dict[int, dict] = {}
        for pid_str, d in raw.get("details", {}).items():
            details[int(pid_str)] = {
                "frames": [_frame_from_dict(f) for f in d["frames"]],
                "scopes": [_scope_from_dict(s) for s in d["scopes"]],
                "variables": {
                    int(ref): [_var_from_dict(v) for v in vs]
                    for ref, vs in d.get("variables", {}).items()
                },
            }
        return {
            "processes": processes,
            "details": details,
            "current_pid": raw.get("current_pid"),
        }
    except (KeyError, TypeError, ValueError):
        log.exception("Processes cache schema mismatch — discarding")
        return None


def clear() -> None:
    """Discard the snapshot. No-op if there is none."""
    global _snapshot
    _snapshot = None
