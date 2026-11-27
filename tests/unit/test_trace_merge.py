"""Tests for tdb.trace.merge: several services woven into one Perfetto trace."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from tdb.cli import parse_args
from tdb.trace.capture import assemble, write_capture
from tdb.trace.merge import merge_main

T0 = 1_795_000_000_000_000_000
SEC = 1_000_000_000


def make_parts(d, name, wall, pid=10):
    d.mkdir()
    recs = [
        {
            "k": "hdr",
            "pid": pid,
            "ppid": 1,
            "proc": "MainProcess",
            "argv": ["p.py"],
            "py": "3.12.8",
            "wall": wall,
            "pc": 0,
            "mode": "trace",
        },
        {
            "k": "fn",
            "id": 1,
            "file": "/r/a.py",
            "qual": "handle",
            "line": 3,
            "kind": "function",
        },
        {"k": "th", "tid": 7, "name": "MainThread"},
        {"k": "call", "t": 100, "c": 1, "fn": 1, "tid": 7, "task": None},
        {"k": "ret", "t": 2 * SEC, "c": 1, "tid": 7, "task": None, "v": "1"},
        {"k": "exit", "t": 2 * SEC + 1, "how": "atexit"},
    ]
    (d / f"{pid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (d / "run.json").write_text(json.dumps({"name": name}))
    return d


def make_capture(tmp_path, name, wall=T0, pid=10):
    parts = make_parts(tmp_path / f"{name}.parts", name, wall, pid)
    cap, _ = assemble(parts, header={"name": name, "exit_code": 0})
    out = tmp_path / f"{name}.jsonl"
    write_capture(cap, out)
    return out


def process_names(path):
    data = json.loads(path.read_text())
    return [
        e["args"]["name"]
        for e in data["traceEvents"]
        if e.get("ph") == "M" and e["name"] == "process_name"
    ]


def test_merge_writes_both_services(tmp_path, capsys):
    a = make_capture(tmp_path, "orders")
    b = make_capture(tmp_path, "payments", wall=T0 + SEC // 2)
    out = tmp_path / "m"
    assert merge_main([str(a), str(b)], str(out)) == 0
    names = process_names(tmp_path / "m.perfetto.json")
    assert any(n.startswith("orders · ") for n in names)
    assert any(n.startswith("payments · ") for n in names)
    err = capsys.readouterr().err
    assert f"tdb: wrote {out}.perfetto.json  (open in https://ui.perfetto.dev)" in err
    assert "warning" not in err
    assert not list(tmp_path.glob(".*.tmp"))


def test_default_output_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    a = make_capture(tmp_path, "orders")
    now = datetime(2026, 11, 25, 10, 42, 12)
    assert merge_main([str(a)], None, now=now) == 0
    assert (tmp_path / "merged-20261125-104212.perfetto.json").exists()


def test_parts_dir_input(tmp_path):
    a = make_capture(tmp_path, "orders")
    parts = make_parts(tmp_path / "left.parts", "payments", T0)
    assert merge_main([str(a), str(parts)], str(tmp_path / "m")) == 0
    names = process_names(tmp_path / "m.perfetto.json")
    assert any(n.startswith("payments · ") for n in names)


def test_non_overlapping_warning(tmp_path, capsys):
    a = make_capture(tmp_path, "orders")
    b = make_capture(tmp_path, "payments", wall=T0 + 3600 * SEC)
    assert merge_main([str(a), str(b)], str(tmp_path / "m")) == 0
    err = capsys.readouterr().err
    assert "tdb: warning: payments does not overlap any other capture in time" in err
    assert "orders does not overlap" in err


def test_single_input_has_no_overlap_warning(tmp_path, capsys):
    a = make_capture(tmp_path, "orders")
    assert merge_main([str(a)], str(tmp_path / "m")) == 0
    assert "warning" not in capsys.readouterr().err


def test_missing_input_returns_2(tmp_path, capsys):
    out = tmp_path / "m"
    missing = tmp_path / "nope.jsonl"
    assert merge_main([str(missing)], str(out)) == 2
    assert f"tdb: error: cannot read {missing}: " in capsys.readouterr().err
    assert not (tmp_path / "m.perfetto.json").exists()


def test_truncated_input_returns_2(tmp_path, capsys):
    a = make_capture(tmp_path, "orders")
    text = a.read_text()
    a.write_text(text[: len(text) - 30])
    assert merge_main([str(a)], str(tmp_path / "m")) == 2
    assert f"tdb: error: cannot read {a}: " in capsys.readouterr().err
    assert not (tmp_path / "m.perfetto.json").exists()


def test_unwritable_output_returns_2_without_partial_file(tmp_path, capsys):
    a = make_capture(tmp_path, "orders")
    out = tmp_path / "no-such-dir" / "m"
    assert merge_main([str(a)], str(out)) == 2
    assert "tdb: error: cannot write " in capsys.readouterr().err


def test_same_service_name_twice_is_suffixed(tmp_path):
    a = make_capture(tmp_path, "orders")
    b = tmp_path / "again.jsonl"
    b.write_text(a.read_text())
    assert merge_main([str(a), str(b)], str(tmp_path / "m")) == 0
    names = process_names(tmp_path / "m.perfetto.json")
    assert any(n.startswith("orders#2 · ") for n in names)


def test_cli_rejects_program():
    with pytest.raises(SystemExit):
        parse_args(["prog.py", "--trace-merge", "a.jsonl"])


def test_cli_rejects_program_message(capsys):
    with pytest.raises(SystemExit):
        parse_args(["prog.py", "--trace-merge", "a.jsonl"])
    assert "--trace-merge takes no program" in capsys.readouterr().err


def test_cli_accepts_inputs():
    a = parse_args(["--trace-merge", "a.jsonl", "b.jsonl", "--trace-out", "m"])
    assert a.trace_merge == ["a.jsonl", "b.jsonl"] and a.program is None


# --- final review F7, M11 ------------------------------------------------------


def test_parts_dir_input_repeats_its_warnings(tmp_path, capsys):
    """F7: a leftover parts directory is assembled as --trace would; what
    the trace may be missing is said, naming the input."""
    a = make_capture(tmp_path, "orders")
    parts = make_parts(tmp_path / "left.parts", "payments", T0)
    hdr = {"k": "hdr", "pid": 11, "ppid": 10, "proc": "MainProcess", "argv": ["c"]}
    hdr.update(py="3.12.8", wall=T0, pc=0, mode="trace")
    (parts / "11.jsonl").write_text(json.dumps(hdr) + "\n")  # no exit record
    marker = {"marker": "skipped", "pid": 12, "argv": ["/opt/py311"]}
    marker.update(python="3.11.9", reason="python 3.11 is older than 3.12")
    (parts / "skipped-12.json").write_text(json.dumps(marker))
    assert merge_main([str(a), str(parts)], str(tmp_path / "m")) == 0
    err = capsys.readouterr().err.splitlines()
    assert err[:2] == [
        f"tdb: warning: {parts}: skipped /opt/py311 (pid 12): "
        "python 3.11 is older than 3.12",
        f"tdb: warning: {parts}: pid 11 ended abruptly; its last events may be missing",
    ]
    assert err[2].startswith("tdb: wrote ") and len(err) == 3


def test_event_without_ts_is_reported_as_unreadable(tmp_path, capsys):
    """M11: a hand-edited capture whose event lost its ts."""
    a = make_capture(tmp_path, "orders")
    lines = a.read_text().splitlines()
    event = json.loads(lines[-1])
    del event["ts"]
    a.write_text("\n".join([*lines[:-1], json.dumps(event)]) + "\n")
    b = make_capture(tmp_path, "payments")
    assert merge_main([str(b), str(a)], str(tmp_path / "m")) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"tdb: error: cannot read {a}: ") and "Traceback" not in err
    assert len(err.splitlines()) == 1
    assert not (tmp_path / "m.perfetto.json").exists()
    assert not list(tmp_path.glob(".*.tmp"))
