"""End to end: ``tdb --trace`` launches a program, supervises it, and writes
the capture and the Perfetto trace.

tdb runs as ``python -P -m tdb`` in a new session.  ``tdb``/``tdb_proc``
SIGKILL that session's process group however the test ends, so neither
tdb nor anything the program started outlives a test.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tdb.trace.capture import load_capture
from tdb.trace.spec import norm_path
from tests.trace_checks import assert_nested

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")
posix = pytest.mark.skipif(os.name == "nt", reason="POSIX signals and scripts")

SRC_DIR = Path(__file__).resolve().parents[2] / "src"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(SRC_DIR),
        "TDB_LOG_DIR": str(tmp_path / "log"),
    }


def _kill_group(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        proc.kill()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # no process left in the group
        pass


@contextlib.contextmanager
def tdb_proc(tmp_path: Path, *argv: str, prefix: tuple[str, ...] = (), **popen_kw):
    """Start tdb (behind ``prefix``, if any) in its own session, stdout and
    stderr piped, cwd tmp_path; SIGKILL the session's process group on the
    way out."""
    kw = dict(
        cwd=tmp_path,
        env=_env(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    kw.update(popen_kw)
    proc = subprocess.Popen([*prefix, sys.executable, "-P", "-m", "tdb", *argv], **kw)
    try:
        yield proc
    finally:
        _kill_group(proc)
        proc.wait()
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()


def tdb(tmp_path: Path, *argv: str, timeout: float = 60, **popen_kw):
    with tdb_proc(tmp_path, *argv, **popen_kw) as proc:
        out, err = proc.communicate(timeout=timeout)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def readline(proc: subprocess.Popen, timeout: float = 30) -> str:
    """The program's next stdout line, stripped; fails instead of hanging
    when none comes (the reader thread ends when tdb_proc kills the group)."""
    lines: queue.Queue = queue.Queue()
    threading.Thread(
        target=lambda: lines.put(proc.stdout.readline()), daemon=True
    ).start()
    try:
        return lines.get(timeout=timeout).strip()
    except queue.Empty:
        raise AssertionError(f"no line from the program in {timeout} s") from None


def _no_core_dumps() -> None:  # preexec_fn: a SIGQUIT'd program dumps no core
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def listing(path: Path) -> list[str]:
    """Directory entries, without tdb's log dir (present only when the
    developer's config enables logging)."""
    return sorted(n for n in os.listdir(path) if n != "log")


def write_prog(tmp_path: Path, source: str) -> Path:
    prog = tmp_path / "prog.py"
    prog.write_text(source, encoding="utf-8")
    return prog


def spec_for(tmp_path: Path, *names: str) -> str:
    spec = tmp_path / "spec.yaml"
    spec.write_text("prog.py:\n" + "".join(f"  - {n}\n" for n in names))
    return str(spec)


def calls(cap, function: str | None = None) -> list[dict]:
    return [
        e
        for e in cap.events
        if e["type"] == "call" and (function is None or e["function"] == function)
    ]


def open_calls(cap) -> dict:
    """Check each call's events come in a valid order; return the calls
    left open, as {(pid, call_id): "running" | "suspended"}."""
    state: dict = {}
    for e in cap.events:
        key = (e["pid"], e.get("call_id"))
        if e["type"] == "call":
            assert key not in state, e
            state[key] = "running"
        elif e["type"] == "suspend":
            assert state.get(key) == "running", e
            state[key] = "suspended"
        elif e["type"] == "resume":
            assert state.get(key) == "suspended", e
            state[key] = "running"
        elif e["type"] in ("return", "raise"):
            assert state.pop(key, None) == "running", e
    return state


def metadata_names(trace: dict, kind: str) -> set[str]:
    return {
        e["args"]["name"]
        for e in trace["traceEvents"]
        if e.get("ph") == "M" and e["name"] == kind
    }


def test_trace_writes_capture_and_perfetto(tmp_path):
    write_prog(tmp_path, "def f():\n    pass\nf()\n")
    spec = spec_for(tmp_path, "f")
    r = tdb(tmp_path, "--trace", spec, "--trace-out", "out", "prog.py")
    cap = load_capture(tmp_path / "out.jsonl")
    assert (
        r.returncode == 0
        and cap.header["name"] == "prog"
        and [e["function"] for e in cap.events if e["type"] == "call"] == ["f"]
    )
    assert "traceEvents" in json.loads((tmp_path / "out.perfetto.json").read_text())
    assert not (tmp_path / "out.parts").exists() and "tdb: wrote" in r.stderr
    assert r.stderr.splitlines()[:3] == [
        'tdb: trace "prog": 1 process, 1 thread, 0 tasks, 1 call',
        "tdb: wrote out.jsonl",
        "tdb: wrote out.perfetto.json  (open in https://ui.perfetto.dev)",
    ]
    h = cap.header
    assert h["command"] == [sys.executable, "prog.py"]  # as typed (Ruling 25)
    assert (h["cwd"], h["spec"], h["exit_code"]) == (str(tmp_path.resolve()), spec, 0)
    assert h["host"] and h["tdb"]
    # Written in place via temp files: nothing else is left behind.
    assert listing(tmp_path) == [
        "out.jsonl",
        "out.perfetto.json",
        "prog.py",
        "spec.yaml",
    ]


REAL_RUN = """\
import asyncio, multiprocessing as mp, os, threading

def work(n):
    return n * 2

def child(n):
    work(n)

async def fetch():
    await asyncio.sleep(0.01)
    return work(1)

async def amain():
    await asyncio.create_task(fetch(), name="fetcher")

if __name__ == "__main__":
    t = threading.Thread(target=work, args=(3,), name="worker-thread")
    t.start()
    t.join()
    asyncio.run(amain())
    p = mp.get_context("spawn").Process(target=child, args=(5,), name="child-proc")
    p.start()
    p.join()
    print(os.getpid(), p.pid)
"""


def test_real_run_capture_contents(tmp_path):
    """The capture of a real run (not hand-made parts): epoch timestamps,
    lane names, process names, consistent call pairing, valid nesting."""
    write_prog(tmp_path, REAL_RUN)
    spec = spec_for(tmp_path, "work", "child", "fetch", "amain")
    t0 = time.time_ns()
    r = tdb(tmp_path, "--trace", spec, "--trace-out", "out", "prog.py")
    t1 = time.time_ns()
    assert r.returncode == 0, r.stderr
    main_pid, child_pid = map(int, r.stdout.split())
    cap = load_capture(tmp_path / "out.jsonl")

    assert t0 <= cap.header["start_ns"] <= cap.header["end_ns"] <= t1
    for e in cap.events:
        assert t0 <= e["ts"] <= t1, e
        assert {"type", "ts", "pid", "process", "tid", "thread", "task_id"} <= set(e)
    procs = {p["pid"]: p for p in cap.processes}
    assert procs[main_pid]["name"] == "MainProcess"
    assert procs[child_pid]["name"] == "child-proc"
    assert procs[child_pid]["ppid"] == main_pid
    for p in cap.processes:
        assert t0 <= p["start_ns"] <= p["end_ns"] <= t1
        assert p["ended"] == "clean", p

    # Lanes: threads and tasks carry their names.
    main_tid = next(e["tid"] for e in calls(cap, "amain"))
    (amain,) = calls(cap, "amain")
    assert (amain["thread"], amain["task"]) == ("MainThread", "Task-1")
    (fetch,) = calls(cap, "fetch")
    assert (fetch["thread"], fetch["task"], fetch["tid"]) == (
        "MainThread",
        "fetcher",
        main_tid,
    )
    assert fetch["task_id"] is not None and fetch["task_id"] != amain["task_id"]
    works = {(e["pid"], e["thread"], e["task"]) for e in calls(cap, "work")}
    assert works == {
        (main_pid, "worker-thread", None),
        (main_pid, "MainThread", "fetcher"),
        (child_pid, "MainThread", None),
    }
    worker = next(e for e in calls(cap, "work") if e["thread"] == "worker-thread")
    assert worker["tid"] != main_tid
    assert {e["process"] for e in calls(cap, "work")} == {"MainProcess", "child-proc"}
    (child,) = calls(cap, "child")
    assert child["pid"] == child_pid and child["process"] == "child-proc"

    # Every call ended, its suspends and resumes alternating in between.
    assert open_calls(cap) == {}
    assert len([e for e in cap.events if e["type"] == "suspend"]) >= 2

    trace = json.loads((tmp_path / "out.perfetto.json").read_text())
    assert_nested(trace)
    procs_shown = metadata_names(trace, "process_name")
    assert any(n.startswith("prog · MainProcess") for n in procs_shown), procs_shown
    assert any(n.startswith("prog · child-proc") for n in procs_shown), procs_shown
    lanes = metadata_names(trace, "thread_name")
    assert any("worker-thread" in n for n in lanes), lanes
    assert any("fetcher" in n for n in lanes), lanes


def test_exit_code_passthrough(tmp_path):
    write_prog(tmp_path, "import sys\ndef f():\n    sys.exit(7)\nf()\n")
    r = tdb(
        tmp_path, "--trace", spec_for(tmp_path, "f"), "--trace-out", "out", "prog.py"
    )
    cap = load_capture(tmp_path / "out.jsonl")
    assert r.returncode == 7 and cap.header["exit_code"] == 7
    assert [e["exception"] for e in cap.events if e["type"] == "raise"] == [
        "SystemExit: 7"
    ]


def test_never_called_reported(tmp_path):
    write_prog(tmp_path, "def f():\n    pass\ndef g():\n    pass\nf()\n")
    r = tdb(
        tmp_path,
        "--trace",
        spec_for(tmp_path, "f", "g"),
        "--trace-out",
        "out",
        "prog.py",
    )
    assert r.returncode == 0
    assert "tdb: never called: prog.py: g" in r.stderr
    assert "never called: prog.py: f" not in r.stderr


def test_cwd_flag_sets_the_programs_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    write_prog(tmp_path, "import os\ndef f():\n    print(os.getcwd())\nf()\n")
    r = tdb(
        tmp_path,
        "--trace",
        spec_for(tmp_path, "f"),
        "--cwd",
        "sub",
        "--trace-out",
        "out",
        "prog.py",
    )
    assert r.returncode == 0 and r.stdout.strip() == str((tmp_path / "sub").resolve())
    cap = load_capture(tmp_path / "out.jsonl")
    assert cap.header["cwd"] == str((tmp_path / "sub").resolve())
    assert [e["function"] for e in calls(cap)] == ["f"]


def _python311() -> str:
    found = shutil.which("python3.11")
    if found is None:
        pytest.skip("python3.11 not found")
    return found


def test_old_interpreter_refused(tmp_path):
    _python311()
    write_prog(tmp_path, "open('ran', 'w').close()\ndef f():\n    pass\n")
    r = tdb(
        tmp_path,
        "--trace",
        spec_for(tmp_path, "f"),
        "--python",
        "python3.11",
        "--trace-out",
        "out",
        "prog.py",
    )
    assert r.returncode == 2 and "needs Python 3.12 or newer" in r.stderr
    assert re.search(
        r"^tdb: error: --trace needs Python 3\.12 or newer; .*python3\.11\S* is 3\.11$",
        r.stderr.strip(),
    ), r.stderr
    assert not (tmp_path / "ran").exists() and not (tmp_path / "out.parts").exists()


@posix
def test_old_python_command_reported_as_skipped(tmp_path):
    py311 = _python311()
    write_prog(tmp_path, "def f():\n    pass\n")
    old = tmp_path / "old"
    old.write_text(f"#!{py311}\nprint('old ran')\n")
    old.chmod(0o755)
    r = tdb(tmp_path, "--trace", spec_for(tmp_path, "f"), "--trace-out", "out", "./old")
    assert r.returncode == 0 and r.stdout == "old ran\n"
    assert re.search(
        r"^tdb: warning: skipped \S+ \(pid \d+\): python 3\.11 is older than 3\.12$",
        r.stderr,
        re.MULTILINE,
    ), r.stderr
    assert "tdb: warning: no process was traced" in r.stderr


def test_parts_dir_exists_refused(tmp_path):
    write_prog(tmp_path, "open('ran', 'w').close()\ndef f():\n    pass\n")
    (tmp_path / "out.parts").mkdir()
    r = tdb(
        tmp_path, "--trace", spec_for(tmp_path, "f"), "--trace-out", "out", "prog.py"
    )
    assert r.returncode == 2 and "already exists" in r.stderr
    assert r.stderr.startswith("tdb: error: ")
    assert not (tmp_path / "ran").exists() and os.listdir(tmp_path / "out.parts") == []


def test_module_form(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "work.py").write_text("def run(x):\n    return x\n")
    (pkg / "__main__.py").write_text(
        "import sys\nfrom pkg.work import run\nrun(sys.argv[1])\n"
    )
    (tmp_path / "s.yaml").write_text("pkg.work:\n  - run\n")
    r = tdb(tmp_path, "--trace", "s.yaml", "-m", "pkg", "--", "hello")
    assert r.returncode == 0, r.stderr
    (out,) = tmp_path.glob("pkg-*.jsonl")
    assert re.fullmatch(r"pkg-\d{8}-\d{6}\.jsonl", out.name)
    assert out.with_name(out.name[: -len(".jsonl")] + ".perfetto.json").is_file()
    cap = load_capture(out)
    assert cap.header["name"] == "pkg"
    assert cap.header["command"] == [sys.executable, "-m", "pkg", "hello"]
    assert [(e["function"], e["file"]) for e in calls(cap)] == [
        ("run", norm_path(pkg / "work.py"))
    ]


@posix
def test_console_script_and_shell_wrapper(tmp_path):
    write_prog(tmp_path, "def f():\n    pass\nif __name__ == '__main__':\n    f()\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    svc = bindir / "svc"
    svc.write_text(
        f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(tmp_path)!r})\n"
        "import prog\nprog.f()\n"
    )
    wrap = bindir / "wrap.sh"
    wrap.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{tmp_path / "prog.py"}" "$@"\n'
    )
    for script in (svc, wrap):
        script.chmod(0o755)
    spec = spec_for(tmp_path, "f")
    env = {**_env(tmp_path), "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
    runs = [
        ("svc", tdb(tmp_path, "--trace", spec, "--trace-out", "o1", "svc", env=env)),
        ("wrap", tdb(tmp_path, "--trace", spec, "--trace-out", "o2", "bin/wrap.sh")),
    ]
    for (name, r), base in zip(runs, ("o1", "o2")):
        assert r.returncode == 0, r.stderr
        cap = load_capture(tmp_path / f"{base}.jsonl")
        assert cap.header["name"] == name
        assert [e["function"] for e in calls(cap)] == ["f"]
    assert load_capture(tmp_path / "o1.jsonl").header["command"] == [str(svc)]


@posix
def test_program_inherits_tdbs_descriptors(tmp_path):
    """A descriptor tdb was handed (as by `3>log`) reaches the program."""
    write_prog(
        tmp_path,
        "import os, sys\ndef f(fd):\n    os.write(fd, b'via fd')\nf(int(sys.argv[1]))\n",
    )
    r_fd, w_fd = os.pipe()
    try:
        r = tdb(
            tmp_path,
            "--trace",
            spec_for(tmp_path, "f"),
            "--trace-out",
            "out",
            "prog.py",
            "--",
            str(w_fd),
            pass_fds=(w_fd,),
        )
        os.close(w_fd)
        w_fd = -1
        assert r.returncode == 0, r.stderr
        assert os.read(r_fd, 100) == b"via fd"
    finally:
        os.close(r_fd)
        if w_fd >= 0:
            os.close(w_fd)


@posix
def test_fork_then_exec_is_not_abrupt(tmp_path):
    """Ruling 18: the pre-exec part of a forked child that exec'd another
    traced program ends "exec" and is not reported as abrupt."""
    write_prog(
        tmp_path,
        "import os, sys\ndef f():\n    pass\nf()\npid = os.fork()\n"
        "if pid == 0:\n    os.execv(sys.executable, [sys.executable, 'other.py'])\n"
        "os.waitpid(pid, 0)\n",
    )
    (tmp_path / "other.py").write_text("print('other ran')\n")
    r = tdb(
        tmp_path, "--trace", spec_for(tmp_path, "f"), "--trace-out", "out", "prog.py"
    )
    assert r.returncode == 0 and r.stdout == "other ran\n", r.stderr
    cap = load_capture(tmp_path / "out.jsonl")
    assert sorted(p["ended"] for p in cap.processes) == ["clean", "clean", "exec"]
    assert "ended abruptly" not in r.stderr and "warning" not in r.stderr


@posix
def test_zero_traced_processes_warning(tmp_path):
    write_prog(tmp_path, "def f():\n    pass\n")
    r = tdb(tmp_path, "--trace", spec_for(tmp_path, "f"), "--trace-out", "out", "true")
    assert r.returncode == 0
    assert 'tdb: trace "true": 0 processes' in r.stderr
    assert "tdb: warning: no process was traced" in r.stderr
    assert (tmp_path / "out.jsonl").is_file() and not (tmp_path / "out.parts").exists()


SIGINT_PROG = """\
import asyncio, threading, time

running = []

def spin(started):
    started.set()
    while True:
        time.sleep(0.05)

async def handle(i):
    running.append(i)
    await asyncio.sleep(30)

async def main():
    started = threading.Event()
    threading.Thread(target=spin, args=(started,), name="spinner", daemon=True).start()
    started.wait()
    tasks = [asyncio.create_task(handle(i), name=f"handler-{i}") for i in range(3)]
    while len(running) < 3:
        await asyncio.sleep(0.01)
    print("ready", flush=True)
    await asyncio.gather(*tasks)

asyncio.run(main())
"""


@posix
def test_sigint_with_calls_in_flight(tmp_path):  # Review Focus 1
    write_prog(tmp_path, SIGINT_PROG)
    spec = spec_for(tmp_path, "handle", "spin", "main")
    with tdb_proc(tmp_path, "--trace", spec, "--trace-out", "out", "prog.py") as p:
        assert readline(p) == "ready"
        os.killpg(p.pid, signal.SIGINT)  # Ctrl-C: tdb and the program alike
        # Ruling 24: tdb dies by the program's signal, as the program would.
        assert p.wait(30) == -signal.SIGINT
        err = p.stderr.read()
    assert err.count("tdb: waiting for prog to exit") == 1
    trace = json.loads((tmp_path / "out.perfetto.json").read_text())
    assert_nested(trace)
    cap = load_capture(tmp_path / "out.jsonl")
    handles = calls(cap, "handle")
    assert len(handles) == 3
    assert {e["task"] for e in handles} == {"handler-0", "handler-1", "handler-2"}
    raised = {e["call_id"]: e["exception"] for e in cap.events if e["type"] == "raise"}
    for e in handles:
        assert raised[e["call_id"]].startswith("CancelledError"), raised
    # Only the daemon thread's call never ended; Perfetto shows it unfinished.
    (spin,) = calls(cap, "spin")
    assert spin["thread"] == "spinner"
    assert list(open_calls(cap)) == [(spin["pid"], spin["call_id"])]
    spins = [
        e for e in trace["traceEvents"] if e.get("ph") == "X" and e["name"] == "spin"
    ]
    assert [e["args"].get("unfinished") for e in spins] == [True]
    assert cap.processes[0]["ended"] == "signal:SIGINT"
    assert cap.header["exit_code"] == 130


WAITER = (
    "import time\n"
    "def wait():\n"
    "    print('ready', flush=True)\n"
    "    time.sleep(60)\n"
    "wait()\n"
)


@posix
def test_sigquit_still_yields_outputs(tmp_path):
    """Ruling 23: Ctrl-\\ kills the program (it gets SIGQUIT from the
    terminal), not tdb, which writes the outputs.  SIGQUIT dumps core, so
    tdb exits 128 + 3 rather than dying by it (Ruling 24)."""
    write_prog(tmp_path, WAITER)
    spec = spec_for(tmp_path, "wait")
    with tdb_proc(
        tmp_path,
        "--trace",
        spec,
        "--trace-out",
        "out",
        "prog.py",
        preexec_fn=_no_core_dumps,
    ) as p:
        assert readline(p) == "ready"
        os.killpg(p.pid, signal.SIGQUIT)
        assert p.wait(30) == 128 + signal.SIGQUIT
        err = p.stderr.read()
    assert err.count("tdb: waiting for prog to exit") == 1 and "tdb: wrote" in err
    cap = load_capture(tmp_path / "out.jsonl")
    assert [pr["ended"] for pr in cap.processes] == ["signal:SIGQUIT"]
    assert cap.header["exit_code"] == 128 + signal.SIGQUIT
    assert not (tmp_path / "out.parts").exists()


@posix
def test_sigterm_to_tdb_is_forwarded(tmp_path):
    write_prog(
        tmp_path,
        "import signal, sys, time\n"
        "def on_term(signum, frame):\n"
        "    print('got TERM', flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, on_term)\n"
        "def wait():\n"
        "    print('ready', flush=True)\n"
        "    while True:\n"
        "        time.sleep(0.05)\n"
        "wait()\n",
    )
    spec = spec_for(tmp_path, "wait")
    with tdb_proc(tmp_path, "--trace", spec, "--trace-out", "out", "prog.py") as p:
        assert readline(p) == "ready"
        os.kill(p.pid, signal.SIGTERM)  # tdb only; it forwards to the program
        assert readline(p) == "got TERM"
        assert p.wait(30) == 0
    cap = load_capture(tmp_path / "out.jsonl")
    assert [e["function"] for e in calls(cap)] == ["wait"]
    assert [e["exception"] for e in cap.events if e["type"] == "raise"] == [
        "SystemExit: 0"
    ]
    assert (tmp_path / "out.perfetto.json").is_file()


@posix
def test_sigkill_of_program_still_yields_outputs(tmp_path):
    write_prog(
        tmp_path,
        "import os, time\n"
        "def nap():\n"
        "    print(os.getpid(), flush=True)\n"
        "    time.sleep(60)\n"
        "nap()\n",
    )
    spec = spec_for(tmp_path, "nap")
    with tdb_proc(tmp_path, "--trace", spec, "--trace-out", "out", "prog.py") as p:
        pid = int(readline(p))
        os.kill(pid, signal.SIGKILL)
        assert p.wait(30) == -signal.SIGKILL  # Ruling 24
        err = p.stderr.read()
    cap = load_capture(tmp_path / "out.jsonl")
    assert [pr["ended"] for pr in cap.processes] == ["signal:SIGKILL"]
    assert cap.header["exit_code"] == 137
    assert "ended abruptly" not in err and "tdb: wrote" in err
    assert (tmp_path / "out.perfetto.json").is_file()


DISPOSITIONS = (
    "import signal\n"
    "def f():\n"
    "    for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT'):\n"
    "        print(name, repr(signal.getsignal(getattr(signal, name))))\n"
    "f()\n"
)


@posix
@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT"])
def test_ignored_signals_stay_ignored(tmp_path, sig):
    """Ruling 22: a signal tdb inherits as ignored (nohup, a background
    job) stays ignored, so the program sees what it would without tdb."""
    write_prog(tmp_path, DISPOSITIONS)
    ignore = functools.partial(signal.signal, getattr(signal, sig), signal.SIG_IGN)
    untraced = subprocess.run(
        [sys.executable, "prog.py"],
        cwd=tmp_path,
        preexec_fn=ignore,
        capture_output=True,
        text=True,
        timeout=60,
    )
    r = tdb(
        tmp_path,
        "--trace",
        spec_for(tmp_path, "f"),
        "--trace-out",
        "out",
        "prog.py",
        preexec_fn=ignore,
    )
    assert r.returncode == 0, r.stderr
    assert f"{sig} {signal.SIG_IGN!r}" in untraced.stdout
    assert r.stdout == untraced.stdout
    assert [e["function"] for e in calls(load_capture(tmp_path / "out.jsonl"))] == ["f"]


@posix
def test_closed_stderr_keeps_the_exit_code(tmp_path):
    """tdb's stderr gone (a `| tee` killed by the same Ctrl-C, a hung-up
    terminal): the outputs are written and the exit code passes through."""
    write_prog(
        tmp_path,
        "import sys\ndef f():\n    sys.stdin.readline()\n    sys.exit(7)\nf()\n",
    )
    spec = spec_for(tmp_path, "f")
    with tdb_proc(
        tmp_path,
        "--trace",
        spec,
        "--trace-out",
        "out",
        "prog.py",
        stdin=subprocess.PIPE,
    ) as p:
        p.stderr.close()
        p.stdin.write("go\n")
        p.stdin.close()
        assert p.wait(60) == 7
    assert (tmp_path / "out.jsonl").is_file() and (
        tmp_path / "out.perfetto.json"
    ).is_file()
    assert not (tmp_path / "out.parts").exists()


@posix
def test_closed_stderr_during_ctrl_c(tmp_path):
    """The waiting line fails to print inside tdb's SIGINT handler: tdb
    still waits for the program and writes the outputs."""
    write_prog(tmp_path, WAITER)
    spec = spec_for(tmp_path, "wait")
    with tdb_proc(tmp_path, "--trace", spec, "--trace-out", "out", "prog.py") as p:
        p.stderr.close()
        assert readline(p) == "ready"
        os.killpg(p.pid, signal.SIGINT)
        assert p.wait(30) == -signal.SIGINT
    cap = load_capture(tmp_path / "out.jsonl")
    assert [pr["ended"] for pr in cap.processes] == ["signal:SIGINT"]
    assert not (tmp_path / "out.parts").exists()


HUP_COUNTER = (
    "import signal, time\n"
    "hups = []\n"
    "signal.signal(signal.SIGHUP, lambda signum, frame: hups.append(signum))\n"
    "def wait():\n"
    "    print('ready', flush=True)\n"
    "    deadline = time.monotonic() + 30\n"
    "    while not hups and time.monotonic() < deadline:\n"
    "        time.sleep(0.01)\n"
    "    time.sleep(0.5)  # room for a second, forwarded SIGHUP\n"
    "    print(len(hups), flush=True)\n"
    "wait()\n"
)


# A session leader standing in for an interactive shell: it takes the pty
# as its controlling terminal, runs the command as its foreground job, and,
# as bash -i does, passes SIGHUP on to the job when the terminal hangs up.
MINI_SHELL = """\
import os, signal, subprocess, sys
os.close(os.open(sys.argv[1], os.O_RDWR))
job = subprocess.Popen(sys.argv[2:], process_group=0)
print(job.pid, flush=True)
os.tcsetpgrp(os.open("/dev/tty", os.O_RDWR), job.pid)
signal.signal(signal.SIGHUP, lambda signum, frame: os.killpg(job.pid, signal.SIGHUP))
sys.exit(job.wait())
"""


def _pty_name() -> tuple[int, str]:
    import pty

    master, slave = pty.openpty()
    name = os.ttyname(slave)
    os.close(slave)
    return master, name


@pytest.mark.skipif(sys.platform != "linux", reason="pty hangup checked on Linux")
def test_terminal_hangup_relayed_by_the_shell_is_not_doubled(tmp_path):
    """The shell passes the hangup's SIGHUP to tdb and the program alike;
    tdb, whose terminal is gone, does not send the program a second one."""
    write_prog(tmp_path, HUP_COUNTER)
    spec = spec_for(tmp_path, "wait")
    master, tty_name = _pty_name()
    job = None
    try:
        with tdb_proc(
            tmp_path,
            "--trace",
            spec,
            "--trace-out",
            "out",
            "prog.py",
            prefix=(sys.executable, "-c", MINI_SHELL, tty_name),
        ) as p:
            job = int(readline(p))  # tdb, leading its own process group
            assert readline(p) == "ready"
            os.close(master)  # hang up the terminal
            master = -1
            assert readline(p) == "1"
            assert p.wait(30) == 0
    finally:
        if master >= 0:
            os.close(master)
        if job is not None:
            with contextlib.suppress(OSError):
                os.killpg(job, signal.SIGKILL)
    assert (tmp_path / "out.jsonl").is_file()


@pytest.mark.skipif(sys.platform != "linux", reason="pty hangup checked on Linux")
def test_terminal_hangup_with_tdb_as_session_leader_is_forwarded(tmp_path):
    """As the session leader (``xterm -e tdb``) tdb alone gets the
    hangup's SIGHUP, so it must pass it on."""
    write_prog(tmp_path, HUP_COUNTER)
    spec = spec_for(tmp_path, "wait")
    master, tty_name = _pty_name()

    def take_tty():  # after setsid: the pty becomes tdb's controlling terminal
        os.close(os.open(tty_name, os.O_RDWR))

    try:
        with tdb_proc(
            tmp_path,
            "--trace",
            spec,
            "--trace-out",
            "out",
            "prog.py",
            preexec_fn=take_tty,
        ) as p:
            assert readline(p) == "ready"
            os.close(master)  # hang up the terminal
            master = -1
            assert readline(p) == "1"
            assert p.wait(30) == 0
    finally:
        if master >= 0:
            os.close(master)
    assert (tmp_path / "out.jsonl").is_file()


@posix
def test_sighup_to_tdb_is_forwarded(tmp_path):
    write_prog(tmp_path, HUP_COUNTER)
    spec = spec_for(tmp_path, "wait")
    with tdb_proc(tmp_path, "--trace", spec, "--trace-out", "out", "prog.py") as p:
        assert readline(p) == "ready"
        os.kill(p.pid, signal.SIGHUP)  # tdb only
        assert readline(p) == "1"
        assert p.wait(30) == 0


SHOW_ARGV = "import sys\ndef f():\n    print(sys.argv[0])\n    print(__file__)\nf()\n"


@posix
def test_program_path_as_typed(tmp_path):
    """Ruling 25: the program sees the sys.argv[0] and __file__ it would
    without tdb, for a symlinked script and for a relative path; with
    --cwd the path is made absolute, never resolved."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "main.py").write_text(SHOW_ARGV)
    (tmp_path / "orders.py").symlink_to(Path("real") / "main.py")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "prog.py").write_text(SHOW_ARGV)
    (tmp_path / "spec.yaml").write_text("orders.py:\n  - f\nsub/prog.py:\n  - f\n")
    for typed in ("orders.py", os.path.join("sub", "prog.py")):
        untraced = subprocess.run(
            [sys.executable, typed],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        r = tdb(tmp_path, "--trace", "spec.yaml", "--trace-out", "out", typed)
        assert r.returncode == 0, r.stderr
        assert r.stdout == untraced.stdout and untraced.stdout.startswith(typed)
        cap = load_capture(tmp_path / "out.jsonl")
        assert [e["function"] for e in calls(cap)] == ["f"]
    (tmp_path / "elsewhere").mkdir()
    r = tdb(
        tmp_path,
        "--trace",
        "spec.yaml",
        "--cwd",
        "elsewhere",
        "--trace-out",
        "out",
        "orders.py",
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == str(tmp_path.resolve() / "orders.py")


def test_failed_write_keeps_the_events(tmp_path):
    """Ruling 21: a write that fails leaves no partial output and keeps
    the parts directory for --trace-merge."""
    write_prog(tmp_path, "def f():\n    pass\nf()\n")
    (tmp_path / "out.perfetto.json").mkdir()  # os.replace onto it fails
    r = tdb(
        tmp_path, "--trace", spec_for(tmp_path, "f"), "--trace-out", "out", "prog.py"
    )
    assert r.returncode == 1
    (line,) = r.stderr.splitlines()
    assert line.startswith("tdb: error: could not write out.perfetto.json: ")
    assert line.endswith("; events kept in out.parts")
    assert listing(tmp_path) == [
        "out.jsonl",
        "out.parts",
        "out.perfetto.json",
        "prog.py",
        "spec.yaml",
    ]
    assert os.listdir(tmp_path / "out.perfetto.json") == []
    assert any(n.endswith(".jsonl") for n in os.listdir(tmp_path / "out.parts"))
