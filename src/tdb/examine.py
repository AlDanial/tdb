"""Run-mode examine: snapshot every thread/task/goroutine/process stack
as one JSON record, then let the program continue.

`collect` is pure with respect to I/O (controller in, dict out) and
never raises: every sub-collector failure becomes a string in the
record's `errors` list, because losing the debugger to a snapshot bug
would be worse than a partial snapshot. `write` serializes once and
writes to every sink. Signal handling and the pause/continue cycle live
in `run_mode.py`. See
docs/superpowers/specs/2026-09-07-run-examine-mode-design.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, TextIO

from tdb._timeouts import EXAMINE_CHILD

if TYPE_CHECKING:
    from tdb.dap.client import DAPClient
    from tdb.dap.types import StackFrame
    from tdb.session.controller import DebugController

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# The DAPClient default of 20 frames is tuned for the TUI's stack view;
# a hang's interesting frame can sit far deeper.
EXAMINE_FRAME_CAP = 200

# Python-only: the adapter never tells us the debuggee's pid directly.
_PID_EXPR = '__import__("os").getpid()'


def now_iso() -> str:
    """Local time with UTC offset, millisecond precision."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _frame_dict(f: "StackFrame") -> dict[str, Any]:
    path = f.source.path if f.source is not None else None
    if path is None:
        return {"function": f.name, "file": None, "line": None}
    return {"function": f.name, "file": path, "line": f.line}


async def _threads_of(client: "DAPClient") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in await client.threads():
        frames = await client.stack_trace(t.id, levels=EXAMINE_FRAME_CAP)
        out.append(
            {"id": t.id, "name": t.name, "frames": [_frame_dict(f) for f in frames]}
        )
    return out


async def _parent_pid(controller: "DebugController", errors: list[str]) -> int | None:
    if controller.profile.id != "python":
        return None
    try:
        raw = await controller.evaluate_on_parent(_PID_EXPR)
        return int(str(raw).strip())
    except Exception as exc:  # evaluate error strings, non-int results
        errors.append(f"pid: {exc}")
        return None


async def collect(
    controller: "DebugController",
    *,
    trigger: str,
    seq: int,
    requested_at: str,
    landed_at: str | None,
    launched_at: float,
    status: str = "ok",
    exit_code: int | None = None,
) -> dict[str, Any]:
    """Build one examine record. `status` "pending"/"exited" produce a
    header-only record and make no DAP requests."""
    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "seq": seq,
        "trigger": trigger,
        "status": status,
        "requested_at": requested_at,
    }
    if landed_at is not None:
        record["landed_at"] = landed_at
    record["elapsed_s"] = round(time.monotonic() - launched_at, 1)
    record["language"] = controller.profile.id
    record["program"] = getattr(controller, "program", None)
    if exit_code is not None:
        record["exit_code"] = exit_code
    record["processes"] = []
    errors: list[str] = []
    record["errors"] = errors
    if status != "ok":
        return record

    parent: dict[str, Any] = {
        "pid": await _parent_pid(controller, errors),
        "role": "parent",
        "name": None,
        "threads": [],
    }
    try:
        parent["threads"] = await _threads_of(controller.client)
    except Exception as exc:
        errors.append(f"parent threads: {exc}")
    record["processes"].append(parent)
    return record
