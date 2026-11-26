"""Tests for tdb.trace.perfetto: captures rendered as Chrome/Perfetto JSON."""

from __future__ import annotations

import json
import random
import time
from datetime import datetime

import pytest

from tdb.trace.capture import Capture
from tdb.trace.perfetto import build_trace, write_trace
from tests.trace_checks import assert_nested

BASE = 1_795_000_000_123_456_000  # epoch ns; the microsecond part is 123456
T7 = {"task_id": 7, "task": "Task-7"}


def proc(pid=4121, start=0, end=100_000, name="MainProcess", ended="clean"):
    return {
        "type": "process",
        "pid": pid,
        "ppid": 1,
        "name": name,
        "argv": ["p.py"],
        "python": "3.12.8",
        "start_ns": BASE + start,
        "end_ns": BASE + end,
        "ended": ended,
    }


def ev(
    type_,
    at,
    *,
    pid=4121,
    tid=1,
    thread="MainThread",
    task_id=None,
    task=None,
    **fields,
):
    e = {
        "type": type_,
        "ts": BASE + at,
        "pid": pid,
        "process": "MainProcess",
        "tid": tid,
        "thread": thread,
        "task_id": task_id,
        "task": task,
    }
    e.update(fields)
    return e


def call(at, c, fn="f", kind="function", file="/r/app.py", line=1, values=None, **lane):
    return ev(
        "call",
        at,
        call_id=c,
        function=fn,
        kind=kind,
        file=file,
        line=line,
        values=values,
        **lane,
    )


def ret(at, c, value=None, **lane):
    return ev("return", at, call_id=c, value=value, **lane)


def rai(at, c, exc, **lane):
    return ev("raise", at, call_id=c, exception=exc, **lane)


def susp(at, c, **lane):
    return ev("suspend", at, call_id=c, **lane)


def res(at, c, **lane):
    return ev("resume", at, call_id=c, **lane)


def note(at, msg, pid=4121):
    return ev("note", at, pid=pid, tid=None, thread=None, message=msg)


def crash(
    at, exc, where="/r/cart.py:41", tb="Traceback (most recent call last): ...", **lane
):
    return ev("crash", at, exception=exc, where=where, traceback=tb, **lane)


def cap(name, processes, events):
    header = {
        "type": "capture",
        "version": 1,
        "name": name,
        "tdb": "0",
        "command": ["p.py"],
        "cwd": "/r",
        "spec": "s.yaml",
        "host": "h",
        "start_ns": min((p["start_ns"] for p in processes), default=BASE),
        "end_ns": max((p["end_ns"] for p in processes), default=BASE),
        "exit_code": 0,
    }
    return Capture(header, processes, events)


def xs(trace, name=None):
    return [
        e
        for e in trace["traceEvents"]
        if e["ph"] == "X" and (name is None or e["name"] == name)
    ]


def instants(trace):
    return [e for e in trace["traceEvents"] if e["ph"] == "i"]


def meta(trace, name):
    return [e for e in trace["traceEvents"] if e["ph"] == "M" and e["name"] == name]


def lanes_by_name(trace):
    """thread_name -> (pid, tid)."""
    return {e["args"]["name"]: (e["pid"], e["tid"]) for e in meta(trace, "thread_name")}


def process_names(trace):
    return {e["pid"]: e["args"]["name"] for e in meta(trace, "process_name")}


def sort_index(trace, kind, pid, tid=None):
    (e,) = [
        e
        for e in meta(trace, kind)
        if e["pid"] == pid and (tid is None or e["tid"] == tid)
    ]
    return e["args"]["sort_index"]


def wall(ns):
    return (
        time.strftime("%H:%M:%S", time.localtime(ns // 1_000_000_000))
        + f".{ns // 1000 % 1_000_000:06d}"
    )


def test_names_and_sorting():
    db = {"tid": 2, "thread": "db_0"}
    t3 = {"task_id": 3, "task": "Task-3"}
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(50, 1, "q", **db),
                    ret(60, 1, **db),
                    call(100, 2, "handle", kind="coroutine", **T7),
                    ret(110, 2, **T7),
                    call(200, 3, "handle", kind="coroutine", **t3),
                    ret(210, 3, **t3),
                    call(300, 4, "tick"),
                    ret(310, 4),
                ],
            )
        ]
    )
    (pid,) = process_names(trace)
    assert process_names(trace) == {pid: "orders · MainProcess (4121)"}
    assert sort_index(trace, "process_sort_index", pid) == 0
    lanes = lanes_by_name(trace)
    assert set(lanes) == {
        "db_0",
        "MainThread",
        "Task-7 · MainThread",
        "Task-3 · MainThread",
    }
    order = sorted(
        lanes, key=lambda n: sort_index(trace, "thread_sort_index", *lanes[n])
    )
    # Threads by first event; each thread lane before its task lanes (even though
    # MainThread's own first event is last); task lanes by first event, not id.
    assert order == ["db_0", "MainThread", "Task-7 · MainThread", "Task-3 · MainThread"]
    assert len(set(lanes.values())) == 4


def test_call_becomes_complete_event():
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    note(0, "base"),
                    call(
                        2000,
                        1,
                        "Api.checkout",
                        file="/r/api.py",
                        line=88,
                        values={"cart": "<Cart 3>"},
                    ),
                    ret(5000, 1, value="'ok'"),
                    call(6000, 2, "g", file="/r/api.py", line=99),
                    ret(7000, 2),
                ],
            )
        ]
    )
    x = xs(trace, "Api.checkout")[0]
    assert {k: x[k] for k in ("ph", "ts", "dur", "name", "cat")} == {
        "ph": "X",
        "ts": 2.0,
        "dur": 3.0,
        "name": "Api.checkout",
        "cat": "call",
    }
    assert x["args"] == {
        "file": "/r/api.py:88",
        "call_id": 1,
        "wall": wall(BASE + 2000),
        "values": {"cart": "<Cart 3>"},
        "return": "'ok'",
    }
    assert x["args"]["wall"].endswith(".123458")
    g = xs(trace, "g")[0]
    assert "values" not in g["args"] and "return" not in g["args"]
    assert (g["pid"], g["tid"]) == (x["pid"], x["tid"])


def test_lifetime_coroutine_with_suspended_child():
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(0, 1, "Api.checkout", kind="coroutine", **T7),
                    susp(1000, 1, **T7),
                    res(4000, 1, **T7),
                    ret(6000, 1, **T7),
                ],
            )
        ]
    )
    lane = lanes_by_name(trace)["Task-7 · MainThread"]
    assert [
        (e["name"], e["cat"], e["ts"], e["dur"], (e["pid"], e["tid"]))
        for e in xs(trace)
    ] == [
        ("Api.checkout", "call", 0.0, 6.0, lane),
        ("(suspended)", "suspended", 1.0, 3.0, lane),
    ]
    assert_nested(trace)


def test_innermost_suspension_only():
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(0, 1, "outer", kind="coroutine", **T7),
                    call(1000, 2, "inner", kind="coroutine", **T7),
                    susp(2000, 2, **T7),
                    susp(2500, 1, **T7),  # innermost yields first
                    res(5000, 1, **T7),
                    res(5500, 2, **T7),  # outermost resumes first
                    ret(6000, 2, **T7),
                    ret(7000, 1, **T7),
                ],
            )
        ]
    )
    (s,) = xs(trace, "(suspended)")
    assert (s["ts"], s["dur"], s["args"]["call_id"]) == (2.0, 3.5, 2)
    assert_nested(trace)


def test_generator_drawn_per_run():
    worker = {"tid": 2, "thread": "worker"}
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(0, 1, "main"),
                    call(1000, 2, "gen", kind="generator", values={"n": "3"}),
                    susp(2000, 2),
                    call(2500, 3, "work", **worker),
                    res(3000, 2, **worker),
                    susp(4000, 2, **worker),  # run 2 in another thread
                    ret(4500, 3, **worker),
                    res(5000, 2),
                    ret(6000, 2, value="'done'"),
                    ret(7000, 1),
                ],
            )
        ]
    )
    lanes = lanes_by_name(trace)
    runs = [
        (e["ts"], e["dur"], e["args"]["run"], (e["pid"], e["tid"]))
        for e in xs(trace, "gen")
    ]
    assert sorted(runs) == [
        (1.0, 1.0, 1, lanes["MainThread"]),
        (3.0, 1.0, 2, lanes["worker"]),
        (5.0, 1.0, 3, lanes["MainThread"]),
    ]
    by_run = {e["args"]["run"]: e["args"] for e in xs(trace, "gen")}
    assert by_run[1]["values"] == {"n": "3"} and "values" not in by_run[2]
    assert by_run[3]["return"] == "'done'" and "return" not in by_run[1]
    assert all(a["call_id"] == 2 for a in by_run.values())
    assert not xs(trace, "(suspended)")
    assert_nested(trace)


def test_coroutine_outside_task_drawn_per_run():
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(0, 1, "hand_driven", kind="coroutine"),
                    susp(1000, 1),
                    res(3000, 1),
                    ret(4000, 1),
                ],
            )
        ]
    )
    assert sorted(
        (e["ts"], e["dur"], e["args"]["run"]) for e in xs(trace, "hand_driven")
    ) == [(0.0, 1.0, 1), (3.0, 1.0, 2)]
    assert not xs(trace, "(suspended)")


def test_unfinished_call_closed_at_process_end():
    trace = build_trace(
        [
            cap(
                "orders",
                [proc(end=9000, ended="abrupt")],
                [
                    note(0, "base"),
                    call(1000, 1, "serve"),
                    call(2000, 2, "step"),
                    ret(3000, 2),
                ],
            )
        ]
    )
    (serve,) = xs(trace, "serve")
    assert (serve["ts"], serve["dur"], serve["args"]["unfinished"]) == (1.0, 8.0, True)
    assert "unfinished" not in xs(trace, "step")[0]["args"]
    assert_nested(trace)


def test_records_of_one_pid_form_one_process():
    # fork, then exec of a traced Python program: two records, one OS process
    processes = [
        proc(start=0, end=3000, ended="exec"),
        proc(start=3500, end=9000, name="worker"),
    ]
    trace = build_trace(
        [
            cap(
                "orders",
                processes,
                [
                    call(1000, 1, "spawn"),  # open at exec: never ends
                    call(4000, 2, "run"),
                    ret(5000, 2),
                ],
            )
        ]
    )
    assert list(process_names(trace).values()) == ["orders · worker (4121)"]
    (spawn,) = xs(trace, "spawn")
    assert (spawn["ts"], spawn["dur"], spawn["args"]["unfinished"]) == (0.0, 8.0, True)
    assert_nested(trace)


def test_raise_crash_and_note_instants():
    trace = build_trace(
        [
            cap(
                "orders",
                [proc(), proc(pid=4122)],
                [
                    call(0, 1, "pay"),
                    rai(1000, 1, "ValueError: bad"),
                    crash(2000, "KeyError: 'sku'"),
                    note(3000, "event cap reached; 12 events dropped"),
                    call(500, 1, "loop", pid=4122, tid=7, thread="MainThread"),
                    crash(
                        4000, "KeyboardInterrupt", pid=4122, tid=7, thread="MainThread"
                    ),
                ],
            )
        ]
    )
    names = {v: k for k, v in process_names(trace).items()}
    p1, p2 = names["orders · MainProcess (4121)"], names["orders · MainProcess (4122)"]
    (pay,) = xs(trace, "pay")
    assert pay["args"]["exception"] == "ValueError: bad" and pay["dur"] == 1.0
    got = {(e["name"], e["s"], e["pid"]): e for e in instants(trace)}
    assert set(got) == {
        ("raise ValueError", "t", p1),
        ("CRASH: KeyError", "p", p1),
        ("note", "p", p1),
        ("INTERRUPTED", "p", p2),
    }
    assert got[("raise ValueError", "t", p1)]["tid"] == pay["tid"]
    assert got[("raise ValueError", "t", p1)]["ts"] == 1.0
    assert got[("CRASH: KeyError", "p", p1)]["args"] == {
        "exception": "KeyError: 'sku'",
        "where": "/r/cart.py:41",
        "traceback": "Traceback (most recent call last): ...",
    }
    assert got[("note", "p", p1)]["args"] == {
        "message": "event cap reached; 12 events dropped"
    }
    # every event names a tid that belongs to its own pid (never pid-defaulted)
    owner = {}
    for e in trace["traceEvents"]:
        assert isinstance(e["tid"], int)
        assert owner.setdefault(e["tid"], e["pid"]) == e["pid"]


def test_merge_two_services_pid_remap_and_names():
    orders = cap(
        "orders",
        [proc(start=100), proc(pid=4000, start=200)],
        [
            call(5000, 1, "checkout"),
            ret(6000, 1),
            call(5500, 1, "audit", pid=4000, tid=9),
            ret(5600, 1, pid=4000, tid=9),
        ],
    )
    payments = cap(
        "payments",
        [proc(start=0)],
        [
            call(0, 1, "charge"),
            ret(7000, 1),
        ],
    )
    trace = build_trace([orders, payments])
    assert process_names(trace) == {
        1: "orders · MainProcess (4000)",
        2: "orders · MainProcess (4121)",
        3: "payments · MainProcess (4121)",
    }
    assert [sort_index(trace, "process_sort_index", p) for p in (2, 1, 3)] == [
        0,
        1,
        1000,
    ]
    assert trace["metadata"]["services"] == ["orders", "payments"]
    assert trace["metadata"]["tdb_base_epoch_ns"] == BASE
    (checkout,) = xs(trace, "checkout")
    assert (checkout["pid"], checkout["ts"]) == (2, 5.0)
    (charge,) = xs(trace, "charge")
    assert charge["pid"] == 3 and charge["tid"] != checkout["tid"]
    assert_nested(trace)


def test_duplicate_service_names_get_suffix():
    caps = [
        cap("orders", [proc()], [call(i, 1, "f"), ret(i + 10, 1)]) for i in range(3)
    ]
    trace = build_trace(caps)
    assert trace["metadata"]["services"] == ["orders", "orders#2", "orders#3"]
    assert sorted(process_names(trace).values()) == [
        "orders · MainProcess (4121)",
        "orders#2 · MainProcess (4121)",
        "orders#3 · MainProcess (4121)",
    ]


def test_relative_ts_precision():
    trace = build_trace(
        [
            cap(
                "orders",
                [proc(end=3_700_000_000_000)],
                [
                    note(0, "base"),
                    call(1, 1, "f"),
                    ret(1 + 3_600_000_000_007, 1),
                ],
            )
        ]
    )
    (f,) = xs(trace, "f")
    assert (f["ts"], f["dur"]) == (0.001, 3600000000.007)
    md = trace["metadata"]
    assert md["tdb_base_epoch_ns"] == BASE and trace["displayTimeUnit"] == "ns"
    start = datetime.fromisoformat(md["start"])
    assert start.tzinfo is not None
    assert round(start.timestamp() * 1_000_000) == BASE // 1000


def mixed_capture():
    """Coroutine lifetimes, generators, sync calls and unfinished calls on shared threads."""
    w = {"tid": 2, "thread": "worker"}
    t1 = {"task_id": 1, "task": "Task-1"}
    t2 = {"task_id": 2, "task": "Task-2"}
    t3 = {"task_id": 3, "task": "Task-3"}
    t6 = {"task_id": 6, "task": "Task-6"}
    t7 = {"task_id": 7, "task": "Task-7"}
    t8 = {"task_id": 8, "task": "Task-8"}
    t9 = {"task_id": 9, "task": "Task-9"}
    events = [
        call(0, 1, "main"),  # unfinished
        call(1000, 2, "gen", kind="generator"),
        susp(2000, 2),
        # cancellation: the outer resume (from throw) comes after the inner raise
        call(3000, 3, "outer", kind="coroutine", **t1),
        call(3500, 4, "inner", kind="coroutine", **t1),
        susp(4000, 4, **t1),
        susp(4100, 3, **t1),
        res(5000, 4, **t1),
        rai(5100, 4, "CancelledError", **t1),
        res(5200, 3, **t1),
        rai(5300, 3, "CancelledError", **t1),
        # two sibling coroutines on one task lane
        call(6000, 5, "parent", kind="coroutine", **t2),
        call(6100, 6, "a", kind="coroutine", **t2),
        susp(6200, 6, **t2),
        susp(6300, 5, **t2),
        res(7000, 5, **t2),
        res(7100, 6, **t2),
        ret(7200, 6, **t2),
        call(7300, 7, "b", kind="coroutine", **t2),
        susp(7400, 7, **t2),
        susp(7500, 5, **t2),
        res(8000, 5, **t2),
        res(8100, 7, **t2),
        ret(8200, 7, **t2),
        ret(8300, 5, **t2),
        # the generator resumed in another thread, then back on the main thread
        call(9000, 8, "work", **w),
        res(9100, 2, **w),
        susp(9200, 2, **w),
        ret(9300, 8, **w),
        res(9500, 2),
        ret(9600, 2),
        # a coroutine resumed in another task stays on its lane; Task-6 moves on
        call(10000, 9, "x", kind="coroutine", **t6),
        susp(10100, 9, **t6),
        call(10200, 10, "y", kind="coroutine", **t6),
        res(10500, 9, **t7),
        ret(10600, 9, **t7),
        ret(10700, 10, **t6),
        # an async generator finished by asyncio's aclose() task in another lane
        call(11000, 11, "consume", kind="coroutine", **t3),
        call(11100, 12, "rows", kind="async_generator", **t3),
        susp(11200, 12, **t3),
        ret(11300, 11, **t3),
        res(12000, 12, **t9),
        rai(12100, 12, "GeneratorExit", **t9),
        # unfinished coroutine, suspended at the end
        call(20000, 13, "z", kind="coroutine", **t8),
        susp(20100, 13, **t8),
        # an unfinished sync call on the worker thread
        call(30000, 14, "spin", **w),
    ]
    return cap("orders", [proc(end=50_000, ended="abrupt")], events)


def test_nesting_valid_for_mixed_capture():
    trace = build_trace([mixed_capture()])
    assert_nested(trace)
    lanes = lanes_by_name(trace)
    assert "Task-7 · MainThread" not in lanes  # x is drawn on its starting lane
    by_name = {}
    for e in xs(trace):
        by_name.setdefault(e["name"], []).append(e)
    assert (
        by_name["main"][0]["args"]["unfinished"] is True
        and by_name["main"][0]["dur"] == 50.0
    )
    assert by_name["spin"][0]["args"]["unfinished"] is True
    assert (by_name["z"][0]["dur"], by_name["z"][0]["args"]["unfinished"]) == (
        30.0,
        True,
    )
    assert sorted(e["args"]["run"] for e in by_name["gen"]) == [1, 2, 3]
    assert {(e["pid"], e["tid"]) for e in by_name["rows"]} == {
        lanes["Task-3 · MainThread"],
        lanes["Task-9 · MainThread"],
    }
    # cancellation: the outer suspension is trimmed to start where inner ends
    susp_t1 = sorted(
        (e["ts"], e["dur"], e["args"]["call_id"], e["args"].get("clipped", False))
        for e in by_name["(suspended)"]
        if (e["pid"], e["tid"]) == lanes["Task-1 · MainThread"]
    )
    assert susp_t1 == [(4.0, 1.0, 4, False), (5.1, 0.1, 3, True)]
    # siblings: only the children's (innermost) suspensions remain
    susp_t2 = sorted(
        (e["ts"], e["args"]["call_id"])
        for e in by_name["(suspended)"]
        if (e["pid"], e["tid"]) == lanes["Task-2 · MainThread"]
    )
    assert susp_t2 == [(6.2, 6), (7.4, 7)]
    # y overlapped x's lifetime past its end: cut there, and marked
    (y,) = by_name["y"]
    assert (y["ts"], y["dur"], y["args"]["clipped"]) == (10.2, 0.4, True)
    assert "clipped" not in by_name["x"][0]["args"]


def test_async_generator_run_spans_awaited_coroutine():
    # `async for` over an async generator that awaits a traced coroutine: the
    # generator's suspension while `fetch` waits is an await, not a yield, so
    # its run goes on (else fetch's slice would straddle two runs).
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(0, 1, "consume", kind="coroutine", **T7),
                    call(100, 2, "stream", kind="async_generator", **T7),
                    call(200, 3, "fetch", kind="coroutine", **T7),
                    susp(300, 3, **T7),
                    susp(310, 2, **T7),
                    susp(320, 1, **T7),
                    res(1000, 1, **T7),
                    res(1010, 2, **T7),
                    res(1020, 3, **T7),
                    ret(1100, 3, value="'row'", **T7),
                    susp(1200, 2, **T7),  # yields the row
                    res(1300, 2, **T7),
                    ret(1400, 2, **T7),  # StopAsyncIteration
                    ret(1500, 1, **T7),
                ],
            )
        ]
    )
    assert_nested(trace)
    assert sorted(
        (e["ts"], e["dur"], e["args"]["run"]) for e in xs(trace, "stream")
    ) == [(0.1, 1.1, 1), (1.3, 0.1, 2)]
    (fetch,) = xs(trace, "fetch")
    assert (fetch["ts"], fetch["dur"], "clipped" in fetch["args"]) == (0.2, 0.9, False)
    (s,) = xs(trace, "(suspended)")
    assert (s["ts"], s["dur"], s["args"]["call_id"]) == (0.3, 0.72, 3)


def test_async_generator_yield_ends_run_despite_tied_start():
    # The consumer started in the same clock tick as the generator's run; it
    # is not awaited by the generator, so the generator's yield ends the run.
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(0, 1, "consume", kind="coroutine", **T7),
                    call(0, 2, "stream", kind="async_generator", **T7),
                    susp(100, 2, **T7),
                    res(200, 2, **T7),
                    ret(300, 2, **T7),
                    ret(400, 1, **T7),
                ],
            )
        ]
    )
    assert sorted((e["ts"], e["dur"]) for e in xs(trace, "stream")) == [
        (0.0, 0.1),
        (0.2, 0.1),
    ]


def test_orphan_events_draw_nothing_but_raises():
    # e.g. a fork child ending calls its parent entered: no lane, no slice,
    # except the raise marker where it happened
    worker = {"tid": 2, "thread": "worker"}
    trace = build_trace(
        [
            cap(
                "orders",
                [proc()],
                [
                    call(0, 1, "main"),
                    ret(100, 50, **worker),
                    susp(110, 51, **worker),
                    res(120, 51, **worker),
                    rai(130, 52, "OSError: gone", tid=3, thread="other"),
                    ret(200, 1),
                ],
            )
        ]
    )
    assert [e["name"] for e in xs(trace)] == ["main"]
    assert set(lanes_by_name(trace)) == {"MainThread", "other"}
    (marker,) = instants(trace)
    assert (marker["name"], marker["tid"]) == (
        "raise OSError",
        lanes_by_name(trace)["other"][1],
    )


def test_process_sort_index_keeps_services_grouped_past_1000_processes():
    big = cap("big", [proc(pid=p, start=p) for p in range(1, 1002)], [])
    small = cap("small", [proc(pid=1)], [])
    trace = build_trace([big, small])
    names = process_names(trace)
    big_idx = [
        sort_index(trace, "process_sort_index", p)
        for p in names
        if names[p].startswith("big ")
    ]
    (small_idx,) = [
        sort_index(trace, "process_sort_index", p)
        for p in names
        if names[p].startswith("small ")
    ]
    assert len(big_idx) == 1001 and max(big_idx) < small_idx
    assert sort_index(trace, "process_sort_index", 1) == 0  # (big, pid 1) starts first


def test_assert_nested_rejects_partial_overlap():
    bad = {
        "traceEvents": [
            {"ph": "X", "pid": 1, "tid": 1, "ts": 0.0, "dur": 2.0, "name": "a"},
            {"ph": "X", "pid": 1, "tid": 1, "ts": 1.0, "dur": 2.0, "name": "b"},
        ]
    }
    with pytest.raises(AssertionError, match="'b'.*partially overlaps 'a'"):
        assert_nested(bad)
    shared_end = {
        "traceEvents": [  # 0.1 + 0.2 != 0.3 in floats; nested in ns
            {"ph": "X", "pid": 1, "tid": 1, "ts": 0.0, "dur": 0.3, "name": "a"},
            {"ph": "X", "pid": 1, "tid": 1, "ts": 0.1, "dur": 0.2, "name": "b"},
        ]
    }
    assert_nested(shared_end)


def random_capture(rng):
    """Calls of every kind whose events land on random lanes, in random order."""
    lanes = [
        {"tid": t, "thread": f"t{t}", "task_id": k, "task": k and f"Task-{k}"}
        for t in (1, 2)
        for k in (None, 1, 2, 3)
    ]
    kinds = ["function", "coroutine", "generator", "async_generator", None]
    pending = []
    for c in range(1, rng.randint(2, 14)):
        steps = [
            (
                "call",
                {
                    "function": f"f{c}",
                    "kind": rng.choice(kinds),
                    "file": "/a.py",
                    "line": c,
                    "values": None,
                },
            )
        ]
        steps += [
            (rng.choice(["suspend", "resume"]), {}) for _ in range(rng.randint(0, 4))
        ]
        if rng.random() < 0.8:  # else unfinished
            steps.append(
                (rng.choice(["return", "raise"]), {"value": None, "exception": "E: x"})
            )
        pending.append((c, steps))
    events, t = [], 0
    while pending:
        i = rng.randrange(len(pending))
        c, steps = pending[i]
        type_, extra = steps.pop(0)
        if not steps:
            pending.pop(i)
        t += rng.choice([0, 0, 1, 5, 100])  # ties included
        events.append(ev(type_, t, call_id=c, **rng.choice(lanes), **extra))
    return cap("svc", [proc(end=t + rng.choice([-50, 0, 1000]))], events)


def test_nesting_valid_for_random_event_orders():
    for seed in range(300):
        rng = random.Random(seed)
        assert_nested(build_trace([random_capture(rng), random_capture(rng)]))


def test_write_trace_matches_build_trace(tmp_path):
    captures = [mixed_capture()]
    out = tmp_path / "t.perfetto.json"
    write_trace(captures, out)
    assert json.loads(out.read_text(encoding="utf-8")) == build_trace(captures)
