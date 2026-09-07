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
from tdb.session.errors import SessionGateError
from tdb.session.inspect_service import InspectService

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


def _task_dict(t: Any) -> dict[str, Any]:
    return {
        "name": t.name,
        "state": t.state,
        "awaiting": t.awaiting,
        "frames": list(t.stack),
    }


async def _add_tasks(
    svc: InspectService, parent: dict[str, Any], errors: list[str]
) -> None:
    try:
        tasks = await svc.collect_tasks()
    except SessionGateError:
        return  # language doesn't support task inspection
    except Exception as exc:
        errors.append(f"tasks: {exc}")
        return
    if tasks:
        parent["tasks"] = [_task_dict(t) for t in tasks]


async def _process_names(svc: InspectService, errors: list[str]) -> dict[int, str]:
    try:
        return {
            p.pid: p.name for p in await svc.collect_processes() if p.pid is not None
        }
    except SessionGateError:
        return {}
    except Exception as exc:
        errors.append(f"processes: {exc}")
        return {}


async def _add_children(
    controller: "DebugController",
    svc: InspectService,
    record: dict[str, Any],
    errors: list[str],
) -> None:
    children = dict(getattr(controller, "_child_clients", {}))
    if not children:
        return
    names = await _process_names(svc, errors)
    for pid in sorted(children):
        entry: dict[str, Any] = {
            "pid": pid,
            "role": "child",
            "name": names.get(pid),
            "threads": [],
        }
        try:
            entry["threads"] = await asyncio.wait_for(
                _threads_of(children[pid]), timeout=EXAMINE_CHILD
            )
        except asyncio.TimeoutError:
            errors.append(f"child {pid}: timeout")
        except Exception as exc:
            errors.append(f"child {pid}: {exc}")
        record["processes"].append(entry)


async def _add_concurrency(
    controller: "DebugController",
    svc: InspectService,
    record: dict[str, Any],
    errors: list[str],
) -> None:
    kind = controller.profile.capabilities.concurrency_inspection
    if kind == "go":
        key, fetch = "goroutines", svc.collect_go_concurrency
    elif kind == "rust":
        key, fetch = "rust_concurrency", svc.collect_rust_concurrency
    else:
        return
    try:
        record[key] = (await fetch()).to_dict()
    except SessionGateError:
        return
    except Exception as exc:
        errors.append(f"{key}: {exc}")


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

    svc = InspectService(lambda: controller)
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
    await _add_tasks(svc, parent, errors)
    record["processes"].append(parent)
    await _add_children(controller, svc, record, errors)
    await _add_concurrency(controller, svc, record, errors)
    return record
