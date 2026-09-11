# Run-Mode Examine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "examine" trigger to `tdb --run` that pauses the debuggee, writes one JSON line describing every thread/task/goroutine/process stack, and resumes.

**Architecture:** A new `src/tdb/examine.py` holds a pure collector (controller → dict) and a writer (dict → sinks). `run_mode.py` gains a fourth wait event (`examine`) set by SIGQUIT/SIGUSR2 (POSIX) or SIGBREAK (Windows); on trigger the loop pauses, collects, writes, continues. `cli.py` gains `--examine-log DEST` (repeatable, `-` = stdout) valid only with `--run`.

**Tech Stack:** Python 3.12+, asyncio, debugpy/DAP via `tdb.dap.client.DAPClient`, existing `tdb.session.inspect_service.InspectService`, pytest with `asyncio_mode = "auto"`.

**Spec:** `docs/superpowers/specs/2026-09-07-run-examine-mode-design.md`

## Global Constraints

- Repository root for all paths below: `/home/al/projects/tdbg/work`. Work on branch `run-examine-mode`.
- Install with `uv pip install`, never bare `pip`.
- Run tests with `python -m pytest <path> -p no:cacheprovider --no-cov -q` (coverage is on by default in `pyproject.toml`; `--no-cov` keeps unit runs fast).
- Ctrl-C (SIGINT) and SIGUSR1 behavior in run mode is **unchanged**.
- Record `schema` is `1`. Language-specific keys are **omitted, not null**: `tasks` (per-process, Python only, only when non-empty), `goroutines` (top level, Go only), `rust_concurrency` (top level, Rust only).
- `language` in the record is `controller.profile.id` (lowercase: `"python"`, `"go"`, `"rust"`, …).
- Frames are innermost first, fields `function`, `file`, `line`; a frame with no source keeps `function` and has `file: null, line: null`.
- Collector requests up to `EXAMINE_FRAME_CAP = 200` frames per thread.
- The collector never raises: each sub-collector failure appends a string to `errors`.
- Timestamps: local time with UTC offset, ISO 8601, millisecond precision (`datetime.now().astimezone().isoformat(timespec="milliseconds")`).
- Log files open in append mode, UTF-8, before the session starts; directories are not created.
- Commit messages end with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_014sDZpBcWD9sQPWUobvsbfu
  ```

---

## File Structure

| File | Responsibility |
|---|---|
| `src/tdb/examine.py` (create) | `collect(...)` builds the record dict from a stopped controller; `write(...)` serializes and writes to sinks; `open_sinks(...)` turns DEST strings into streams; `EXAMINE_FRAME_CAP`. No signal or loop logic. |
| `src/tdb/_timeouts.py` (modify) | New constant `EXAMINE_CHILD` (per-child-process collection timeout). |
| `src/tdb/run_mode.py` (modify) | `_arm_signals`/`_disarm_signals` take an `examine` trigger; `run()` takes `examine_dests`, opens sinks, adds the `examine` event and capture cycle to the loop, prints the extended startup hint. |
| `src/tdb/cli.py` (modify) | `--examine-log` flag, validation, dispatch into `run_mode.run`. |
| `README.md` (modify) | Run Mode section: examine key, flag, record shape, caveat, `jq` example. |
| `tests/unit/test_examine.py` (create) | Collector + writer + sink-opening unit tests with fakes. |
| `tests/unit/test_run_mode_examine_loop.py` (create) | Loop behavior with a class-level-monkeypatched `DebugController`. |
| `tests/unit/test_cli_examine.py` (create) | Flag parsing and validation. |
| `tests/integration/test_run_mode.py` (modify) | Real-debugpy SIGUSR2 captures: threads, asyncio, multiprocessing, log-file append. |
| `tests/integration/test_go_session.py`, `tests/integration/test_rust_run_mode.py` (modify) | One examine capture each under existing toolchain skips. |

Existing pieces consumed (do not modify):

- `DebugController` (`src/tdb/session/controller.py`): `.client` (parent `DAPClient`), `._child_clients: dict[int, DAPClient]` (pid → client), `.profile` (has `.id` and `.capabilities.task_inspection` / `.capabilities.concurrency_inspection`), `.state.phase`, `async pause(timeout) -> bool` (pauses parent and all children), `async continue_()`, `async evaluate_on_parent(expr) -> str`.
- `DAPClient`: `async threads() -> list[Thread]` (`Thread.id`, `Thread.name`), `async stack_trace(thread_id, start_frame=0, levels=20) -> list[StackFrame]` (`StackFrame.name`, `.line`, `.source: Source | None`, `Source.path`).
- `InspectService(provider)` (`src/tdb/session/inspect_service.py`): `async collect_tasks() -> list[AsyncTaskInfo]` (`.name`, `.state`, `.awaiting`, `.stack: list[str]`), `async collect_processes() -> list[ProcessInfo]` (`.name`, `.pid`), `async collect_go_concurrency()` → object with `.to_dict()`, `async collect_rust_concurrency()` → object with `.to_dict()`. All raise `SessionGateError` when unsupported.
- `run_mode.py`: `ConsoleRunHandler` (events `initialized`, `stopped`, `exited`, `exit_code`), `_wait_first(*events)`, `_PAUSE_TIMEOUT`, `start_session`, `stop_session_on_error`, `configure_when_initialized`.

---

### Task 1: Collector — header, threads, frames

**Files:**
- Create: `src/tdb/examine.py`
- Modify: `src/tdb/_timeouts.py` (append after `ADAPTER_LISTEN`)
- Test: `tests/unit/test_examine.py`

**Interfaces:**
- Produces:
  ```python
  EXAMINE_FRAME_CAP: int = 200
  async def collect(
      controller, *, trigger: str, seq: int, requested_at: str,
      landed_at: str | None, launched_at: float, status: str = "ok",
      exit_code: int | None = None,
  ) -> dict
  ```
  `launched_at` is a `time.monotonic()` reading taken just before `controller.start`. `status` is `"ok"`, `"pending"`, or `"exited"`. For `"pending"` and `"exited"` the function returns the header only (empty `processes`, no stacks, no DAP calls).
- Produces in `_timeouts.py`: `EXAMINE_CHILD = 2.0`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_examine.py`:

```python
"""Examine collector: builds the run-mode snapshot record from a stopped
controller. All DAP traffic is faked; no adapter, no TUI."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from tdb.dap.types import Source, StackFrame, Thread
from tdb.session.errors import SessionGateError


class FakeClient:
    def __init__(self, threads, frames_by_thread, *, delay=0.0, fail=False):
        self._threads = threads
        self._frames = frames_by_thread
        self.delay = delay
        self.fail = fail
        self.levels_seen = []

    async def threads(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("threads boom")
        return list(self._threads)

    async def stack_trace(self, thread_id, start_frame=0, levels=20):
        self.levels_seen.append(levels)
        return list(self._frames.get(thread_id, []))


def make_controller(
    *,
    language="python",
    task_inspection=True,
    concurrency=None,
    client=None,
    children=None,
    pid_expr_result="4321",
):
    caps = SimpleNamespace(
        task_inspection=task_inspection, concurrency_inspection=concurrency
    )
    profile = SimpleNamespace(id=language, capabilities=caps)
    state = SimpleNamespace(is_terminated=False, is_running=False)

    async def evaluate_on_parent(expr):
        return pid_expr_result

    return SimpleNamespace(
        profile=profile,
        state=state,
        client=client or FakeClient([], {}),
        _child_clients=children or {},
        evaluate_on_parent=evaluate_on_parent,
    )


def frame(name, path, line):
    return StackFrame(
        id=1, name=name, source=Source(path=path) if path else None, line=line
    )


async def _collect(controller, **kw):
    from tdb.examine import collect

    kw.setdefault("trigger", "SIGQUIT")
    kw.setdefault("seq", 1)
    kw.setdefault("requested_at", "2026-09-07T14:02:11.482-07:00")
    kw.setdefault("landed_at", "2026-09-07T14:02:11.511-07:00")
    kw.setdefault("launched_at", 0.0)
    return await collect(controller, **kw)


async def test_header_fields_and_json_serializable(monkeypatch):
    from tdb import examine

    monkeypatch.setattr(examine.time, "monotonic", lambda: 87.34)
    ctrl = make_controller(task_inspection=False)
    rec = await _collect(ctrl, launched_at=0.0)
    assert rec["schema"] == 1
    assert rec["seq"] == 1
    assert rec["trigger"] == "SIGQUIT"
    assert rec["status"] == "ok"
    assert rec["requested_at"] == "2026-09-07T14:02:11.482-07:00"
    assert rec["landed_at"] == "2026-09-07T14:02:11.511-07:00"
    assert rec["elapsed_s"] == 87.3
    assert rec["language"] == "python"
    assert rec["errors"] == []
    json.dumps(rec)  # must be serializable as-is


async def test_threads_and_frames_innermost_first_with_null_source():
    client = FakeClient(
        [Thread(id=1, name="MainThread"), Thread(id=2, name="worker")],
        {
            1: [
                frame("wait", "/usr/lib/python3.13/threading.py", 359),
                frame("main", "/home/al/app.py", 42),
            ],
            2: [frame("<native>", None, 0)],
        },
    )
    ctrl = make_controller(task_inspection=False, client=client)
    rec = await _collect(ctrl)
    assert len(rec["processes"]) == 1
    parent = rec["processes"][0]
    assert parent["role"] == "parent"
    assert parent["pid"] == 4321
    assert [t["name"] for t in parent["threads"]] == ["MainThread", "worker"]
    assert parent["threads"][0]["frames"] == [
        {"function": "wait", "file": "/usr/lib/python3.13/threading.py", "line": 359},
        {"function": "main", "file": "/home/al/app.py", "line": 42},
    ]
    assert parent["threads"][1]["frames"] == [
        {"function": "<native>", "file": None, "line": None}
    ]
    assert client.levels_seen == [200, 200]


async def test_pid_null_when_not_python():
    ctrl = make_controller(language="bash", task_inspection=False)
    rec = await _collect(ctrl)
    assert rec["processes"][0]["pid"] is None


async def test_pid_null_when_evaluate_returns_garbage():
    ctrl = make_controller(task_inspection=False, pid_expr_result="NameError: os")
    rec = await _collect(ctrl)
    assert rec["processes"][0]["pid"] is None
    assert any(e.startswith("pid:") for e in rec["errors"])


async def test_parent_threads_failure_is_recorded_not_raised():
    ctrl = make_controller(task_inspection=False, client=FakeClient([], {}, fail=True))
    rec = await _collect(ctrl)
    assert rec["processes"][0]["threads"] == []
    assert any("parent" in e and "threads boom" in e for e in rec["errors"])


async def test_pending_and_exited_records_skip_dap():
    ctrl = make_controller(task_inspection=False, client=FakeClient([], {}, fail=True))
    pend = await _collect(ctrl, status="pending", landed_at=None)
    assert pend["status"] == "pending"
    assert "landed_at" not in pend
    assert pend["processes"] == []
    assert pend["errors"] == []  # no DAP call was made
    ex = await _collect(ctrl, status="exited", landed_at=None, exit_code=7)
    assert ex["status"] == "exited"
    assert ex["exit_code"] == 7
    assert ex["processes"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_examine.py -p no:cacheprovider --no-cov -q`
Expected: FAIL / ERROR with `ModuleNotFoundError: No module named 'tdb.examine'`

- [ ] **Step 3: Add the timeout constant**

Append to `src/tdb/_timeouts.py`, inside the `DAP protocol round-trips` group after `ADAPTER_LISTEN`:

```python
# Run-mode examine reads each child process's threads and stacks in
# turn. A child that is mid-fork or wedged in native code can leave
# `threads` unanswered; bound it so one slow child can't stall the
# whole snapshot (the parent and other children still get captured).
EXAMINE_CHILD = 2.0
"""Per-child-process budget for the examine collector."""
```

- [ ] **Step 4: Write the collector (header + threads)**

Create `src/tdb/examine.py`:

```python
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
    if not controller.profile.capabilities.task_inspection:
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
```

`controller.program` does not exist yet on `DebugController`; Task 4 passes the program path through instead. For now the `getattr` default keeps the record serializable. **Task 4 replaces this line** with a `program` keyword argument — see that task.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_examine.py -p no:cacheprovider --no-cov -q`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/tdb/examine.py src/tdb/_timeouts.py tests/unit/test_examine.py
git commit -m "examine: collector header, parent threads and frames"
```

---

### Task 2: Collector — child processes, tasks, Go and Rust snapshots

**Files:**
- Modify: `src/tdb/examine.py`
- Test: `tests/unit/test_examine.py`

**Interfaces:**
- Consumes: `collect(...)` from Task 1; `InspectService` from `tdb.session.inspect_service`.
- Produces: the full record shape from the spec (per-process `tasks`, top-level `goroutines` / `rust_concurrency`, child process entries with `role: "child"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_examine.py`:

```python
class FakeInspect:
    """Stands in for InspectService; each method either returns its
    configured value or raises."""

    def __init__(self, *, tasks=None, processes=None, go=None, rust=None):
        self._tasks, self._processes, self._go, self._rust = tasks, processes, go, rust

    async def collect_tasks(self):
        return _maybe(self._tasks)

    async def collect_processes(self):
        return _maybe(self._processes)

    async def collect_go_concurrency(self):
        return _maybe(self._go)

    async def collect_rust_concurrency(self):
        return _maybe(self._rust)


def _maybe(v):
    if isinstance(v, Exception):
        raise v
    return v if v is not None else []


class Dictable:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


@pytest.fixture
def inspect_factory(monkeypatch):
    """Patch examine.InspectService to return a preconfigured FakeInspect."""
    from tdb import examine

    def install(fake):
        monkeypatch.setattr(examine, "InspectService", lambda provider: fake)
        return fake

    return install


async def test_python_tasks_and_process_name(inspect_factory):
    task = SimpleNamespace(
        name="worker-2",
        state="PENDING",
        awaiting="Lock.acquire",
        stack=["run (/home/al/app.py:18)"],
    )
    inspect_factory(FakeInspect(tasks=[task], processes=[]))
    ctrl = make_controller()
    rec = await _collect(ctrl)
    parent = rec["processes"][0]
    assert parent["tasks"] == [
        {
            "name": "worker-2",
            "state": "PENDING",
            "awaiting": "Lock.acquire",
            "frames": ["run (/home/al/app.py:18)"],
        }
    ]
    assert "goroutines" not in rec and "rust_concurrency" not in rec


async def test_tasks_key_absent_when_none_and_when_unsupported(inspect_factory):
    inspect_factory(FakeInspect(tasks=[], processes=[]))
    rec = await _collect(make_controller())
    assert "tasks" not in rec["processes"][0]
    inspect_factory(FakeInspect(tasks=SessionGateError("unsupported")))
    rec = await _collect(make_controller(language="bash", task_inspection=False))
    assert "tasks" not in rec["processes"][0]
    assert rec["errors"] == []  # unsupported is not an error


async def test_task_collector_failure_recorded(inspect_factory):
    inspect_factory(FakeInspect(tasks=RuntimeError("evaluate failed"), processes=[]))
    rec = await _collect(make_controller())
    assert "tasks" not in rec["processes"][0]
    assert "tasks: evaluate failed" in rec["errors"]


async def test_children_follow_parent_in_pid_order(inspect_factory):
    inspect_factory(
        FakeInspect(
            tasks=[],
            processes=[
                SimpleNamespace(name="Process-2", pid=5002),
                SimpleNamespace(name="Process-1", pid=5001),
            ],
        )
    )
    c1 = FakeClient([Thread(id=1, name="MainThread")], {1: [frame("f", "/c.py", 3)]})
    c2 = FakeClient([Thread(id=1, name="MainThread")], {1: []})
    ctrl = make_controller(children={5002: c2, 5001: c1})
    rec = await _collect(ctrl)
    procs = rec["processes"]
    assert [p["role"] for p in procs] == ["parent", "child", "child"]
    assert [p["pid"] for p in procs[1:]] == [5001, 5002]
    assert [p["name"] for p in procs[1:]] == ["Process-1", "Process-2"]
    assert procs[1]["threads"][0]["frames"] == [
        {"function": "f", "file": "/c.py", "line": 3}
    ]


async def test_slow_child_times_out_others_still_captured(inspect_factory, monkeypatch):
    from tdb import examine

    monkeypatch.setattr(examine, "EXAMINE_CHILD", 0.05)
    inspect_factory(FakeInspect(tasks=[], processes=[]))
    slow = FakeClient([Thread(id=1, name="T")], {}, delay=1.0)
    fast = FakeClient([Thread(id=1, name="T")], {1: [frame("g", "/d.py", 9)]})
    ctrl = make_controller(children={7001: slow, 7002: fast})
    rec = await _collect(ctrl)
    assert rec["processes"][1]["threads"] == []
    assert "child 7001: timeout" in rec["errors"]
    assert rec["processes"][2]["threads"][0]["frames"][0]["function"] == "g"


async def test_go_snapshot_key_only_for_go(inspect_factory):
    inspect_factory(FakeInspect(go=Dictable({"goroutines": [{"goid": 1}]})))
    rec = await _collect(
        make_controller(language="go", task_inspection=False, concurrency="go")
    )
    assert rec["goroutines"] == {"goroutines": [{"goid": 1}]}
    assert "rust_concurrency" not in rec and "tasks" not in rec["processes"][0]


async def test_rust_snapshot_key_only_for_rust(inspect_factory):
    inspect_factory(FakeInspect(rust=Dictable({"threads": []})))
    rec = await _collect(
        make_controller(language="rust", task_inspection=False, concurrency="rust")
    )
    assert rec["rust_concurrency"] == {"threads": []}
    assert "goroutines" not in rec


async def test_concurrency_snapshot_failure_recorded(inspect_factory):
    inspect_factory(FakeInspect(go=RuntimeError("dlv gone")))
    rec = await _collect(
        make_controller(language="go", task_inspection=False, concurrency="go")
    )
    assert "goroutines" not in rec
    assert "goroutines: dlv gone" in rec["errors"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_examine.py -p no:cacheprovider --no-cov -q`
Expected: the 8 new tests FAIL (`AttributeError: module 'tdb.examine' has no attribute 'InspectService'` or missing keys); the Task 1 tests still pass.

- [ ] **Step 3: Extend the collector**

In `src/tdb/examine.py`, add the import (module level, not under `TYPE_CHECKING` — the tests monkeypatch it):

```python
from tdb.session.errors import SessionGateError
from tdb.session.inspect_service import InspectService
```

Add helpers after `_parent_pid`:

```python
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
```

Replace the tail of `collect` (everything from `parent: dict[str, Any] = {` to `return record`) with:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_examine.py -p no:cacheprovider --no-cov -q`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add src/tdb/examine.py tests/unit/test_examine.py
git commit -m "examine: child processes, asyncio tasks, Go/Rust snapshots"
```

---

### Task 3: Writer and sink opening

**Files:**
- Modify: `src/tdb/examine.py`
- Test: `tests/unit/test_examine.py`

**Interfaces:**
- Produces:
  ```python
  def open_sinks(dests: list[str] | None) -> list[TextIO]
      # None or [] -> [sys.stdout]; "-" -> sys.stdout; path -> open(path, "a", encoding="utf-8")
      # raises OSError on an unopenable path (caller reports and exits 2)
  def close_sinks(sinks: list[TextIO]) -> None   # closes everything except sys.stdout/sys.stderr
  def sink_names(sinks: list[TextIO]) -> str     # "stdout, hang.jsonl" for the stderr notice
  def write(record: dict, sinks: list[TextIO], *, notice: TextIO = sys.stderr) -> None
  ```

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_examine.py`:

```python
import io


class BrokenSink(io.StringIO):
    name = "broken.jsonl"

    def write(self, s):
        raise OSError("disk full")


def test_write_serializes_once_compact_newline_and_flushes():
    from tdb.examine import write

    class Sink(io.StringIO):
        name = "a.jsonl"
        flushes = 0

        def flush(self):
            self.flushes += 1
            super().flush()

    a, b = Sink(), Sink()
    b.name = "b.jsonl"
    note = io.StringIO()
    write({"schema": 1, "seq": 2, "x": [1, 2]}, [a, b], notice=note)
    assert a.getvalue() == '{"schema":1,"seq":2,"x":[1,2]}\n'
    assert a.getvalue() == b.getvalue()
    assert a.flushes >= 1 and b.flushes >= 1
    assert note.getvalue() == "tdb: examine #2 written to a.jsonl, b.jsonl\n"


def test_write_reports_broken_sink_once_and_continues():
    from tdb.examine import write

    good = io.StringIO()
    good.name = "good.jsonl"
    bad = BrokenSink()
    note = io.StringIO()
    write({"seq": 1}, [bad, good], notice=note)
    write({"seq": 2}, [bad, good], notice=note)
    assert good.getvalue().count("\n") == 2
    msgs = note.getvalue().splitlines()
    assert sum("broken.jsonl" in m and "disk full" in m for m in msgs) == 1
    assert "tdb: examine #1 written to good.jsonl" in msgs
    assert "tdb: examine #2 written to good.jsonl" in msgs


def test_open_sinks_default_dash_and_path(tmp_path):
    from tdb.examine import close_sinks, open_sinks, sink_names

    assert open_sinks(None) == [sys.stdout]
    assert open_sinks([]) == [sys.stdout]
    p = tmp_path / "hang.jsonl"
    p.write_text("existing\n")
    sinks = open_sinks(["-", str(p)])
    assert sinks[0] is sys.stdout
    assert sink_names(sinks) == f"stdout, {p}"
    sinks[1].write("new\n")
    close_sinks(sinks)
    assert sinks[1].closed and not sys.stdout.closed
    assert p.read_text() == "existing\nnew\n"  # append, never truncate


def test_open_sinks_bad_path_raises_and_creates_no_dirs(tmp_path):
    from tdb.examine import open_sinks

    target = tmp_path / "missing" / "hang.jsonl"
    with pytest.raises(OSError):
        open_sinks([str(target)])
    assert not (tmp_path / "missing").exists()
```

Add `import sys` to the top of the test file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_examine.py -p no:cacheprovider --no-cov -q`
Expected: 4 new FAIL with `ImportError: cannot import name 'write'` (and friends)

- [ ] **Step 3: Write the sink helpers and writer**

Append to `src/tdb/examine.py`:

```python
# --- sinks -----------------------------------------------------------------

STDOUT_DEST = "-"


def open_sinks(dests: list[str] | None) -> list[TextIO]:
    """Resolve --examine-log DESTs. Files open in append mode up front so
    a bad path fails before the program launches; directories are not
    created. OSError propagates to the caller."""
    if not dests:
        return [sys.stdout]
    sinks: list[TextIO] = []
    for d in dests:
        if d == STDOUT_DEST:
            sinks.append(sys.stdout)
        else:
            sinks.append(open(d, "a", encoding="utf-8"))
    return sinks


def close_sinks(sinks: list[TextIO]) -> None:
    for s in sinks:
        if s is sys.stdout or s is sys.stderr:
            continue
        try:
            s.close()
        except Exception:
            log.debug("closing examine sink failed", exc_info=True)


def _sink_name(s: TextIO) -> str:
    if s is sys.stdout:
        return "stdout"
    return str(getattr(s, "name", "?"))


def sink_names(sinks: list[TextIO]) -> str:
    return ", ".join(_sink_name(s) for s in sinks)


_reported_broken: set[int] = set()


def write(
    record: dict[str, Any], sinks: list[TextIO], *, notice: TextIO = sys.stderr
) -> None:
    """Write `record` as one compact JSON line to every sink and flush.
    A sink that fails is reported once (per sink object) and skipped
    thereafter; the run continues."""
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    written: list[TextIO] = []
    for s in sinks:
        try:
            s.write(line)
            s.flush()
            written.append(s)
        except Exception as exc:
            if id(s) not in _reported_broken:
                _reported_broken.add(id(s))
                print(
                    f"tdb: examine: cannot write to {_sink_name(s)}: {exc}", file=notice
                )
    if written:
        print(
            f"tdb: examine #{record.get('seq')} written to {sink_names(written)}",
            file=notice,
        )
    notice.flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_examine.py -p no:cacheprovider --no-cov -q`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add src/tdb/examine.py tests/unit/test_examine.py
git commit -m "examine: JSON Lines writer and sink handling"
```

---

### Task 4: Run-mode signals and the capture cycle

**Files:**
- Modify: `src/tdb/run_mode.py` (`_arm_signals`, `_disarm_signals`, `run`)
- Modify: `src/tdb/examine.py` (`collect` takes `program`)
- Test: `tests/unit/test_run_mode_examine_loop.py`

**Interfaces:**
- Consumes: `collect`, `write`, `open_sinks`, `close_sinks`, `sink_names`, `now_iso` from `tdb.examine`.
- Produces:
  ```python
  def _arm_signals(loop, trigger, examine: Callable[[str], None] | None = None) -> list
  # examine receives the signal name ("SIGQUIT" / "SIGUSR2" / "SIGBREAK")
  async def run(..., examine_dests: list[str] | None = None, ...) -> int
  ```
  `collect(...)` gains keyword `program: str | None`.

- [ ] **Step 1: Change `collect` to take `program`**

In `src/tdb/examine.py`, add `program: str | None = None,` to the keyword parameters of `collect` (after `launched_at`) and replace

```python
    record["program"] = getattr(controller, "program", None)
```
with
```python
    record["program"] = program
```

Run: `python -m pytest tests/unit/test_examine.py -p no:cacheprovider --no-cov -q` — Expected: 18 passed (tests never asserted on `program`).

- [ ] **Step 2: Write the failing loop tests**

Create `tests/unit/test_run_mode_examine_loop.py`:

```python
"""Run-mode examine cycle, driven with a class-level-patched
DebugController: no adapter, no debuggee. Signals are raised at the
process itself (POSIX only), exactly as a terminal would deliver them."""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal

import pytest

from tdb import run_mode
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")


class Script:
    """Records controller calls and lets tests decide when a pause lands."""

    def __init__(self):
        self.calls: list[str] = []
        self.pause_lands = True
        self.console: run_mode.ConsoleRunHandler | None = None
        self.controller: DebugController | None = None

    def stop_now(self, reason="pause"):
        self.controller.state.enter_stop(1, reason)
        self.console.on_stopped(1, reason, None, None)


@pytest.fixture
def script(monkeypatch):
    s = Script()

    async def start(self, **kw):
        s.controller = self
        s.calls.append("start")
        self.state.transition_to(SessionPhase.RUNNING)

    async def do_configure(self):
        s.calls.append("configure")

    async def pause(self, timeout=2.0):
        s.calls.append("pause")
        if s.pause_lands:
            s.stop_now()
            return True
        return False

    async def continue_(self):
        s.calls.append("continue")
        self.state.transition_to(SessionPhase.RUNNING)
        s.console.on_continued()

    async def stop(self):
        s.calls.append("stop")

    for name, fn in [
        ("start", start),
        ("do_configure", do_configure),
        ("pause", pause),
        ("continue_", continue_),
        ("stop", stop),
    ]:
        monkeypatch.setattr(DebugController, name, fn)

    async def fake_collect(controller, **kw):
        s.calls.append(f"collect:{kw['status']}:{kw['seq']}:{kw['trigger']}")
        return {"seq": kw["seq"], "status": kw["status"], "trigger": kw["trigger"]}

    monkeypatch.setattr(run_mode.examine, "collect", fake_collect)
    return s


def _capture_console(script):
    orig = run_mode.ConsoleRunHandler

    class Hooked(orig):
        def __init__(self):
            super().__init__()
            script.console = self

    return Hooked


async def _run(script, monkeypatch, sink, *, driver, episode=None):
    monkeypatch.setattr(run_mode, "ConsoleRunHandler", _capture_console(script))
    monkeypatch.setattr(run_mode.examine, "open_sinks", lambda dests: [sink])

    async def default_episode(controller, handler, console, config, program):
        script.calls.append("episode")
        return True  # detach

    async def ready_then_drive(controller):
        script.console.initialized.set()
        await driver()

    def on_ready(controller):
        asyncio.get_running_loop().create_task(ready_then_drive(controller))

    monkeypatch.setattr(run_mode, "configure_when_initialized", _configure_immediately)
    return await asyncio.wait_for(
        run_mode.run(
            program="/p.py",
            config=TdbConfig(),
            tui_episode=episode or default_episode,
            on_session_ready=on_ready,
            examine_dests=["-"],
        ),
        timeout=10,
    )


async def _configure_immediately(console, controller):
    await controller.do_configure()


def _records(sink):
    return [json.loads(l) for l in sink.getvalue().splitlines()]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


async def test_sigusr2_pause_collect_write_continue(script, monkeypatch):
    sink = io.StringIO()
    sink.name = "s"

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.console.on_exited(0)

    code = await _run(script, monkeypatch, sink, driver=driver)
    assert code == 0
    assert script.calls == [
        "start",
        "configure",
        "pause",
        "collect:ok:1:SIGUSR2",
        "continue",
    ]
    assert [r["seq"] for r in _records(sink)] == [1]


async def test_sigquit_and_repeat_presses_coalesce_and_number_sequentially(
    script, monkeypatch
):
    sink = io.StringIO()
    sink.name = "s"

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGQUIT)
        os.kill(os.getpid(), signal.SIGQUIT)
        await _settle()
        os.kill(os.getpid(), signal.SIGQUIT)
        await _settle()
        script.console.on_exited(3)

    code = await _run(script, monkeypatch, sink, driver=driver)
    assert code == 3
    assert script.calls.count("pause") == 2
    assert [r["seq"] for r in _records(sink)] == [1, 2]
    assert _records(sink)[0]["trigger"] == "SIGQUIT"


async def test_pending_then_landed_completes_without_tui(script, monkeypatch):
    sink = io.StringIO()
    sink.name = "s"
    script.pause_lands = False

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.stop_now()  # the pause finally lands
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" not in script.calls
    assert script.calls == [
        "start",
        "configure",
        "pause",
        "collect:pending:1:SIGUSR2",
        "collect:ok:1:SIGUSR2",
        "continue",
    ]
    recs = _records(sink)
    assert [(r["seq"], r["status"]) for r in recs] == [(1, "pending"), (1, "ok")]


async def test_ctrl_c_cancels_outstanding_examine_and_opens_tui(script, monkeypatch):
    sink = io.StringIO()
    sink.name = "s"
    script.pause_lands = False

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.pause_lands = True
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls
    assert "collect:ok:1:SIGUSR2" not in script.calls
    assert [r["status"] for r in _records(sink)] == ["pending"]


async def test_interrupt_wins_over_simultaneous_examine(script, monkeypatch):
    sink = io.StringIO()
    sink.name = "s"

    async def driver():
        await _settle()
        # Both delivered before the loop wakes: SIGINT is armed on the loop
        # too, so raise them back to back.
        os.kill(os.getpid(), signal.SIGUSR2)
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls
    assert not any(c.startswith("collect") for c in script.calls)
    assert _records(sink) == []


async def test_exit_during_capture_writes_exited_record(script, monkeypatch):
    sink = io.StringIO()
    sink.name = "s"

    async def pause(self, timeout=2.0):
        script.calls.append("pause")
        script.console.on_exited(9)
        return False

    monkeypatch.setattr(DebugController, "pause", pause)

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)

    code = await _run(script, monkeypatch, sink, driver=driver)
    assert code == 9
    assert script.calls[-1] == "collect:exited:1:SIGUSR2"
    assert _records(sink)[0]["status"] == "exited"


async def test_late_landing_ctrl_c_stop_still_opens_tui(script, monkeypatch):
    """Regression guard for the stray-pause rule: a Ctrl-C whose pause
    lands late must still open the TUI when the stop arrives."""
    sink = io.StringIO()
    sink.name = "s"
    script.pause_lands = False

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.stop_now("pause")  # the Ctrl-C pause finally lands
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls
    assert _records(sink) == []


async def test_stray_pause_stop_is_resumed_not_debugged(script, monkeypatch):
    """A `stopped(reason=pause)` with nothing waiting on it (a child's
    late pause-all stop after an examine already resumed everything)
    must be continued, not turned into a TUI episode."""
    sink = io.StringIO()
    sink.name = "s"

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGUSR2)
        await _settle()
        script.stop_now("pause")  # late child stop
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" not in script.calls
    assert script.calls.count("continue") == 2
    assert [r["seq"] for r in _records(sink)] == [1]


async def test_breakpoint_stop_still_opens_tui(script, monkeypatch):
    """The stray-pause rule must not swallow real stops."""
    sink = io.StringIO()
    sink.name = "s"

    async def driver():
        await _settle()
        script.stop_now("breakpoint")
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver)
    assert "episode" in script.calls


async def test_examine_signals_ignored_during_episode_and_restored_after(
    script, monkeypatch
):
    sink = io.StringIO()
    sink.name = "s"
    seen = {}

    async def episode(controller, handler, console, config, program):
        seen["during"] = signal.getsignal(signal.SIGQUIT)
        return True

    async def driver():
        await _settle()
        os.kill(os.getpid(), signal.SIGINT)
        await _settle()
        script.console.on_exited(0)

    await _run(script, monkeypatch, sink, driver=driver, episode=episode)
    assert seen["during"] is signal.SIG_IGN
    assert signal.getsignal(signal.SIGQUIT) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGUSR2) is signal.SIG_DFL
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_run_mode_examine_loop.py -p no:cacheprovider --no-cov -q`
Expected: all FAIL — `AttributeError: module 'tdb.run_mode' has no attribute 'examine'` (first failure), then `TypeError: run() got an unexpected keyword argument 'examine_dests'`.

- [ ] **Step 4: Extend the signal helpers**

In `src/tdb/run_mode.py`, add the import after `from tdb.session.event_bus import SwappableEventHandler`:

```python
from tdb import examine
```

Replace `_arm_signals` and `_disarm_signals` entirely:

```python
# Examine trigger signals per platform: the terminal driver turns
# Ctrl-\ into SIGQUIT (POSIX) and Ctrl-Break into SIGBREAK (Windows)
# while the terminal stays in cooked mode, so tdb never touches
# terminal settings. SIGUSR2 mirrors the SIGUSR1 convention for
# out-of-band triggering from another terminal.
if os.name != "nt":
    EXAMINE_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGQUIT, signal.SIGUSR2)
    EXAMINE_KEY = "Ctrl-\\"
else:  # pragma: no cover - Windows only
    EXAMINE_SIGNALS = (signal.SIGBREAK,)
    EXAMINE_KEY = "Ctrl-Break"


def _arm_signals(
    loop: asyncio.AbstractEventLoop,
    trigger: Callable[[], None],
    examine_trigger: Callable[[str], None] | None = None,
) -> list:
    """Route SIGINT (and SIGUSR1 on POSIX) to `trigger`, and the examine
    signals to `examine_trigger(signal_name)` when given.

    Returns the list of signals actually armed. Failure (non-main
    thread — embedded use, some test runners) degrades to "no signal
    interruption" rather than crashing run mode.
    """
    installed: list = []
    try:
        if os.name != "nt":
            for sig in (signal.SIGINT, signal.SIGUSR1):
                loop.add_signal_handler(sig, trigger)
                installed.append(sig)
            if examine_trigger is not None:
                for sig in EXAMINE_SIGNALS:
                    loop.add_signal_handler(sig, examine_trigger, sig.name)
                    installed.append(sig)
        else:
            signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(trigger))
            installed.append(signal.SIGINT)
            if examine_trigger is not None:
                for sig in EXAMINE_SIGNALS:
                    signal.signal(
                        sig,
                        lambda *_, _n=sig.name: loop.call_soon_threadsafe(
                            examine_trigger, _n
                        ),
                    )
                    installed.append(sig)
    except (ValueError, NotImplementedError, RuntimeError):
        log.warning("cannot install run-mode signal handlers", exc_info=True)
    return installed


def _disarm_signals(
    loop: asyncio.AbstractEventLoop, installed: list, *, ignore: bool
) -> None:
    """Remove run-mode handlers.

    ignore=True while a TUI episode owns the terminal: a stray SIGUSR1 or
    SIGUSR2 must be a no-op, not the default action (which kills the
    process — SIGQUIT's default even dumps core).
    ignore=False on final exit: restore Python defaults.
    """
    for sig in installed:
        if os.name != "nt":
            try:
                loop.remove_signal_handler(sig)
            except (ValueError, RuntimeError):
                pass
        if ignore:
            handler = signal.SIG_IGN
        elif sig == signal.SIGINT:
            handler = signal.default_int_handler
        else:
            handler = signal.SIG_DFL
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
```

- [ ] **Step 5: Extend `run()`**

Change the signature: add `examine_dests: list[str] | None = None,` after `on_session_ready`.

Replace the body of `run()` from `console = ConsoleRunHandler()` through the end of the function with:

```python
try:
    sinks = examine.open_sinks(examine_dests)
except OSError as exc:
    print(f"tdb: cannot open examine log: {exc}", file=sys.stderr)
    return 2
console = ConsoleRunHandler()
handler = SwappableEventHandler(console)
controller = DebugController(handler, profile=profile)
controller.step_mode = config.step_mode
controller.adopted_session = True  # restart is never offered in run mode

import time

launched_at = time.monotonic()
try:
    bail = await start_session(
        controller,
        program=program,
        args=args,
        cwd=cwd or str(Path.cwd()),
        stop_on_entry=False,
        just_my_code=just_my_code,
        python=python,
        sub_process=sub_process,
    )
    if bail is not None:
        return bail

    async with stop_session_on_error(controller):
        await configure_when_initialized(console, controller)
        if on_session_ready is not None:
            on_session_ready(controller)

        pid = os.getpid()
        hint = "Ctrl-C" if os.name == "nt" else f"Ctrl-C or `kill -USR1 {pid}`"
        ehint = (
            EXAMINE_KEY if os.name == "nt" else f"{EXAMINE_KEY} or `kill -USR2 {pid}`"
        )
        print(
            f"tdb: running {program} — {hint} opens the debugger; "
            f"{ehint} writes a stack snapshot to {examine.sink_names(sinks)}",
            file=sys.stderr,
        )

        loop = asyncio.get_running_loop()
        interrupt = asyncio.Event()
        examine_ev = asyncio.Event()
        examine_sig: list[str] = []  # name of the signal that set examine_ev

        def on_examine(name: str) -> None:
            examine_sig.append(name)
            examine_ev.set()

        episode = tui_episode or _default_tui_episode
        installed = _arm_signals(loop, interrupt.set, on_examine)
        exit_code = 0
        seq = 0
        # (seq, trigger, requested_at) of a capture whose pause hasn't
        # landed yet; completed on the next stopped event.
        outstanding: tuple[int, str, str] | None = None
        # True after a Ctrl-C pause that hasn't landed yet: the stop
        # that eventually arrives must open the TUI.
        interrupt_pending = False

        async def emit(
            status: str,
            seq_: int,
            trigger: str,
            requested: str,
            landed: str | None,
            exit_code_: int | None = None,
        ) -> None:
            record = await examine.collect(
                controller,
                trigger=trigger,
                seq=seq_,
                requested_at=requested,
                landed_at=landed,
                launched_at=launched_at,
                program=program,
                status=status,
                exit_code=exit_code_,
            )
            examine.write(record, sinks)

        try:
            while True:
                await _wait_first(
                    console.exited, interrupt, console.stopped, examine_ev
                )
                if console.exited.is_set():
                    exit_code = console.exit_code or 0
                    break

                if interrupt.is_set() and not console.stopped.is_set():
                    interrupt.clear()
                    examine_ev.clear()
                    examine_sig.clear()
                    outstanding = None  # Ctrl-C supersedes a pending examine
                    interrupt_pending = False
                    ok = await controller.pause(timeout=_PAUSE_TIMEOUT)
                    if console.exited.is_set():
                        # Died between the signal and the pause landing.
                        exit_code = console.exit_code or 0
                        print(
                            f"tdb: program exited (code {exit_code}) before "
                            "the debugger could open",
                            file=sys.stderr,
                        )
                        break
                    if not ok:
                        interrupt_pending = True
                        print(
                            "tdb: pause requested — the program is blocked inside "
                            "a single call; the debugger opens when it returns",
                            file=sys.stderr,
                        )
                        continue

                elif examine_ev.is_set() and not console.stopped.is_set():
                    examine_ev.clear()
                    trigger = examine_sig[-1] if examine_sig else "SIGUSR2"
                    examine_sig.clear()
                    if outstanding is not None:
                        continue  # one capture at a time; wait for it to land
                    seq += 1
                    requested = examine.now_iso()
                    ok = await controller.pause(timeout=_PAUSE_TIMEOUT)
                    if console.exited.is_set():
                        exit_code = console.exit_code or 0
                        await emit("exited", seq, trigger, requested, None, exit_code)
                        break
                    if not ok:
                        await emit("pending", seq, trigger, requested, None)
                        print(
                            "tdb: pause requested — the program is blocked inside "
                            "a single call; the snapshot is written when it returns",
                            file=sys.stderr,
                        )
                        outstanding = (seq, trigger, requested)
                        continue
                    await emit("ok", seq, trigger, requested, examine.now_iso())
                    examine_ev.clear()
                    examine_sig.clear()
                    console.stopped.clear()
                    await controller.continue_()
                    continue

                elif console.stopped.is_set() and outstanding is not None:
                    # A deferred examine's pause finally landed.
                    seq_, trigger, requested = outstanding
                    outstanding = None
                    await emit("ok", seq_, trigger, requested, examine.now_iso())
                    examine_ev.clear()
                    examine_sig.clear()
                    console.stopped.clear()
                    await controller.continue_()
                    continue

                elif (
                    console.stopped.is_set()
                    and not interrupt_pending
                    and not interrupt.is_set()
                    and console.last_stop is not None
                    and console.last_stop[1] == "pause"
                ):
                    # Stray pause-all stop: nobody is waiting for it. Typical
                    # cause is a child process whose `stopped` event for an
                    # examine pause arrives after we already resumed every
                    # client (the parent's stop is what released the wait).
                    # Resume again — harmless for anything already running —
                    # rather than opening the TUI on a pause nobody asked for.
                    console.stopped.clear()
                    await controller.continue_()
                    continue

                # Reached on a landed Ctrl-C pause, on a Ctrl-C pause that
                # landed late, or on a spontaneous stop (a breakpoint set
                # during a previous episode).
                interrupt_pending = False
                interrupt.clear()
                examine_ev.clear()
                examine_sig.clear()
                _disarm_signals(loop, installed, ignore=True)
                detach = await episode(controller, handler, console, config, program)
                handler.retarget(console)
                console.stopped.clear()
                if controller.state.is_terminated:
                    break
                if not detach:
                    await controller.stop()
                    break
                await controller.continue_()
                installed = _arm_signals(loop, interrupt.set, on_examine)
        finally:
            _disarm_signals(loop, installed, ignore=False)
        return exit_code
finally:
    examine.close_sinks(sinks)
```

Move `import time` to the module's import block (alphabetical, after `sys`) rather than leaving it inline.

- [ ] **Step 6: Run the loop tests and the existing run-mode unit tests**

Also append to `tests/unit/test_examine.py` (runs on every platform, so the Windows constants are at least import-checked there):

```python
def test_examine_signal_names_per_platform():
    import os
    import signal as _signal

    from tdb.run_mode import EXAMINE_KEY, EXAMINE_SIGNALS

    if os.name == "nt":
        assert EXAMINE_SIGNALS == (_signal.SIGBREAK,)
        assert EXAMINE_KEY == "Ctrl-Break"
    else:
        assert EXAMINE_SIGNALS == (_signal.SIGQUIT, _signal.SIGUSR2)
        assert EXAMINE_KEY == "Ctrl-\\"
```

Run: `python -m pytest tests/unit/test_run_mode_examine_loop.py tests/unit/test_examine.py tests/unit/test_console_run_handler.py tests/unit/test_app_adopted_session.py -p no:cacheprovider --no-cov -q`
Expected: all pass (10 new loop tests + 19 examine tests + existing). The Windows branch of `_arm_signals` cannot run on this machine; say so in the task report.

If `test_interrupt_wins_over_simultaneous_examine` is flaky because the loop wakes between the two `os.kill` calls, replace the two kills with `signal.raise_signal` calls inside a single `loop.call_soon` callback so both handlers are queued before the loop resumes; the priority logic under test (`interrupt` branch checked before `examine_ev`) is what matters.

- [ ] **Step 7: Run the existing run-mode integration tests**

Run: `python -m pytest tests/integration/test_run_mode.py -p no:cacheprovider --no-cov -q -k "not perl and not ruby"`
Expected: pass (Ctrl-C/SIGUSR1 behavior unchanged).

- [ ] **Step 8: Commit**

```bash
git add src/tdb/run_mode.py src/tdb/examine.py tests/unit/test_run_mode_examine_loop.py
git commit -m "run mode: examine trigger signals and capture cycle"
```

---

### Task 5: CLI flag, validation, dispatch

**Files:**
- Modify: `src/tdb/cli.py` (parser after the `--run` argument ~line 60; validation block starting `if args.run:` ~line 715; `_run_run` ~line 995)
- Test: `tests/unit/test_cli_examine.py`

**Interfaces:**
- Consumes: `run_mode.run(..., examine_dests=...)` from Task 4.
- Produces: `args.examine_log: list[str]` (default `[]`).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_cli_examine.py`:

```python
"""--examine-log parsing: repeatable, '-' means stdout, only with --run."""

import pytest

from tdb.cli import build_parser


def test_examine_log_default_empty():
    args = build_parser().parse_args(["--run", "prog.py"])
    assert args.examine_log == []


def test_examine_log_repeatable_in_order():
    args = build_parser().parse_args(
        ["--run", "--examine-log", "-", "--examine-log", "hang.jsonl", "prog.py"]
    )
    assert args.examine_log == ["-", "hang.jsonl"]


def test_examine_log_requires_run(capsys):
    from tdb.cli import parse_args

    with pytest.raises(SystemExit) as ei:
        parse_args(["--examine-log", "x.jsonl", "prog.py"])
    assert ei.value.code == 2
    assert "--examine-log requires --run" in capsys.readouterr().err


def test_run_help_mentions_examine():
    text = build_parser().format_help()
    assert "--examine-log" in text
    assert "snapshot" in text
```

`parse_args` (`src/tdb/cli.py:705`) is the public entry that builds the parser, parses, and runs the validation block containing the `if args.run:` conflict loop.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_cli_examine.py -p no:cacheprovider --no-cov -q`
Expected: FAIL — `AttributeError: 'Namespace' object has no attribute 'examine_log'` / unrecognized arguments.

- [ ] **Step 3: Add the flag**

In `build_parser()` in `src/tdb/cli.py`, directly after the `--run` `add_argument` call, add:

```python
    parser.add_argument(
        "--examine-log",
        action="append",
        default=[],
        metavar="DEST",
        help="With --run: where each stack snapshot goes when you press "
        "Ctrl-\\ (Ctrl-Break on Windows) or send SIGUSR2. DEST is '-' for "
        "stdout or a file path (opened in append mode). May be repeated "
        "to write to several places. Default: stdout.",
    )
```

Extend the `--run` help string by appending one clause:

```python
"inspecting programs that appear to be hung. Ctrl-\\ (Ctrl-Break"

"on Windows) or SIGUSR2 instead writes a JSON stack snapshot of "
("every thread and resumes; see --examine-log.",)
```

- [ ] **Step 4: Add validation**

In the argument-validation function, immediately after the `if args.run:` conflict block, add:

```python
    if args.examine_log and not args.run:
        parser.error("--examine-log requires --run")
```

- [ ] **Step 5: Pass it through**

In `_run_run`, add `examine_dests=args.examine_log,` to the `run(...)` call, after `config=load_config(),`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_cli_examine.py tests/unit -p no:cacheprovider --no-cov -q -k "cli or examine"`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/tdb/cli.py tests/unit/test_cli_examine.py
git commit -m "cli: --examine-log for run mode"
```

---

### Task 6: Integration tests against real debuggees (Python)

**Files:**
- Modify: `tests/integration/test_run_mode.py`

**Interfaces:**
- Consumes: `run_mode.run(..., examine_dests=[...])`, real debugpy session, `SIGUSR2` delivered to the test process (same pattern as the existing SIGUSR1 test).

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_run_mode.py`:

```python
import json

THREAD_SCRIPT = """\
import threading, time
def spin():
    while True:
        time.sleep(0.01)
threading.Thread(target=spin, name="spinner", daemon=True).start()
while True:
    time.sleep(0.01)
"""

ASYNC_SCRIPT = """\
import asyncio
async def waiter(lock):
    async with lock:
        await asyncio.sleep(3600)
async def main():
    lock = asyncio.Lock()
    asyncio.create_task(waiter(lock), name="holder")
    await asyncio.sleep(0)
    asyncio.create_task(waiter(lock), name="blocked")
    while True:
        await asyncio.sleep(0.01)
asyncio.run(main())
"""

MP_SCRIPT = """\
import multiprocessing, time
def child():
    while True:
        time.sleep(0.01)
if __name__ == "__main__":
    p = multiprocessing.Process(target=child, name="kid")
    p.start()
    while True:
        time.sleep(0.01)
"""


async def _examine_once(program: str, dests: list[str], captures_wanted: int = 1):
    """Run `program` headless, send SIGUSR2 `captures_wanted` times once
    the debuggee is running, then terminate via a TUI episode."""
    box = {}

    def ready(controller):
        box["controller"] = controller

    async def fake_episode(controller, handler, console, config, program):
        return False  # terminate

    async def pulses():
        await _wait_until(
            lambda: (
                box.get("controller") is not None
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        for _ in range(captures_wanted):
            os.kill(os.getpid(), signal.SIGUSR2)
            await asyncio.sleep(1.0)
        await _wait_until(lambda: box["controller"].state.phase is SessionPhase.RUNNING)
        os.kill(os.getpid(), signal.SIGUSR1)

    task = asyncio.create_task(pulses())
    code = await asyncio.wait_for(
        run_mode.run(
            program=program,
            config=TdbConfig(),
            tui_episode=fake_episode,
            on_session_ready=ready,
            examine_dests=dests,
        ),
        timeout=90.0,
    )
    await task
    return code


def _stdout_records(capfd):
    out = capfd.readouterr().out
    return [json.loads(l) for l in out.splitlines() if l.startswith("{")]


async def test_examine_threads_to_stdout(tmp_path, capfd):
    p = tmp_path / "threads.py"
    p.write_text(THREAD_SCRIPT)
    await _examine_once(str(p), ["-"])
    recs = _stdout_records(capfd)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["schema"] == 1 and rec["status"] == "ok" and rec["trigger"] == "SIGUSR2"
    assert rec["language"] == "python" and rec["program"] == str(p)
    assert "landed_at" in rec and rec["elapsed_s"] >= 0
    parent = rec["processes"][0]
    assert parent["role"] == "parent" and isinstance(parent["pid"], int)
    names = {t["name"] for t in parent["threads"]}
    assert "spinner" in names
    spinner = next(t for t in parent["threads"] if t["name"] == "spinner")
    assert any(
        f["function"] == "spin" and f["file"] == str(p) for f in spinner["frames"]
    )
    assert "goroutines" not in rec and "rust_concurrency" not in rec


async def test_examine_asyncio_tasks(tmp_path, capfd):
    p = tmp_path / "tasks.py"
    p.write_text(ASYNC_SCRIPT)
    await _examine_once(str(p), ["-"])
    rec = _stdout_records(capfd)[0]
    tasks = {t["name"]: t for t in rec["processes"][0].get("tasks", [])}
    assert {"holder", "blocked"} <= set(tasks)
    assert tasks["blocked"]["awaiting"] == "Lock.acquire"


async def test_examine_multiprocessing_children(tmp_path, capfd):
    p = tmp_path / "mp.py"
    p.write_text(MP_SCRIPT)
    await _examine_once(str(p), ["-"])
    rec = _stdout_records(capfd)[0]
    roles = [pr["role"] for pr in rec["processes"]]
    assert roles[0] == "parent" and "child" in roles
    child = next(pr for pr in rec["processes"] if pr["role"] == "child")
    assert isinstance(child["pid"], int)
    assert any(
        f["function"] == "child" for t in child["threads"] for f in t["frames"]
    ), rec


async def test_examine_log_file_appends_across_captures(tmp_path, capfd):
    p = tmp_path / "threads.py"
    p.write_text(THREAD_SCRIPT)
    log = tmp_path / "hang.jsonl"
    log.write_text('{"pre":true}\n')
    await _examine_once(str(p), [str(log)], captures_wanted=2)
    lines = log.read_text().splitlines()
    assert lines[0] == '{"pre":true}'
    seqs = [json.loads(l)["seq"] for l in lines[1:]]
    assert seqs == [1, 2]
    captured = capfd.readouterr()
    assert f"examine #1 written to {log}" in captured.err
    assert f"examine #2 written to {log}" in captured.err
    assert not [l for l in captured.out.splitlines() if l.startswith("{")]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/integration/test_run_mode.py -p no:cacheprovider --no-cov -q -k examine`
Expected: FAIL only if Tasks 4/5 are incomplete; with them in place these should pass on the first run. If a test fails, the failure is real — investigate rather than loosening the assertion.

- [ ] **Step 3: Run and fix**

Run: `python -m pytest tests/integration/test_run_mode.py -p no:cacheprovider --no-cov -q`
Expected: all pass, including the pre-existing SIGUSR1 tests.

Likely adjustments if a test fails:
- Multiprocessing child attach can take a few seconds (forkserver on 3.14): if the child is missing, increase the first `asyncio.sleep(1.0)` in `pulses` to `3.0` for that test by adding a `settle: float = 1.0` parameter to `_examine_once`.
- `asyncio` `awaiting` label depends on the innermost frame being in `locks.py`; if the label differs, assert on `"Lock" in tasks["blocked"]["awaiting"]`.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_run_mode.py
git commit -m "run mode: examine integration tests (threads, asyncio, multiprocessing, log file)"
```

---

### Task 7: Integration tests for Go and Rust snapshots

**Files:**
- Modify: `tests/integration/test_go_session.py`
- Modify: `tests/integration/test_rust_run_mode.py`

**Interfaces:**
- Consumes: `run_mode.run(..., examine_dests=["-"])` (Task 4); Go: the module's `pytestmark` (skips without `go`/`dlv`), `GO_BLOCKED_SRC`, `build_go_profile`; Rust: `rust_adapter_harness` exports `available_rust_adapters`, `_rust_debug_binary` (registers the `rust_debug_binary` fixture, returning a `RustDebugTarget` with `.program` and `.arguments(port, control=True)`), `_ready_listener()` (async context manager yielding `(port, fixture_ready)` where `fixture_ready` resolves to a `ReadyConnection` once the fixture program has parked), `WAIT`, and `tdb.languages.rust.build_rust_profile`.

- [ ] **Step 1: Add the Go test**

`go_blocked/main.go` parks three goroutines on a channel and one on a mutex, prints a marker after 200 ms, then sleeps 10 s — a long enough window for one capture. Append to `tests/integration/test_go_session.py`:

```python
async def test_run_mode_examine_includes_goroutines(capfd):
    """`tdb --run` + SIGUSR2 on a Go program: the record carries the
    goroutine snapshot under `goroutines` and no Rust key."""
    import json
    import os
    import signal

    from tdb import run_mode
    from tdb.persist import TdbConfig
    from tdb.session.state import SessionPhase

    box = {}

    def ready(controller):
        box["controller"] = controller

    async def episode(controller, handler, console, config, program):
        return False  # terminate

    async def pulses():
        deadline = asyncio.get_running_loop().time() + WAIT
        while not (
            box.get("controller") is not None
            and box["controller"].state.phase is SessionPhase.RUNNING
        ):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)  # let the workers park
        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.sleep(3.0)  # capture + resume
        os.kill(os.getpid(), signal.SIGUSR1)

    task = asyncio.create_task(pulses())
    await asyncio.wait_for(
        run_mode.run(
            program=str(GO_BLOCKED_SRC),
            config=TdbConfig(),
            profile=build_go_profile(),
            tui_episode=episode,
            on_session_ready=ready,
            examine_dests=["-"],
        ),
        timeout=WAIT * 4,
    )
    await task
    recs = [
        json.loads(line)
        for line in capfd.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert len(recs) == 1, recs
    rec = recs[0]
    assert rec["status"] == "ok" and rec["language"] == "go"
    assert "rust_concurrency" not in rec
    assert "tasks" not in rec["processes"][0]
    assert len(rec["goroutines"]["goroutines"]) >= 5
    assert rec["processes"][0]["threads"], rec
    assert rec["errors"] == [], rec
```

- [ ] **Step 2: Add the Rust test**

The Rust fixture needs the harness's ready listener: the program connects back on `port` once its threads are parked, and a `control=True` argument list lets the test release it afterwards. Append to `tests/integration/test_rust_run_mode.py`:

```python
import asyncio
import json
import os
import signal

from tdb import run_mode
from tdb.languages.rust import build_rust_profile
from tdb.persist import TdbConfig
from tests.integration.rust_adapter_harness import WAIT, _ready_listener


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_run_mode_examine_includes_rust_concurrency(
    adapter, rust_debug_binary, capfd
):
    """`tdb --run` + SIGUSR2 on a parked Rust program: the record carries
    the concurrency snapshot under `rust_concurrency` and no Go key."""
    target = rust_debug_binary("park", adapter)

    async def episode(controller, handler, console, config, program):
        return False  # terminate

    async with _ready_listener() as (port, fixture_ready):

        async def pulses():
            connection = await asyncio.wait_for(fixture_ready, WAIT)
            assert connection.scenario == target.scenario
            os.kill(os.getpid(), signal.SIGUSR2)
            await asyncio.sleep(5.0)  # native stacks are slower to walk
            os.kill(os.getpid(), signal.SIGUSR1)

        task = asyncio.create_task(pulses())
        await asyncio.wait_for(
            run_mode.run(
                program=target.program,
                args=target.arguments(port, control=True),
                profile=build_rust_profile(adapter=adapter),
                config=TdbConfig(),
                tui_episode=episode,
                examine_dests=["-"],
            ),
            WAIT * 3,
        )
        await task

    recs = [
        json.loads(line)
        for line in capfd.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert len(recs) == 1, recs
    rec = recs[0]
    assert rec["status"] == "ok" and rec["language"] == "rust"
    assert "goroutines" not in rec
    assert "threads" in rec["rust_concurrency"]
    assert rec["processes"][0]["threads"], rec
```

If the capture's `errors` list is non-empty for Rust (the snapshot collector has its own timeout and probe warnings), do not assert `errors == []` there; print `rec["errors"]` in the task report instead.

- [ ] **Step 3: Run under the toolchain skips**

Run: `python -m pytest tests/integration/test_go_session.py tests/integration/test_rust_run_mode.py -p no:cacheprovider --no-cov -q -k examine`
Expected: pass where `go`+`dlv` / a Rust adapter are installed; skipped otherwise. Report which happened.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_go_session.py tests/integration/test_rust_run_mode.py
git commit -m "run mode: examine integration tests for Go and Rust snapshots"
```

---

### Task 8: README

**Files:**
- Modify: `README.md` (Run Mode section, ~lines 1420-1478; CLI Reference section ~line 1756)

- [ ] **Step 1: Add the examine subsection**

In the Run Mode section, after the paragraph beginning `**Interrupting:**` and before `**Quitting an adopted session**`, insert:

````markdown
**Examining without opening the TUI:** press `Ctrl-\` in the terminal (`Ctrl-Break` on
Windows), or, on Unix, send `SIGUSR2` to `tdb`'s pid: `kill -USR2 <tdb pid>`. `tdb`
pauses the program, writes one JSON line describing the call stack of every thread
(plus asyncio tasks and multiprocessing children for Python, goroutines for Go, and
the concurrency snapshot for Rust), and resumes the program. Ctrl-C behavior is
unchanged. By default the line goes to stdout; `--examine-log DEST` sends it to a file
instead (`-` means stdout; repeat the flag to write to several places):

```bash
tdb --run --examine-log hang.jsonl my_program.py     # file only, appended
tdb --run --examine-log - --examine-log hang.jsonl my_program.py   # both
```

Each record looks like (abridged):

```json
{"schema":1,"seq":1,"trigger":"SIGQUIT","status":"ok",
 "requested_at":"2026-09-07T14:02:11.482-07:00","landed_at":"2026-09-07T14:02:11.511-07:00",
 "elapsed_s":87.3,"language":"python","program":"/home/al/work/app.py",
 "processes":[{"pid":41213,"role":"parent","name":null,
   "threads":[{"id":1,"name":"MainThread","frames":[
     {"function":"wait","file":"/usr/lib/python3.13/threading.py","line":359},
     {"function":"main","file":"/home/al/work/app.py","line":42}]}],
   "tasks":[{"name":"worker-2","state":"PENDING","awaiting":"Lock.acquire","frames":["run (/home/al/work/app.py:18)"]}]}],
 "errors":[]}
```

Frames are innermost first. `tasks` appears only for Python programs with live asyncio
tasks; `goroutines` only for Go; `rust_concurrency` only for Rust. `status` is
`"pending"` when the pause could not land within a few seconds (the program is blocked
inside a single call) -- a second record with the same `seq` and `status: "ok"` follows
when it does land. `errors` lists any sub-collector that failed, so a partial record is
explicit about what is missing. A stderr line confirms each capture and where it went.

Narrow a long capture with `jq`, for example only threads with a frame in your own code:

```bash
jq -c '.processes[].threads[] | select(.frames[].file | test("/work/"))' hang.jsonl
```
````

- [ ] **Step 2: Update the incompatible-flags line**

No change: `--examine-log` is compatible with `--run` only, which the flag's own help states. Add to the CLI Reference table (around line 1756) a row for `--examine-log DEST` with the one-line description `With --run: write each Ctrl-\ / SIGUSR2 stack snapshot to DEST (- = stdout; repeatable)`. Match the table's existing formatting.

- [ ] **Step 3: Verify rendering**

Run: `grep -n 'examine' README.md | head` — expect hits in the Run Mode section and the CLI Reference.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: run-mode examine"
```

---

### Task 9: Full verification

**Files:** none new.

- [ ] **Step 1: Unit suite**

Run: `python -m pytest tests/unit -p no:cacheprovider --no-cov -q`
Expected: all pass.

- [ ] **Step 2: Run-mode integration**

Run: `python -m pytest tests/integration/test_run_mode.py tests/integration/test_eval_mode.py -p no:cacheprovider --no-cov -q`
Expected: all pass (eval mode shares the signal helpers and must be unaffected).

- [ ] **Step 3: Manual smoke test**

In a terminal:

```bash
cd /home/al/projects/tdbg/work
printf 'import time\nwhile True:\n    time.sleep(0.1)\n' > /tmp/claude-1000/-home-al-projects-tdbg/9334adb8-1ac4-4ed0-ba33-1043962894d3/scratchpad/spin.py
tdb --run --examine-log - --examine-log /tmp/claude-1000/-home-al-projects-tdbg/9334adb8-1ac4-4ed0-ba33-1043962894d3/scratchpad/spin.jsonl \
    /tmp/claude-1000/-home-al-projects-tdbg/9334adb8-1ac4-4ed0-ba33-1043962894d3/scratchpad/spin.py
```

Press `Ctrl-\` twice, then `Ctrl-C`, then `t` in the quit dialog. Expect: two JSON lines on the terminal and two in the file with `seq` 1 and 2, the stderr notices, and a normal TUI episode on Ctrl-C. Confirm `Ctrl-\` inside the TUI does nothing. After exit, confirm `stty -a | grep quit` still shows `quit = ^\` and a fresh `sleep 5` in the same shell is killable with `Ctrl-\` (defaults restored).

- [ ] **Step 4: Final commit if anything changed during verification**

```bash
git status --short
git add -A src tests README.md
git commit -m "run-mode examine: verification fixes"
```
