"""Pieces of ``tdb --trace``'s launcher that need no traced program."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from tdb.trace.capture import CaptureSummary
from tdb.trace.launch import (
    LaunchError,
    TraceRequest,
    _exit_status,
    _summary_lines,
    _unflushed,
    _wait_for_children,
    _write_atomic,
    default_name,
    default_out_base,
    probe_version,
    resolve_command,
    run,
)

DEAD_PID = 1 << 30  # beyond any pid_max, so never a live process


def request(tmp_path, **kw):
    fields = dict(
        mode="trace",
        program=None,
        module=None,
        args=["a"],
        python=None,
        cwd=None,
        name="n",
        out_base=tmp_path / "o",
    )
    fields.update(kw)
    return TraceRequest(**fields)


def test_default_name():
    assert default_name("services/orders/main.py", None) == "main"
    assert default_name(None, "orders.main") == "orders.main"
    assert default_name("/venv/bin/uvicorn", None) == "uvicorn"


def test_default_out_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert (
        default_out_base("my svc", datetime(2026, 11, 25, 10, 42, 12))
        == tmp_path / "my_svc-20261125-104212"
    )


def test_default_out_base_never_leaves_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    base = default_out_base("../a/b\\c", datetime(2026, 11, 25, 10, 42, 12))
    assert base.parent == tmp_path and base.name == ".._a_b_c-20261125-104212"


def test_resolve_command_forms(tmp_path, monkeypatch):
    prog = tmp_path / "p.py"
    prog.write_text("")

    def req(**kw):
        return request(tmp_path, **kw)

    assert resolve_command(req(program=str(prog))) == [sys.executable, str(prog), "a"]
    assert resolve_command(req(module="pkg.main")) == [
        sys.executable,
        "-m",
        "pkg.main",
        "a",
    ]
    if os.name != "nt":
        bindir = tmp_path / "bin"
        bindir.mkdir()
        exe = bindir / "svc"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        monkeypatch.setenv("PATH", str(bindir))
        assert resolve_command(req(program="svc")) == [str(exe), "a"]
    with pytest.raises(LaunchError, match="command not found: nope"):
        resolve_command(req(program="nope"))


def test_resolve_command_existing_path_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "run.sh").write_text("")  # not executable: still a path that exists
    assert resolve_command(request(tmp_path, program="run.sh")) == [
        str(tmp_path / "run.sh"),
        "a",
    ]


def test_interpreter_path_is_absolute_against_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    req = request(tmp_path, module="m", python=os.path.join(".venv", "bin", "python"))
    assert resolve_command(req)[0] == str(tmp_path / ".venv" / "bin" / "python")


@pytest.mark.skipif(os.name == "nt", reason="PATH lookup of a shell script")
def test_bare_interpreter_name_is_looked_up_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "mypython"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert resolve_command(request(tmp_path, module="m", python="mypython"))[0] == str(
        exe
    )


def test_probe_version():
    assert probe_version(sys.executable) == tuple(sys.version_info[:2])
    with pytest.raises(LaunchError, match="cannot run"):
        probe_version(str(Path(sys.executable).with_name("no-such-python")))


@pytest.mark.parametrize(
    "returncode,expected",
    [(0, (0, None)), (7, (7, None)), (-9, (137, "SIGKILL")), (-2, (130, "SIGINT"))],
)
def test_exit_status(returncode, expected):
    if os.name == "nt" and returncode < 0:
        pytest.skip("no signal deaths on Windows")
    assert _exit_status(returncode) == expected


def test_summary_lines_order_and_plurals():
    s = CaptureSummary(
        processes=1,
        threads=3,
        tasks=1,
        calls=18204,
        skipped=[
            {
                "marker": "skipped",
                "pid": 5,
                "argv": ["/usr/bin/python3.11", "x"],
                "python": "3.11.9",
                "reason": "python 3.11 is older than 3.12",
            }
        ],
        agent_errors=[
            {"marker": "agent-error", "pid": 6, "argv": [], "error": "OSError: boom"}
        ],
        abrupt=[7],
        notes=["pid 7: event cap reached; 12 events dropped"],
    )
    lines = _summary_lines(
        "orders", s, ["prog.py: g"], Path("o.jsonl"), Path("o.perfetto.json")
    )
    assert lines == [
        'tdb: trace "orders": 1 process, 3 threads, 1 task, 18,204 calls',
        "tdb: wrote o.jsonl",
        "tdb: wrote o.perfetto.json  (open in https://ui.perfetto.dev)",
        (
            "tdb: warning: skipped /usr/bin/python3.11 (pid 5): "
            "python 3.11 is older than 3.12"
        ),
        "tdb: warning: tracer error in pid 6: OSError: boom",
        "tdb: warning: pid 7 ended abruptly; its last events may be missing",
        "tdb: warning: pid 7: event cap reached; 12 events dropped",
        "tdb: never called: prog.py: g",
    ]


def test_summary_warns_when_nothing_was_traced():
    lines = _summary_lines(
        "x", CaptureSummary(threads=1, calls=1), [], Path("o.jsonl"), Path("o.pf")
    )
    assert lines[0] == 'tdb: trace "x": 0 processes, 1 thread, 0 tasks, 1 call'
    assert lines[-1] == (
        "tdb: warning: no process was traced (is it Python 3.12+, "
        "started without -I/-E, with PYTHONPATH intact?)"
    )


def _part(path: Path, *records: dict) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


HDR = {"k": "hdr", "pid": 0, "wall": 0, "pc": 0}
EXIT = {"k": "exit", "t": 1, "how": "atexit"}


def test_unflushed_parts(tmp_path):
    live = tmp_path / f"{os.getpid()}.jsonl"
    _part(live, HDR)
    for name in ("run.json", "config.json", f"skipped-{os.getpid()}.json"):
        (tmp_path / name).write_text("{}")
    done: set[str] = set()
    assert _unflushed(tmp_path, done)  # live pid, no exit record yet
    _part(live, HDR, EXIT)
    assert not _unflushed(tmp_path, done)
    assert live.name in done


@pytest.mark.skipif(os.name == "nt", reason="pid_exists cannot tell on Windows")
def test_dead_pid_is_not_waited_for(tmp_path):
    _part(tmp_path / f"{DEAD_PID}.jsonl", HDR)
    _part(tmp_path / f"{DEAD_PID}-2.jsonl")  # empty: killed before its header
    assert not _unflushed(tmp_path, set())


def test_wait_for_children_is_bounded(tmp_path):
    _part(tmp_path / f"{os.getpid()}.jsonl", HDR)
    start = time.monotonic()
    _wait_for_children(tmp_path, timeout=0.3)
    assert 0.3 <= time.monotonic() - start < 2.0
    _part(tmp_path / f"{os.getpid()}.jsonl", HDR, EXIT)
    start = time.monotonic()
    _wait_for_children(tmp_path, timeout=5.0)
    assert time.monotonic() - start < 1.0


def test_write_atomic_replaces_only_on_success(tmp_path):
    out = tmp_path / "o.jsonl"
    out.write_text("old\n")

    def failing(path):
        Path(path).write_text("half")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        _write_atomic(out, failing)
    assert out.read_text() == "old\n" and os.listdir(tmp_path) == ["o.jsonl"]
    _write_atomic(out, lambda path: Path(path).write_text("new\n"))
    assert out.read_text() == "new\n" and os.listdir(tmp_path) == ["o.jsonl"]


def test_existing_parts_dir_refused(tmp_path):
    (tmp_path / "o.parts").mkdir()
    # The command form: no interpreter probe, which tdb's own Python
    # (3.11 is allowed for tdb) would fail first.
    req = request(tmp_path, program=sys.executable, args=["-c", "open('ran', 'w')"])
    with pytest.raises(
        LaunchError,
        match=r"o\.parts already exists \(left by an earlier run\?\); "
        r"merge it with --trace-merge or remove it",
    ):
        run(req)
    assert os.listdir(tmp_path) == ["o.parts"]


def test_discover_mode_not_yet_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        run(request(tmp_path, mode="discover", module="m"))
