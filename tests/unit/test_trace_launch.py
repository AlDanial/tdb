"""Pieces of ``tdb --trace``'s launcher that need no traced program."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from tdb.trace import launch
from tdb.trace.capture import CaptureSummary
from tdb.trace.launch import (
    LaunchError,
    TraceRequest,
    _end_status,
    _exit_status,
    _summary_lines,
    _supervise,
    _unflushed,
    _wait_for_children,
    default_name,
    default_out_base,
    probe_version,
    resolve_command,
    run,
    say,
    write_atomic,
)

SRC_DIR = Path(__file__).resolve().parents[2] / "src"
posix = pytest.mark.skipif(os.name == "nt", reason="POSIX signals")

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


def test_command_path_as_typed_unless_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "run.sh").write_text("")  # not executable, but it exists
    typed = os.path.join("bin", "run.sh")
    assert resolve_command(request(tmp_path, program=typed)) == [typed, "a"]
    assert resolve_command(request(tmp_path, program=typed, cwd="elsewhere")) == [
        str(tmp_path / "bin" / "run.sh"),
        "a",
    ]
    # A bare name only found in the cwd must be run as a path.
    (tmp_path / "here.sh").write_text("")
    assert resolve_command(request(tmp_path, program="here.sh")) == [
        str(tmp_path / "here.sh"),
        "a",
    ]


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_py_program_as_typed_unless_cwd(tmp_path, monkeypatch):
    """Ruling 25: as typed, so sys.argv[0] is unchanged; with --cwd made
    absolute but never resolved (a symlink stays the symlink)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real.py").write_text("")
    (tmp_path / "link.py").symlink_to("real.py")
    assert resolve_command(request(tmp_path, program="link.py"))[1] == "link.py"
    assert resolve_command(request(tmp_path, program="link.py", cwd="sub"))[1] == str(
        tmp_path / "link.py"
    )


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


@posix
@pytest.mark.parametrize(
    "returncode,expected",
    [
        (0, 0),
        (7, 7),
        (-signal.SIGINT, -signal.SIGINT),
        (-signal.SIGKILL, -signal.SIGKILL),
        (-signal.SIGTERM, -signal.SIGTERM),
        (-signal.SIGQUIT, 128 + signal.SIGQUIT),  # dumps core: no core of tdb's
        (-signal.SIGSEGV, 128 + signal.SIGSEGV),
        (-signal.SIGABRT, 128 + signal.SIGABRT),
    ],
)
def test_end_status(returncode, expected):
    """Ruling 24: tdb ends by the program's signal unless it dumps core."""
    assert _end_status(returncode) == expected


@pytest.mark.parametrize(
    "status,returncode",
    [(3, 3), (-signal.SIGINT, -signal.SIGINT), (-signal.SIGTERM, -signal.SIGTERM)],
)
def test_exit_as(status, returncode):
    if os.name == "nt" and status < 0:
        pytest.skip("no signal deaths on Windows")
    code = (
        "import signal; from tdb.trace.launch import exit_as; "
        # an inherited handler must not stop the death by signal
        "signal.signal(signal.SIGTERM, lambda *a: None); "
        f"exit_as({status})"
    )
    done = subprocess.run(
        [sys.executable, "-P", "-c", code],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == returncode, done.stderr


def test_say_survives_a_closed_stderr(monkeypatch):
    """A failed write must neither raise nor leave unflushable bytes
    behind (CPython exits 120 when flushing stderr fails at exit)."""
    r, w = os.pipe()
    os.close(r)
    stream = open(w, "w", encoding="utf-8")
    try:
        monkeypatch.setattr(sys, "stderr", stream)
        say("tdb: one")  # EPIPE: the descriptor becomes /dev/null
        say("tdb: two")
        stream.write("more\n")
        stream.flush()
    finally:
        monkeypatch.undo()
        stream.close()


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
        killed=[("orders", 4, "SIGKILL")],
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
        (
            "tdb: warning: orders (pid 4) was killed by SIGKILL; "
            "its last events may be missing"
        ),
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
    # A part that ends at an exec is finished too, though its pid lives on.
    pre_exec = tmp_path / f"{os.getpid()}-2.jsonl"
    _part(pre_exec, HDR, {"k": "exec", "t": 1})
    assert not _unflushed(tmp_path, done) and pre_exec.name in done


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
        write_atomic(out, failing)
    assert out.read_text() == "old\n" and os.listdir(tmp_path) == ["o.jsonl"]
    write_atomic(out, lambda path: Path(path).write_text("new\n"))
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


# --- supervision, with a stand-in for the program (no process is started) ---

WATCHED = [s for s in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT") if hasattr(signal, s)]


@pytest.fixture
def handlers():
    """Snapshot pytest's handlers; restore them whatever the test did."""
    sigs = [getattr(signal, s) for s in WATCHED]
    saved = {s: signal.getsignal(s) for s in sigs}
    yield lambda: {s: signal.getsignal(s) for s in sigs}
    for s, h in saved.items():
        signal.signal(s, h)


def fake_popen(monkeypatch, *, at_start=(), at_wait=()):
    """Make launch's Popen a stand-in that raises signals in this process
    while "starting" and while "running", and records what tdb sends it.
    A signal is raised only while handled or ignored, never with its
    default action (which would kill pytest)."""
    sent: list = []
    seen: dict = {}

    def raise_all(sigs):
        for sig in sigs:
            handler = signal.getsignal(sig)
            assert callable(handler) or handler is signal.SIG_IGN, (sig, handler)
            signal.raise_signal(sig)  # its Python handler runs before this returns

    class Proc:
        pid = DEAD_PID
        returncode = 0

        def __init__(self, cmd, **kw):
            raise_all(at_start)

        def send_signal(self, sig):
            sent.append(sig)

        def terminate(self):
            sent.append("terminate")

        def wait(self):
            seen.update({s: signal.getsignal(s) for s in at_wait})
            raise_all(at_wait)
            return 0

    monkeypatch.setattr(launch.subprocess, "Popen", Proc)
    return sent, seen


@posix
def test_signals_before_the_start_reach_the_program(
    tmp_path, monkeypatch, capsys, handlers
):
    before = handlers()
    sent, _ = fake_popen(
        monkeypatch, at_start=[signal.SIGINT, signal.SIGTERM, signal.SIGQUIT]
    )
    _supervise(["prog"], request(tmp_path), tmp_path)
    assert sent == [signal.SIGINT, signal.SIGTERM, signal.SIGQUIT]
    assert capsys.readouterr().err == "tdb: waiting for n to exit\n"
    assert handlers() == before


@posix
def test_while_running_only_term_and_hup_are_forwarded(
    tmp_path, monkeypatch, capsys, handlers
):
    """SIGINT and SIGQUIT (Ruling 23) reach the program from the terminal;
    tdb survives them and says so once."""
    before = handlers()
    sent, _ = fake_popen(
        monkeypatch,
        at_wait=[signal.SIGINT, signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP],
    )
    _supervise(["prog"], request(tmp_path), tmp_path)
    assert sent == [signal.SIGTERM, signal.SIGHUP]
    assert capsys.readouterr().err == "tdb: waiting for n to exit\n"
    assert handlers() == before


@posix
@pytest.mark.parametrize("name", WATCHED)
def test_ignored_signal_stays_ignored(tmp_path, monkeypatch, capsys, handlers, name):
    """Ruling 22: inherited as ignored, it stays ignored and is never sent."""
    sig = getattr(signal, name)
    signal.signal(sig, signal.SIG_IGN)
    sent, seen = fake_popen(monkeypatch, at_start=[sig], at_wait=[sig])
    _supervise(["prog"], request(tmp_path), tmp_path)
    assert seen[sig] is signal.SIG_IGN and sent == []
    assert capsys.readouterr().err == ""
    assert signal.getsignal(sig) is signal.SIG_IGN


def test_ctrl_c_before_the_start_removes_the_parts_dir(tmp_path, monkeypatch):
    def interrupted(cmd, req, parts_dir):
        raise KeyboardInterrupt

    monkeypatch.setattr(launch, "_supervise", interrupted)
    req = request(tmp_path, program=sys.executable, args=["-c", "pass"])
    assert run(req) == launch._INTERRUPTED
    assert os.listdir(tmp_path) == []  # a later run is not blocked


def test_ctrl_c_while_writing_keeps_the_events(tmp_path, monkeypatch, capsys):
    hdr = {"k": "hdr", "pid": DEAD_PID, "ppid": 1, "proc": "MainProcess"}
    hdr.update(argv=["x"], py="3.12.8", wall=1, pc=0, mode="trace")

    class Ended:
        pid = DEAD_PID
        returncode = 0

    def supervised(cmd, req, parts_dir):
        _part(parts_dir / f"{DEAD_PID}.jsonl", hdr, EXIT)
        return Ended()

    def interrupted(captures, path):
        Path(path).write_text("half")
        raise KeyboardInterrupt

    monkeypatch.setattr(launch, "_supervise", supervised)
    monkeypatch.setattr(launch, "write_trace", interrupted)
    req = request(tmp_path, program=sys.executable, args=["-c", "pass"])
    assert run(req) == launch._INTERRUPTED
    parts = tmp_path / "o.parts"
    assert capsys.readouterr().err == f"tdb: interrupted; events kept in {parts}\n"
    assert sorted(os.listdir(tmp_path)) == ["o.jsonl", "o.parts"]
    assert f"{DEAD_PID}.jsonl" in os.listdir(parts)


def test_ctrl_c_before_creation_leaves_an_earlier_parts_dir(tmp_path, monkeypatch):
    (tmp_path / "o.parts").mkdir()  # an earlier run's: not ours to remove

    def interrupted(req):
        raise KeyboardInterrupt

    monkeypatch.setattr(launch, "resolve_command", interrupted)
    assert run(request(tmp_path, program="x")) == launch._INTERRUPTED
    assert os.listdir(tmp_path) == ["o.parts"]


# --- final review L4, L5, M3 ---------------------------------------------------


def test_exit_as_keeps_a_windows_status_in_range():
    """L5 (Ruling 30): Windows reports a Ctrl-C'd program's exit as
    0xC000013A, which sys.exit overflows on older Pythons (gh-125842):
    passed on as the same 32 bits, signed."""
    with pytest.raises(SystemExit) as ei:
        launch.exit_as(0xC000013A)
    assert ei.value.code == 0xC000013A - (1 << 32) == -1073741510
    with pytest.raises(SystemExit) as ei:
        launch.exit_as(0x7FFFFFFF)
    assert ei.value.code == 0x7FFFFFFF


WRITE_PHASE = """\
import os, signal, sys
from pathlib import Path
from tdb.trace import discover, launch

mode, tmp = sys.argv[1], Path(sys.argv[2])
signal.signal(signal.SIGHUP, signal.SIG_IGN)  # inherited as ignored: stays so
dead = 1 << 30
hdr = '{"k":"hdr","pid":%d,"ppid":1,"proc":"MainProcess","argv":["x"],' % dead
hdr += '"py":"3.12.8","wall":1,"pc":0,"mode":"%s"}' % mode
count = '{"k":"count","file":%s,"qual":"f","line":1,"kind":"function","n":1,'
count = count % repr(str((tmp / "prog.py").resolve())).replace("'", '"')
count += '"capped":false}'


class Ended:
    pid, returncode = dead, 0


def supervised(cmd, req, parts_dir):
    text = hdr + "\\n" + count + "\\n" + '{"k":"exit","t":9,"how":"atexit"}\\n'
    (parts_dir / ("%d.jsonl" % dead)).write_text(text)
    return Ended()


def signalled(real):
    def write(*args, **kwargs):
        # Ctrl-\\ again, a SIGTERM, a SIGHUP, while tdb writes: it carries on.
        held = signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        print("SIGHUP still ignored" if held else "SIGHUP replaced", flush=True)
        for sig in (signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP):
            os.kill(os.getpid(), sig)
        return real(*args, **kwargs)
    return write


launch._supervise = supervised
if mode == "trace":
    launch.write_capture = signalled(launch.write_capture)
else:
    discover.write_spec = signalled(discover.write_spec)
req = launch.TraceRequest(
    mode=mode, program=sys.executable, module=None, args=[], python=None,
    cwd=None, name="n", out_base=tmp / "o", discover_out=tmp / "out.yaml",
)
launch.exit_as(launch.run(req))
"""


@posix
@pytest.mark.parametrize("mode", ["trace", "discover"])
def test_signals_while_writing_do_not_stop_the_writes(tmp_path, mode):
    """L4: once the program has ended, SIGQUIT (a second Ctrl-\\), SIGTERM
    and SIGHUP no longer kill tdb half-way through its writes (leaving
    the parts and a temp file, and no word): it finishes them and exits
    as the program did.  A signal it inherited as ignored stays so."""
    (tmp_path / "prog.py").write_text("def f():\n    pass\n")

    def no_core_dumps():
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    done = subprocess.run(
        [sys.executable, "-P", "-c", WRITE_PHASE, mode, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
        preexec_fn=no_core_dumps,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "SIGHUP still ignored\n"
    outputs = ["o.jsonl", "o.perfetto.json"] if mode == "trace" else ["out.yaml"]
    assert sorted(os.listdir(tmp_path)) == sorted(outputs + ["prog.py"])
    assert "tdb: wrote" in done.stderr


def test_default_out_base_in_use_gets_a_number(tmp_path):
    """M3 (Ruling 31): two services both named main, started in the same
    second from one cwd: a default BASE whose parts or outputs exist
    takes the first free -2, -3, ...; an explicit --trace-out keeps
    refusing (test_existing_parts_dir_refused)."""
    (tmp_path / "o.parts").mkdir()
    (tmp_path / "o-2.perfetto.json").write_text("")
    req = request(
        tmp_path, program=sys.executable, args=["-c", "pass"], out_is_default=True
    )
    assert run(req) == 0
    assert sorted(os.listdir(tmp_path)) == [
        "o-2.perfetto.json",
        "o-3.jsonl",
        "o-3.perfetto.json",
        "o.parts",
    ]
