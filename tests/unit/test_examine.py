"""Examine collector: builds the run-mode snapshot record from a stopped
controller. All DAP traffic is faked; no adapter, no TUI."""

from __future__ import annotations

import asyncio
import io
import json
import sys
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
