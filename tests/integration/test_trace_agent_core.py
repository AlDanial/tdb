"""The trace agent's core: ordinary functions, threads, flushing, exit, markers."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from tdb.trace.spec import norm_path
from tests.integration.trace_helpers import read_parts, run_traced, start_traced, target

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")

NEST = "def g():\n    return 2\ndef f():\n    return g()\nf()\n"


def test_records_entry_and_return_with_nesting(tmp_path):
    parts = run_traced(tmp_path, NEST)
    (stem,) = parts.records
    seq = [
        (r["k"], parts.qual_of(stem, r.get("fn")) if r["k"] == "call" else r["c"])
        for r in parts.records[stem]
        if r["k"] in ("call", "ret")
    ]
    assert seq == [("call", "f"), ("call", "g"), ("ret", 2), ("ret", 1)]
    fns = {r["qual"]: r for r in parts.all("fn")}
    assert fns["f"]["kind"] == "function" and fns["f"]["file"] == norm_path(
        tmp_path / "prog.py"
    )
    ts = [r["t"] for r in parts.records[stem] if "t" in r]
    assert ts == sorted(ts)


def test_header_and_exit_records(tmp_path):
    recs = next(iter(run_traced(tmp_path, NEST).records.values()))
    hdr = recs[0]
    assert hdr["k"] == "hdr" and hdr["proc"] == "MainProcess" and hdr["mode"] == "trace"
    assert hdr["argv"][0].endswith("prog.py") and hdr["py"].startswith("3.")
    assert isinstance(hdr["wall"], int) and isinstance(hdr["pc"], int)
    assert recs[-1] == {"k": "exit", "t": recs[-1]["t"], "how": "atexit"}


def test_thread_names(tmp_path):
    src = "import threading\ndef f():\n    pass\nt = threading.Thread(target=f, name='worker-1')\nt.start(); t.join()\nf()\n"
    parts = run_traced(tmp_path, src)
    names = {r["tid"]: r["name"] for r in parts.all("th")}
    assert sorted(names[c["tid"]] for c in parts.calls("f")) == [
        "MainThread",
        "worker-1",
    ]


def test_untraced_code_is_disabled(tmp_path):
    def callbacks(n):
        src = (
            f"def hot():\n    return 1\nfor _ in range({n}):\n    hot()\n"
            "import _tdb_trace_agent\nprint(_tdb_trace_agent.stats()['py_start_callbacks'])\n"
        )
        out = run_traced(
            tmp_path, src, [target(tmp_path / "prog.py", "nothing_here")]
        ).stdout
        return int(out.splitlines()[-1])

    assert abs(callbacks(100_000) - callbacks(10)) <= 2


def test_program_sees_unchanged_sys_path_and_argv(tmp_path):
    src = "import sys, json\nprint(json.dumps([sys.path, sys.argv]))\n"
    traced = run_traced(tmp_path, src, args=("--port", "8001")).stdout
    plain = subprocess.run(
        [sys.executable, "prog.py", "--port", "8001"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    ).stdout
    assert traced == plain


def test_chains_existing_sitecustomize(tmp_path):
    user = tmp_path / "usersite"
    user.mkdir()
    (user / "sitecustomize.py").write_text("print('user-site-ran')\n")
    parts = run_traced(tmp_path, NEST, extra_env={"PYTHONPATH": str(user)})
    assert parts.stdout.startswith("user-site-ran") and parts.calls("f")


def test_cprofile_still_works(tmp_path):
    src = NEST + "import cProfile\ncProfile.run('f()')\n"
    parts = run_traced(tmp_path, src)
    assert parts.returncode == 0 and "function calls" in parts.stdout


def test_old_python_writes_skipped_marker(tmp_path):
    py311 = shutil.which("python3.11") or pytest.skip("no python3.11")
    parts = run_traced(tmp_path, NEST + "print('ran')\n", python=py311)
    assert parts.records == {} and parts.stdout.strip() == "ran"
    assert [(m["marker"], m["reason"]) for m in parts.markers] == [
        ("skipped", "python 3.11 is older than 3.12")
    ]


def test_unreadable_config_writes_agent_error(tmp_path):
    parts = run_traced(
        tmp_path,
        NEST + "print('ran')\n",
        extra_env={"TDB_TRACE_CONFIG": str(tmp_path / "missing.json")},
    )
    assert parts.returncode == 0 and parts.stdout.strip() == "ran"
    assert [m["marker"] for m in parts.markers] == ["agent-error"]


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL")
def test_flush_bounds_loss_on_sigkill(tmp_path):
    src = NEST + "import time\nprint('ready', flush=True)\ntime.sleep(30)\n"
    proc, parts_dir = start_traced(tmp_path, src)
    assert proc.stdout.readline().strip() == "ready"
    time.sleep(0.6)
    proc.kill()
    proc.wait()
    proc.stdout.close()
    assert read_parts(parts_dir).calls("f")


@pytest.mark.skipif(os.name == "nt", reason="SIGINT")
def test_sigint_in_traced_hot_loop_reaches_program(tmp_path):
    # Python runs signal handlers inside monitoring callbacks too; a
    # callback that swallowed BaseException lost most of these Ctrl-Cs.
    src = (
        "def spin():\n    return 1\ndef f():\n    pass\nprint('ready', flush=True)\n"
        "try:\n    while True:\n        spin()\nexcept KeyboardInterrupt:\n    f()\n    print('interrupted')\n"
    )
    for attempt in range(5):
        run_dir = tmp_path / str(attempt)
        run_dir.mkdir()
        proc, parts_dir = start_traced(run_dir, src)
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.05)
        proc.send_signal(signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail(f"attempt {attempt}: SIGINT never reached the program")
        assert out.strip() == "interrupted" and proc.returncode == 0
        parts = read_parts(parts_dir)
        assert parts.calls("f") and parts.all("exit")  # tracing went on


def test_clean_exit_while_the_flusher_writes_loses_nothing(tmp_path):
    # The program exits while the flusher is inside a slow write (its first
    # os.write sleeps 2.5 s).  Every record must still reach the part file,
    # and "exit" must come last; a final flush that gave up waiting for the
    # writer lost the writer's whole batch.
    src = (
        "import os, time, _thread, _tdb_trace_agent as A\n"
        "real_write, slow = os.write, [True]\n"
        "def write(fd, data):\n"
        "    if slow[0] and _thread.get_ident() == A._flusher_ident:\n"
        "        slow[0] = False\n        time.sleep(2.5)\n"
        "    return real_write(fd, data)\n"
        "os.write = write\n"
        "def f():\n    return 1\n"
        "for _ in range(200_000):\n    f()\n"
        "time.sleep(0.6)\n"  # the flusher wakes and starts its slow write
    )
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    (recs,) = parts.records.values()
    assert len(parts.calls("f")) == 200_000 and len(parts.all("ret")) == 200_000
    assert recs[-1] == {"k": "exit", "t": recs[-1]["t"], "how": "atexit"}
    assert parts.returncode == 0 and parts.stderr == ""


def test_backlog_stays_bounded_in_hot_loop(tmp_path):
    src = (
        "import _tdb_trace_agent as A, time\ndef f():\n    return 1\npeak = 0\n"
        "end = time.perf_counter() + 1.0\nwhile time.perf_counter() < end:\n"
        "    for _ in range(1000):\n        f()\n    peak = max(peak, len(A._buf))\n"
        "print(peak, A._HIGH_WATER)\n"
    )
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    peak, high_water = map(int, parts.stdout.split())
    assert peak <= 1.5 * high_water  # without backpressure it passes 3x in 0.25 s


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL")
def test_sigkill_in_hot_loop_loses_only_recent_events(tmp_path):
    src = (
        "import time\ndef f():\n    return 1\nn = 0\nnext_report = 0\nwhile True:\n"
        "    for _ in range(1000):\n        f()\n    n += 1000\n"
        "    if time.perf_counter() >= next_report:\n"
        "        print(n, flush=True)\n        next_report = time.perf_counter() + 0.05\n"
    )
    proc, parts_dir = start_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    # (when the test read it, calls made by then), timed from the first
    # report so a slow start-up under load cannot shorten the run.
    seen = [(time.monotonic(), int(proc.stdout.readline()))]
    while time.monotonic() - seen[0][0] < 2.0:
        seen.append((time.monotonic(), int(proc.stdout.readline())))
    killed_at = time.monotonic()
    proc.kill()
    proc.wait()
    proc.stdout.close()
    # Design bound: about 0.25 s (one flush interval) or _HIGH_WATER records.
    # Calls made at least 1.5 s before the kill must all be on disk.  Records
    # are written in order (completeness: the clean-exit test), so the last
    # call record on disk says how far the part got; read only the file's tail.
    old = max(n for when, n in seen if when <= killed_at - 1.5)
    (part,) = parts_dir.glob("*.jsonl")
    tail = part.read_bytes()[-4_000_000:].split(b"\n")[1:-1]  # whole lines only
    last_call = max(r["c"] for r in map(json.loads, tail) if r["k"] == "call")
    assert last_call >= old


def test_flusher_thread_is_invisible_to_threading(tmp_path):
    src = "import threading\nprint(threading.active_count(), [t.name for t in threading.enumerate()])\n"
    traced = run_traced(tmp_path, src)
    plain = subprocess.run(
        [sys.executable, "prog.py"], cwd=tmp_path, capture_output=True, text=True
    )
    assert traced.stdout == plain.stdout == "1 ['MainThread']\n"
    assert traced.records  # and the program was traced


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_child_exit_does_not_wait_on_an_inherited_flush_lock(tmp_path):
    # A thread holds the agent's flush lock (as the flusher does while it
    # writes) when the program forks; the child's os._exit must not hang.
    src = (
        "import os, threading, time, _tdb_trace_agent as A\n"
        "held, done = threading.Event(), threading.Event()\n"
        "def hold():\n    with A._flush_lock:\n        held.set(); done.wait()\n"
        "t = threading.Thread(target=hold); t.start(); held.wait()\n"
        "pid = os.fork()\nif pid == 0:\n    os._exit(7)\n"
        "done.set(); t.join()\ndeadline = time.monotonic() + 10\n"
        "while not os.waitpid(pid, os.WNOHANG)[0] and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if time.monotonic() >= deadline:\n    os.kill(pid, 9); os.waitpid(pid, 0); print('hung')\n"
        "else:\n    print('exited')\n"
    )
    parts = run_traced(tmp_path, src)
    assert parts.stdout.strip() == "exited"


@pytest.mark.skipif(os.name == "nt", reason="fd numbering")
def test_closed_part_descriptor_is_never_written_through_reuse(tmp_path):
    # Daemonizing code closes every descriptor; the next file the program
    # opens gets the part file's number.  The tracer must stop rather than
    # write records into the program's file.
    src = (
        "import os, _tdb_trace_agent as A\ndef f():\n    pass\nf()\n"
        "os.close(A._fd)\nfd = os.open('mine.txt', os.O_WRONLY | os.O_CREAT | os.O_TRUNC)\n"
        "print(fd == A._fd)\nfor _ in range(150_000):\n    f()\n"  # past _HIGH_WATER: a write
        "import time; time.sleep(0.6)\n"  # the flusher wakes meanwhile
        "os.write(fd, b'mine'); os.close(fd)\n"
    )
    parts = run_traced(tmp_path, src)
    assert (
        parts.stdout.strip() == "True" and parts.returncode == 0 and parts.stderr == ""
    )
    assert (tmp_path / "mine.txt").read_bytes() == b"mine"
    (marker,) = parts.markers
    assert (
        marker["marker"] == "agent-error"
        and "cannot write the part file" in marker["error"]
    )
    (recs,) = parts.records.values()
    assert recs[-1]["k"] != "exit"  # ended early, and the part says so
