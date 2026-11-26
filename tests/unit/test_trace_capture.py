"""Tests for tdb.trace.capture: assembling part files into a capture."""

from __future__ import annotations

import json
from pathlib import Path

from tdb.trace.capture import (
    PART_FILE,
    Capture,
    assemble,
    load_capture,
    load_input,
    write_capture,
)


def H(pid, wall, pc, ppid=1, **kw):
    rec = {
        "k": "hdr",
        "pid": pid,
        "ppid": ppid,
        "proc": "MainProcess",
        "argv": ["p.py"],
        "py": "3.12.8",
        "wall": wall,
        "pc": pc,
        "mode": "trace",
    }
    rec.update(kw)
    return rec


FN = {
    "k": "fn",
    "id": 1,
    "file": "/r/api.py",
    "qual": "Api.checkout",
    "line": 88,
    "kind": "coroutine",
}


def part(d: Path, pid, records, suffix=""):
    name = f"{pid}{suffix}.jsonl"
    (d / name).write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    )
    return d / name


HDR = {"name": "o", "exit_code": 0}


def test_part_file_pattern():
    assert PART_FILE.match("10.jsonl") and PART_FILE.match("10-2.jsonl")
    assert not PART_FILE.match("run.json") and not PART_FILE.match("skipped-10.json")


def test_assemble_converts_to_epoch_and_sorts(tmp_path):
    part(
        tmp_path,
        10,
        [
            H(10, 1_000_000, 500),
            FN,
            {"k": "th", "tid": 7, "name": "MainThread"},
            {"k": "call", "t": 600, "c": 1, "fn": 1, "tid": 7, "task": None},
            {"k": "exit", "t": 900, "how": "atexit"},
        ],
    )
    part(
        tmp_path,
        11,
        [
            H(11, 2_000_000, 1_000_050, ppid=10),
            FN,
            {"k": "th", "tid": 8, "name": "MainThread"},
            {"k": "call", "t": 1_000_000, "c": 1, "fn": 1, "tid": 8, "task": None},
        ],
    )
    cap, summ = assemble(tmp_path, header={"name": "orders", "exit_code": 0})
    assert [(e["pid"], e["ts"]) for e in cap.events] == [
        (10, 1_000_100),
        (11, 1_999_950),
    ]
    assert {p["pid"]: p["ended"] for p in cap.processes} == {10: "clean", 11: "abrupt"}
    assert summ.abrupt == [11]
    assert cap.header["type"] == "capture" and cap.header["version"] == 1
    assert cap.header["name"] == "orders" and cap.header["exit_code"] == 0
    assert cap.header["start_ns"] == 1_000_000
    p10 = cap.processes[0]
    assert p10 == {
        "type": "process",
        "pid": 10,
        "ppid": 1,
        "name": "MainProcess",
        "argv": ["p.py"],
        "python": "3.12.8",
        "start_ns": 1_000_000,
        "end_ns": 1_000_400,
        "ended": "clean",
    }


def test_call_record_is_self_contained(tmp_path):
    part(
        tmp_path,
        10,
        [
            H(10, 0, 0),
            FN,
            {"k": "th", "tid": 7, "name": "MainThread"},
            {"k": "task", "id": 3, "name": "Task-3", "tid": 7},
            {
                "k": "call",
                "t": 5,
                "c": 1,
                "fn": 1,
                "tid": 7,
                "task": 3,
                "v": {"x": "1"},
            },
            {"k": "susp", "t": 6, "c": 1, "tid": 7, "task": 3},
        ],
    )
    cap, _ = assemble(tmp_path, header=HDR)
    assert cap.events[0] == {
        "type": "call",
        "ts": 5,
        "pid": 10,
        "process": "MainProcess",
        "tid": 7,
        "thread": "MainThread",
        "task_id": 3,
        "task": "Task-3",
        "file": "/r/api.py",
        "function": "Api.checkout",
        "line": 88,
        "kind": "coroutine",
        "call_id": 1,
        "values": {"x": "1"},
    }
    assert cap.events[1] == {
        "type": "suspend",
        "ts": 6,
        "pid": 10,
        "process": "MainProcess",
        "tid": 7,
        "thread": "MainThread",
        "task_id": 3,
        "task": "Task-3",
        "call_id": 1,
    }


def test_main_signal_and_process_name(tmp_path):
    part(
        tmp_path, 10, [H(10, 0, 0), {"k": "proc", "t": 1, "name": "SpawnPoolWorker-1"}]
    )
    cap, _ = assemble(
        tmp_path,
        header={"name": "o", "exit_code": 137},
        main_pid=10,
        main_signal="SIGKILL",
    )
    assert (
        cap.processes[0]["name"] == "SpawnPoolWorker-1"
        and cap.processes[0]["ended"] == "signal:SIGKILL"
    )


def test_truncated_last_line_is_ignored(tmp_path):
    part(
        tmp_path,
        10,
        [
            H(10, 0, 0),
            FN,
            {"k": "th", "tid": 7, "name": "MainThread"},
            {"k": "call", "t": 5, "c": 1, "fn": 1, "tid": 7, "task": None},
        ],
    )
    with open(tmp_path / "10.jsonl", "a") as f:
        f.write('{"k":"ret","t":9,"c"')
    cap, summ = assemble(tmp_path, header=HDR)
    assert len(cap.events) == 1 and "1 unreadable line(s) in 10.jsonl" in summ.notes


def test_summary_counts_markers_and_called(tmp_path):
    part(
        tmp_path,
        10,
        [
            H(10, 0, 0),
            FN,
            {"k": "th", "tid": 7, "name": "MainThread"},
            {"k": "th", "tid": 8, "name": "w"},
            {"k": "task", "id": 1, "name": "Task-1", "tid": 7},
            {"k": "call", "t": 1, "c": 1, "fn": 1, "tid": 7, "task": 1},
            {"k": "call", "t": 2, "c": 2, "fn": 1, "tid": 8, "task": None},
            {
                "k": "note",
                "t": 3,
                "msg": "event cap of 2 reached; later calls not recorded",
            },
            {"k": "exit", "t": 4, "how": "_exit"},
        ],
    )
    skipped = {
        "marker": "skipped",
        "pid": 12,
        "argv": ["x"],
        "python": "/py",
        "reason": "old",
    }
    err = {"marker": "agent-error", "pid": 13, "argv": ["y"], "error": "boom"}
    (tmp_path / "skipped-12.json").write_text(json.dumps(skipped))
    (tmp_path / "agent-error-13.json").write_text(json.dumps(err))
    (tmp_path / "run.json").write_text("{}")
    (tmp_path / "config.json").write_text("{}")
    cap, summ = assemble(tmp_path, header=HDR)
    assert (summ.processes, summ.threads, summ.tasks, summ.calls) == (1, 2, 1, 2)
    assert summ.skipped == [skipped] and summ.agent_errors == [err]
    assert summ.abrupt == []
    assert summ.called == {("/r/api.py", "Api.checkout")}
    assert summ.notes == ["pid 10: event cap of 2 reached; later calls not recorded"]
    assert [e["type"] for e in cap.events] == ["call", "call", "note"]
    assert cap.events[2]["message"].startswith("event cap")


def test_write_load_roundtrip_and_parts_dir_input(tmp_path):
    parts = tmp_path / "x.parts"
    parts.mkdir()
    part(
        parts,
        10,
        [
            H(10, 100, 0),
            FN,
            {"k": "th", "tid": 7, "name": "MainThread"},
            {"k": "call", "t": 5, "c": 1, "fn": 1, "tid": 7, "task": None},
            {"k": "ret", "t": 6, "c": 1, "tid": 7, "task": None, "v": "'é'"},
            {
                "k": "crash",
                "t": 7,
                "tid": 7,
                "exc": "E: x",
                "where": "/r/a.py:1",
                "tb": "tb",
            },
            {"k": "exit", "t": 8, "how": "atexit"},
        ],
    )
    (parts / "run.json").write_text(
        json.dumps(
            {
                "name": "orders",
                "tdb": "1",
                "command": ["p.py"],
                "cwd": "/c",
                "spec": ["a"],
                "host": "h",
            }
        )
    )
    cap, _ = assemble(parts, header={"name": "orders", "exit_code": 0})
    out = tmp_path / "c.jsonl"
    write_capture(cap, out)
    assert json.loads(out.read_text().splitlines()[0])["type"] == "capture"
    assert load_capture(out) == cap
    assert isinstance(load_capture(out), Capture)
    cap2 = load_input(parts)
    assert cap2.header["name"] == "orders" and cap2.header["exit_code"] is None
    assert cap2.events == cap.events
    assert load_input(out) == cap
    ret = [e for e in cap.events if e["type"] == "return"][0]
    assert ret["value"] == "'é'"
    crash = [e for e in cap.events if e["type"] == "crash"][0]
    assert (crash["exception"], crash["where"], crash["traceback"]) == (
        "E: x",
        "/r/a.py:1",
        "tb",
    )


def test_events_take_lane_from_own_record(tmp_path):
    part(
        tmp_path,
        10,
        [
            H(10, 0, 0),
            FN,
            {"k": "th", "tid": 7, "name": "MainThread"},
            {"k": "task", "id": 1, "name": "A", "tid": 7},
            {"k": "call", "t": 1, "c": 1, "fn": 1, "tid": 7, "task": 1},
            {"k": "th", "tid": 9, "name": "worker"},
            {"k": "task", "id": 2, "name": "B", "tid": 9},
            {"k": "res", "t": 2, "c": 1, "tid": 9, "task": 2},
            {"k": "task", "id": 2, "name": "B2", "tid": 9},
            {
                "k": "raise",
                "t": 3,
                "c": 1,
                "tid": 9,
                "task": 2,
                "exc": "ValueError: bad",
            },
        ],
    )
    cap, _ = assemble(tmp_path, header=HDR)
    res, rai = cap.events[1], cap.events[2]
    assert (res["type"], res["tid"], res["thread"], res["task_id"], res["task"]) == (
        "resume",
        9,
        "worker",
        2,
        "B",
    )
    assert (rai["type"], rai["task"], rai["exception"]) == (
        "raise",
        "B2",
        "ValueError: bad",
    )


def test_call_before_its_fn_record(tmp_path):
    part(
        tmp_path,
        10,
        [
            H(10, 0, 0),
            {"k": "th", "tid": 7, "name": "MainThread"},
            {"k": "call", "t": 1, "c": 1, "fn": 1, "tid": 7, "task": None},
            FN,
        ],
    )
    cap, summ = assemble(tmp_path, header=HDR)
    e = cap.events[0]
    assert (e["file"], e["function"], e["line"], e["kind"]) == (
        "/r/api.py",
        "Api.checkout",
        88,
        "coroutine",
    )
    assert summ.called == {("/r/api.py", "Api.checkout")}


def test_fork_child_ids_resolved_per_part(tmp_path):
    other = dict(FN, qual="Other.run", file="/r/o.py")
    part(
        tmp_path,
        10,
        [
            H(10, 0, 0),
            FN,
            {"k": "call", "t": 1, "c": 1, "fn": 1, "tid": 7, "task": None},
        ],
    )
    part(
        tmp_path,
        11,
        [
            H(11, 0, 0, ppid=10, argv=["p.py"]),
            other,
            {"k": "call", "t": 1, "c": 1, "fn": 1, "tid": 8, "task": None},
            {"k": "proc", "t": 2, "name": "ForkPoolWorker-1"},
        ],
    )
    cap, _ = assemble(tmp_path, header=HDR)
    by_pid = {e["pid"]: e for e in cap.events}
    assert (
        by_pid[10]["function"] == "Api.checkout"
        and by_pid[11]["function"] == "Other.run"
    )
    assert by_pid[11]["process"] == "ForkPoolWorker-1"
    assert {p["pid"]: p["name"] for p in cap.processes}[11] == "ForkPoolWorker-1"


def test_multiple_parts_per_pid_renumber_and_exec(tmp_path):  # Rulings 17 and 18
    part(
        tmp_path,
        10,
        [
            H(10, 0, 0),
            FN,
            {"k": "th", "tid": 7, "name": "m"},
            {"k": "task", "id": 1, "name": "T1", "tid": 7},
            {"k": "call", "t": 1, "c": 1, "fn": 1, "tid": 7, "task": 1},
            {"k": "call", "t": 2, "c": 2, "fn": 1, "tid": 7, "task": None},
        ],
    )
    part(
        tmp_path,
        10,
        [
            H(10, 1000, 0),
            FN,
            {"k": "th", "tid": 7, "name": "m"},
            {"k": "task", "id": 1, "name": "T1b", "tid": 7},
            {"k": "call", "t": 5, "c": 1, "fn": 1, "tid": 7, "task": 1},
            {"k": "ret", "t": 6, "c": 1, "tid": 7, "task": 1, "v": "1"},
        ],
        suffix="-2",
    )
    cap, summ = assemble(tmp_path, header=HDR, main_pid=10, main_signal="SIGTERM")
    assert [(p["pid"], p["ended"]) for p in cap.processes] == [
        (10, "exec"),
        (10, "signal:SIGTERM"),
    ]
    assert summ.abrupt == [] and summ.processes == 2
    calls = [e for e in cap.events if e["type"] == "call"]
    assert [c["call_id"] for c in calls] == [1, 2, 3]
    assert [c["task_id"] for c in calls] == [1, None, 2]
    assert calls[2]["task"] == "T1b"
    assert [e["call_id"] for e in cap.events if e["type"] == "return"] == [3]
    assert summ.tasks == 2 and summ.threads == 1


def test_exec_part_without_main_is_not_abrupt_but_last_part_is(tmp_path):
    part(tmp_path, 10, [H(10, 0, 0)])
    part(tmp_path, 10, [H(10, 10, 0)], suffix="-2")
    cap, summ = assemble(tmp_path, header=HDR)
    assert [p["ended"] for p in cap.processes] == [
        "exec",
        "abrupt",
    ] and summ.abrupt == [10]


def test_empty_and_headerless_parts_skipped(tmp_path):  # Ruling 19
    (tmp_path / "10.jsonl").write_text("")
    part(
        tmp_path,
        11,
        [FN, {"k": "call", "t": 1, "c": 1, "fn": 1, "tid": 1, "task": None}],
    )
    part(tmp_path, 12, [H(12, 0, 0), {"k": "exit", "t": 1, "how": "atexit"}])
    cap, summ = assemble(tmp_path, header=HDR)
    assert [p["pid"] for p in cap.processes] == [12] and cap.events == []
    assert (
        "10.jsonl: no header; skipped" in summ.notes
        and "11.jsonl: no header; skipped" in summ.notes
    )
    assert summ.processes == 1
