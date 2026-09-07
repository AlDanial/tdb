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
