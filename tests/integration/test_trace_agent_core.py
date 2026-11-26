"""The trace agent's core: ordinary functions, threads, flushing, exit, markers."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tdb.trace.spec import norm_path
from tests.integration.trace_helpers import (
    STUCK_FLUSHER,
    read_parts,
    run_traced,
    start_traced,
    target,
)

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


def _part_file(parts_dir):
    (part,) = Path(parts_dir).glob("*.jsonl")
    return part


def _call_ids(part):
    """The call ids in a part, streamed: some of these parts are large."""
    with open(part, "rb") as fh:
        for line in fh:
            if line.startswith(b'{"k":"call"') and line.endswith(b"\n"):
                yield json.loads(line)["c"]


def _last_record(part):
    with open(part, "rb") as fh:
        fh.seek(max(0, part.stat().st_size - 4096))
        return json.loads(fh.read().splitlines()[-1])


def _communicate(proc, timeout):
    """communicate(); on a timeout, kill the program and fail (a hang)."""
    try:
        return proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(f"the traced program hung for {timeout} s")


def test_clean_exit_while_the_flusher_writes_loses_nothing(tmp_path):
    # Armed after the loop, so the flusher's next os.write (which sleeps
    # 3.5 s before writing) is still going when the program exits 0.6 s
    # later, and still going when final_flush stops waiting for the flusher
    # (2 s): final_flush must then neither lose that chunk nor write it
    # twice, and "exit" must come last.
    src = (
        "import os, time, _thread, _tdb_trace_agent as A\n"
        "real_write, slow, at = os.write, [False], {}\n"
        "def write(fd, data):\n"
        "    if slow[0] and _thread.get_ident() == A._flusher_ident:\n"
        "        slow[0] = False\n        at['slow'] = time.perf_counter()\n        time.sleep(3.5)\n"
        "    return real_write(fd, data)\n"
        "A._os_write = write\n"  # the tracer's own os.write
        "def f():\n    return 1\n"
        "for _ in range(20_000):\n    f()\n"
        "slow[0] = True\n"
        "for _ in range(1_000):\n    f()\n"  # something left for the flusher
        "time.sleep(0.6)\n"
        "print(time.perf_counter() - at['slow'])\n"
    )
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    assert 0 < float(parts.stdout) < 1.0  # exit + 2 s still inside the slow write
    ids = list(_call_ids(_part_file(parts.dir)))
    assert len(ids) == len(set(ids)) == 21_000
    (recs,) = parts.records.values()
    assert len(parts.all("ret")) == 21_000
    assert recs[-1] == {"k": "exit", "t": recs[-1]["t"], "how": "atexit"}
    assert parts.returncode == 0 and parts.stderr == ""


def test_backlog_stays_bounded_in_hot_loop(tmp_path):
    src = (
        "import _tdb_trace_agent as A, time\ndef f():\n    return 1\npeak = 0\n"
        "end = time.perf_counter() + 1.0\nwhile time.perf_counter() < end:\n"
        "    for _ in range(1000):\n        f()\n    peak = max(peak, len(A._buf))\n"
        "print(peak, A._HIGH_WATER)\n"
    )
    proc, _ = start_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    peak, high_water = map(int, _communicate(proc, 60).split())
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
    part = _part_file(parts_dir)
    with open(part, "rb") as fh:
        fh.seek(max(0, part.stat().st_size - 4_000_000))
        tail = fh.read().split(b"\n")[1:-1]  # whole lines only
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


def _notes(part):
    with open(part, "rb") as fh:
        return [
            json.loads(line)["msg"] for line in fh if line.startswith(b'{"k":"note"')
        ]


def test_finalizer_in_the_flusher_cannot_deadlock_the_program(tmp_path):
    # While the flusher is stuck, the main thread makes events fast enough
    # to need backpressure: it must wait only briefly, then go on (with a
    # note), never for the stuck flusher.
    src = STUCK_FLUSHER + (
        "    for _ in range(120_000):\n        f()\n"  # past _HIGH_WATER
        "print(bool(stuck), flush=True)\n"
    )
    proc, parts_dir = start_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    assert _communicate(proc, 30).strip() == "True" and proc.returncode == 0
    part = _part_file(parts_dir)
    ids = list(_call_ids(part))
    assert len(ids) == len(set(ids)) == 125_000 and _last_record(part)["k"] == "exit"
    assert any("fell behind" in msg for msg in _notes(part))


def test_exit_while_the_flusher_is_stuck_in_a_finalizer(tmp_path):
    # The main thread calls os._exit while still holding the lock the stuck
    # flusher waits for: final_flush must stop waiting for the flusher after
    # a bounded time and write the rest itself, once.
    src = STUCK_FLUSHER + "    print(bool(stuck), flush=True)\n    os._exit(0)\n"
    proc, parts_dir = start_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    assert _communicate(proc, 30).strip() == "True" and proc.returncode == 0
    part = _part_file(parts_dir)
    ids = list(_call_ids(part))
    assert len(ids) == len(set(ids)) == 5_000
    assert _last_record(part)["how"] == "_exit"


@pytest.mark.skipif(os.name == "nt", reason="SIGINT")
def test_sigint_while_the_trace_is_written_records_no_call_twice(tmp_path):
    # Three times: a SIGINT arrives right after the tracer's next write to
    # the part file, while the program makes events fast.  A writer that
    # signal handlers can interrupt between writing a chunk and dropping it
    # from the buffer writes that chunk again later.
    src = (
        "import os, signal, _tdb_trace_agent as A\n"
        "real_write, armed = os.write, [False]\n"
        "def write(fd, data):\n"
        "    n = real_write(fd, data)\n"
        "    if armed[0] and fd == A._fd:\n"
        "        armed[0] = False\n        os.kill(os.getpid(), signal.SIGINT)\n"
        "    return n\n"
        "A._os_write = write\n"  # the tracer's own os.write
        "def f():\n    return 1\n"
        "interrupts = 0\n"
        "for attempt in range(3):\n"
        "    armed[0] = True\n"
        "    try:\n        while True:\n            f()\n"
        "    except KeyboardInterrupt:\n        interrupts += 1\n"
        "for _ in range(1_000):\n    f()\n"
        "print(interrupts)\n"
    )
    proc, parts_dir = start_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    assert _communicate(proc, 60).strip() == "3" and proc.returncode == 0
    part = _part_file(parts_dir)
    ids = list(_call_ids(part))
    assert len(ids) == len(set(ids)) and _last_record(part)["k"] == "exit"


@pytest.mark.skipif(os.name == "nt", reason="SIGINT")
@pytest.mark.parametrize("when", ["before", "after"])
def test_sigint_during_the_final_flush_loses_and_repeats_nothing(tmp_path, when):
    # During the run only the flusher writes; at exit final_flush writes the
    # rest from the main thread, where signal handlers run.  A SIGINT that
    # lands just before or just after its first os.write must neither cut
    # the trace short nor write anything twice.
    send = "os.kill(os.getpid(), signal.SIGINT)\n"
    src = (
        "import os, signal, threading, _tdb_trace_agent as A\n"
        "real_write, armed = os.write, [True]\n"
        "def write(fd, data):\n"
        "    fire = armed[0] and threading.get_ident() == threading.main_thread().ident\n"
        "    if fire:\n        armed[0] = False\n        open('fired', 'w').close()\n"
        + (f"        {send}" if when == "before" else "")
        + "    n = real_write(fd, data)\n"
        + (f"    if fire:\n        {send}" if when == "after" else "")
        + "    return n\n"
        "def f():\n    return 1\n"
        "A._os_write = write\n"  # the tracer's own os.write
        "for _ in range(30_000):\n    f()\n"  # left for final_flush
    )
    proc, parts_dir = start_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    _communicate(proc, 30)
    assert (tmp_path / "fired").exists() and proc.returncode == 0
    part = _part_file(parts_dir)
    ids = list(_call_ids(part))
    assert len(ids) == len(set(ids)) == 30_000 and _last_record(part)["k"] == "exit"


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_child_exit_does_not_wait_for_the_parents_flusher(tmp_path):
    # A forked child has none of the parent's threads; its os._exit must not
    # wait for a flusher that is not there.
    src = (
        "import os, time\nt0 = time.monotonic()\npid = os.fork()\n"
        "if pid == 0:\n    os._exit(7)\n"
        "os.waitpid(pid, 0)\nprint(time.monotonic() - t0)\n"
    )
    parts = run_traced(tmp_path, src)
    assert float(parts.stdout) < 1.0


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
