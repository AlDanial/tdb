"""The trace agent in child processes: fork, multiprocessing, subprocess.

Every test that starts children runs the program with ``own_group=True``,
so no child outlives the test, even when an assertion fails.
"""

from __future__ import annotations

import os
import re
import shutil
import sys

import pytest

from tdb.trace.contract import CHILD_FLUSH_WAIT
from tests.integration.trace_helpers import STUCK_FLUSHER, run_traced, target

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")

WORK = "def work(x):\n    return x * x\n"


@pytest.mark.parametrize(
    "method", ["spawn"] + ([] if os.name == "nt" else ["forkserver", "fork"])
)
def test_pool_workers_traced(tmp_path, method):
    # close()+join() so workers exit normally; the `with` block's terminate() would SIGTERM them
    src = WORK + (
        f"if __name__ == '__main__':\n    import multiprocessing as mp\n"
        f"    with mp.get_context('{method}').Pool(2) as p:\n        print(p.map(work, range(4)))\n"
        f"        p.close(); p.join()\n"
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", "work")], own_group=True
    )
    assert parts.stdout.strip() == "[0, 1, 4, 9]" and len(parts.calls("work")) == 4
    worker_names = {r["name"] for r in parts.all("proc")} | {
        r["proc"] for r in parts.all("hdr")
    }
    prefix = {
        "spawn": "SpawnPoolWorker",
        "forkserver": "ForkServerPoolWorker",
        "fork": "ForkPoolWorker",
    }[method]
    assert any(n.startswith(prefix) for n in worker_names)


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_child_traced_and_parent_events_not_duplicated(tmp_path):
    src = (
        "import os\ndef f(tag):\n    pass\nf('parent')\npid = os.fork()\n"
        "if pid == 0:\n    f('child'); os._exit(0)\nos.waitpid(pid, 0)\n"
    )
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "prog.py", {"pattern": "f", "capture": ["tag"]})],
        own_group=True,
    )
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    child = next(s for s, h in hdrs.items() if h["ppid"] == hdrs[main]["pid"])
    assert [c["v"]["tag"] for c in parts.calls("f") if c["_stem"] == child] == [
        "'child'"
    ]
    assert parts.records[child][-1]["how"] == "_exit"
    # And the parent's part holds the parent's events only, once.
    assert len(parts.records) == 2 and child == str(hdrs[child]["pid"])
    assert [c["v"]["tag"] for c in parts.calls("f") if c["_stem"] == main] == [
        "'parent'"
    ]
    assert [r["how"] for r in parts.records[main] if r["k"] == "exit"] == ["atexit"]


def test_subprocess_python_child(tmp_path):
    files = {"child.py": "def in_child():\n    pass\nin_child()\n"}
    src = "import subprocess, sys\nsubprocess.run([sys.executable, 'child.py'], check=True)\n"
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "child.py", "in_child")],
        files=files,
        own_group=True,
    )
    assert len(parts.calls("in_child")) == 1 and len(parts.records) == 2


def test_old_python_child_marker(tmp_path):
    py311 = shutil.which("python3.11") or pytest.skip("no python3.11")
    src = f"import subprocess\nsubprocess.run([{py311!r}, '-c', 'pass'], check=True)\n"
    parts = run_traced(tmp_path, src, own_group=True)
    assert [m["reason"] for m in parts.markers] == ["python 3.11 is older than 3.12"]


def _shape(parts, stem):
    """A part's records after the header, without times, threads and pids."""
    recs = parts.records[stem]
    quals = {r["id"]: r["qual"] for r in recs if r["k"] == "fn"}
    out = []
    for r in recs[1:]:
        if r["k"] == "call":
            out.append(("call", r["c"], quals[r["fn"]], r["task"], r.get("v")))
        elif r["k"] in ("ret", "susp", "res", "raise"):
            out.append((r["k"], r["c"], r["task"]))
        elif r["k"] in ("fn", "th"):
            out.append((r["k"], r.get("qual", r.get("name"))))
        elif r["k"] in ("task", "note"):
            out.append((r["k"], r.get("id", r.get("msg"))))
        else:
            out.append((r["k"],))
    return out


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_child_part_starts_afresh(tmp_path):
    # Everything kept per process starts again in the child: call and task
    # ids, the fn/th/task records, max_hits and the event cap.  So the
    # child, running what the parent ran, writes the same records.
    src = (
        "import asyncio, os\n"
        "def f(tag):\n    pass\n"
        "def g():\n    pass\n"
        "async def t():\n    g()\n"
        # 8 events: f 'a' (2; 'b' is past max_hits), t and g in a task
        # (4), then g (2); the last g is past the cap.
        "def run():\n    f('a'); f('b'); asyncio.run(t()); g(); g()\n"
        "run()\npid = os.fork()\n"
        "if pid == 0:\n    run(); os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
    )
    entries = [{"pattern": "f", "capture": ["tag"], "max_hits": 1}, "g", "t"]
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "prog.py", *entries)],
        max_events=8,
        own_group=True,
    )
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    (child,) = set(hdrs) - {main}
    assert hdrs[child]["ppid"] == hdrs[main]["pid"]
    shape = _shape(parts, child)
    assert shape == _shape(parts, main)
    assert [s for s in shape if s[0] == "call"] == [
        ("call", 1, "f", None, {"tag": "'a'"}),
        ("call", 2, "t", 1, None),
        ("call", 3, "g", 1, None),
        ("call", 4, "g", None, None),
    ]
    assert {s for s in shape if s[0] in ("fn", "th", "task")} == {
        ("fn", "f"),
        ("fn", "g"),
        ("fn", "t"),
        ("th", "MainThread"),
        ("task", 1),
    }
    assert ("note", "1 calls not recorded (event cap)") in shape
    assert [parts.records[s][-1]["how"] for s in (main, child)] == ["atexit", "_exit"]


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_child_never_writes_what_it_inherited(tmp_path):
    # The parent's flusher is in a slow write when the fork stops waiting
    # for it, so the child starts with all the parent's records unwritten
    # in its copy of the buffer.  Only the parent may write them, and the
    # child may not write to the parent's part.
    src = (
        "import os, time, _tdb_trace_agent as A\n"
        "def busy():\n    pass\n"
        "def mine():\n    pass\n"
        "real_write, parent, slow = A._os_write, os.getpid(), [True]\n"
        "def write(fd, data):\n"
        "    if slow[0] and os.getpid() == parent:\n"
        "        slow[0] = False\n        time.sleep(1.0)\n"  # longer than a fork waits
        "    return real_write(fd, data)\n"
        "A._os_write = write\n"
        "for _ in range(1_000):\n    busy()\n"
        "t0 = time.monotonic()\npid = os.fork()\n"
        "if pid == 0:\n    mine(); os._exit(0)\n"
        "print(time.monotonic() - t0 < 1.0)\n"  # the fork went ahead mid-write
        "os.waitpid(pid, 0)\n"
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", "busy", "mine")], own_group=True
    )
    assert parts.stdout == "True\n" and parts.returncode == 0, parts.stderr
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    (child,) = set(hdrs) - {main}
    recs = parts.records[child]
    assert [parts.qual_of(child, r["fn"]) for r in recs if r["k"] == "call"] == ["mine"]
    assert {r["c"] for r in recs if "c" in r} == {1}
    assert recs[-1] == {"k": "exit", "t": recs[-1]["t"], "how": "_exit"}
    recs = parts.records[main]
    calls = [r for r in recs if r["k"] == "call"]
    assert {parts.qual_of(main, r["fn"]) for r in calls} == {"busy"}
    ids = [r["c"] for r in calls]
    assert len(ids) == len(set(ids)) == 1_000
    assert [r["how"] for r in recs if r["k"] == "exit"] == ["atexit"]


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_call_that_forks_ends_in_the_parent_only(tmp_path):
    # The child returns from the call it was in at the fork; that call
    # started in the parent, so only the parent's part has it.
    src = (
        "import os\ndef forker():\n    return os.fork()\n"
        "pid = forker()\nif pid == 0:\n    os._exit(0)\nos.waitpid(pid, 0)\n"
    )
    parts = run_traced(tmp_path, src, own_group=True)
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    (child,) = set(hdrs) - {main}
    assert [r["k"] for r in parts.records[child]] == ["hdr", "exit"]
    assert [r["k"] for r in parts.records[main] if "c" in r] == ["call", "ret"]


@pytest.mark.skipif(os.name == "nt", reason="fork, SIGKILL")
def test_flusher_runs_again_after_fork_in_parent_and_child(tmp_path):
    # Both processes die by SIGKILL, skipping the final flush: only a
    # flusher running after the fork can have written their calls.
    src = (
        "import os, signal, time\ndef f(tag):\n    pass\n"
        "pid = os.fork()\n"
        "f('parent' if pid else 'child')\ntime.sleep(0.6)\n"
        "if pid:\n    os.waitpid(pid, 0)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "prog.py", {"pattern": "f", "capture": ["tag"]})],
        own_group=True,
    )
    assert parts.returncode == -9
    tags = {c["_stem"]: c["v"]["tag"] for c in parts.calls("f")}
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    assert sorted(tags.values()) == ["'child'", "'parent'"] and len(tags) == 2
    for stem, tag in tags.items():
        assert (hdrs[stem]["ppid"] == os.getpid()) == (tag == "'parent'")
    assert parts.all("exit") == []


FORKS = (
    "for _ in range(20):\n"
    "    pid = os.fork()\n"
    "    if pid == 0:\n        os._exit(0)\n"
    "    os.waitpid(pid, 0)\n"
)


def _untraced(tmp_path, src):
    """The same program on the same interpreter, the tracer not started."""
    return run_traced(tmp_path, src, extra_env={"TDB_TRACE_CONFIG": ""}, own_group=True)


def _no_pids(text):
    return re.sub(r"pid=\d+", "pid=N", text)


@pytest.mark.skipif(os.name == "nt", reason="fork")
@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="macOS: Python counts Mach threads, and the tracer can only wait for "
    "its flusher's thread to leave on Linux (/proc), so the warning may show "
    "(a documented limitation)",
)
def test_fork_from_main_shows_no_threads_warning(tmp_path):
    # Python 3.12+ warns (a DeprecationWarning, shown for __main__) when
    # os.fork() runs while the process has other threads.  The flusher must
    # not be one at that moment.
    src = "import os\n" + FORKS
    traced = run_traced(tmp_path, src, own_group=True)
    plain = _untraced(tmp_path, src)
    assert plain.records == {} and len(traced.records) == 21
    assert traced.returncode == plain.returncode == 0
    assert traced.stderr == plain.stderr == ""
    # The check is live: a thread of the program's own is warned about.
    src = (
        "import os, threading, time\n"
        "threading.Thread(target=time.sleep, args=(30,), daemon=True).start()\n"
    ) + FORKS
    traced = run_traced(tmp_path, src, own_group=True)
    plain = _untraced(tmp_path, src)
    assert "is multi-threaded, use of fork()" in plain.stderr
    assert _no_pids(traced.stderr) == _no_pids(plain.stderr)


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_while_the_flusher_is_stuck(tmp_path):
    # The flusher waits, in a finalizer, for a lock the forking thread
    # holds.  The first fork goes ahead after a short wait, without stopping
    # it; later ones do not wait while it has not moved.  It carries on
    # afterwards (only it can write the calls made before the SIGKILL), and
    # each child starts its own flusher and part.
    src = STUCK_FLUSHER + (
        "    waits = []\n"
        "    for _ in range(3):\n"
        "        t0 = time.monotonic()\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n            f(); os._exit(0)\n"
        "        waits.append(time.monotonic() - t0)\n"
        "        os.waitpid(pid, 0)\n"
        "    print(bool(stuck), waits[0] > 0.45, sum(waits[1:]) < 0.2, flush=True)\n"
        "for _ in range(1_000):\n    f()\n"
        "time.sleep(0.6)\nos.kill(os.getpid(), 9)\n"
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", "f")], own_group=True
    )
    assert parts.stdout == "True True True\n" and parts.returncode == -9
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    children = set(hdrs) - {main}
    ids = [r["c"] for r in parts.records[main] if r["k"] == "call"]
    assert len(ids) == len(set(ids)) == 6_000 and len(children) == 3
    for child in children:
        recs = parts.records[child]
        assert [r["k"] for r in recs] == ["hdr", "th", "fn", "call", "ret", "exit"]
        assert recs[-1]["how"] == "_exit"


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_fork_child_with_a_stuck_flusher_exits_within_tdbs_wait(tmp_path):
    # tdb waits CHILD_FLUSH_WAIT for children once the program has exited.
    # A forked child whose flusher is stuck (in a finalizer, waiting for a
    # lock the child holds as it exits) must be done by then, its events
    # written.
    src = (
        (
            "import os, time\n"
            "pid = os.fork()\n"
            "if pid:\n"
            "    os.waitpid(pid, 0)\n    print('done', time.monotonic(), flush=True)\n"
            "    raise SystemExit\n"
        )
        + STUCK_FLUSHER
        + (
            "    print('stuck', bool(stuck), flush=True)\n"
            "    print('exit', time.monotonic(), flush=True)\n"
            "    os._exit(0)\n"
        )
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", "f")], own_group=True
    )
    out = dict(line.split() for line in parts.stdout.splitlines())
    assert out["stuck"] == "True" and parts.returncode == 0, parts.stderr
    assert float(out["done"]) - float(out["exit"]) < CHILD_FLUSH_WAIT
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    (child,) = [s for s, h in hdrs.items() if h["ppid"] != os.getpid()]
    recs = parts.records[child]
    ids = [r["c"] for r in recs if r["k"] == "call"]
    assert len(ids) == len(set(ids)) == 5_000 and recs[-1]["how"] == "_exit"


# A KeyboardInterrupt raised once, in the parent, at one point of the
# at-fork hooks, as a signal handler's exception can be (Python prints and
# drops it).  One fork only, so no later fork can repair anything: after it
# there is exactly one flusher, and every call is written once.  The writes
# are slow, so two flushers would overlap.
INTERRUPTED_FORK = (
    "import os, signal, time, _tdb_trace_agent as A\n"
    "def f():\n    pass\n"
    "real_write, parent, armed = A._os_write, os.getpid(), [1]\n"
    "def write(fd, data):\n    time.sleep(0.01)\n    return real_write(fd, data)\n"
    "A._os_write = write\n"
    "{window}"
    "def fork():\n"
    "    pid = os.fork()\n"
    "    if pid == 0:\n        os._exit(0)\n"
    "    os.waitpid(pid, 0)\n"
    "for _ in range(20_000):\n    f()\n"
    "fork()\n"  # interrupted
    "for _ in range(20_000):\n    f()\n"
    "time.sleep(0.8)\n"  # only a running flusher can write these before the SIGKILL
    "tasks = '/proc/self/task'\n"
    "print(len(os.listdir(tasks)) if os.path.isdir(tasks) else 2, armed, flush=True)\n"
    "os.kill(os.getpid(), signal.SIGKILL)\n"
)
RAISE_IN_PARENT = "    if armed and os.getpid() == parent:\n        armed.pop(); raise KeyboardInterrupt\n"
PARENT_WINDOWS = {
    # right after the fork asked the flusher to stop
    "request": "class Requests(list):\n    def append(self, item):\n"
    "        list.append(self, item)\n"
    "        if armed and os.getpid() == parent:\n"
    "            armed.pop(); raise KeyboardInterrupt\n"
    "A._halt = Requests()\n",
    # while the fork waits for the flusher to end
    "waiting": "wake = A._wake_flusher\ndef wake_then_raise():\n    wake()\n"
    + RAISE_IN_PARENT
    + "A._wake_flusher = wake_then_raise\n",
    # once the flusher has ended, before the fork
    "ended": "def gone(tid):\n" + RAISE_IN_PARENT + "A._wait_gone = gone\n",
    # after the new flusher's thread is started, before the start returns
    "started": "start = A._start_new_thread\ndef start_then_raise(fn, args):\n"
    "    ident = start(fn, args)\n" + RAISE_IN_PARENT + "    return ident\n"
    "A._start_new_thread = start_then_raise\n",
    # before both tries of the first after-fork hook: the second one starts it
    "not started": "start = A._start_new_thread\narmed.append(1)\n"
    "def raise_then_start(fn, args):\n"
    + RAISE_IN_PARENT
    + "    return start(fn, args)\n"
    "A._start_new_thread = raise_then_start\n",
    # in the first after-fork hook before its try, as at its very start (see
    # the next test for a real signal there): the second one starts it
    "entry": "restart = A._restart_flusher\ndef raise_then_restart():\n"
    + RAISE_IN_PARENT
    + "    return restart()\nA._restart_flusher = raise_then_restart\n",
}


@pytest.mark.skipif(os.name == "nt", reason="fork, SIGKILL")
@pytest.mark.parametrize("window", list(PARENT_WINDOWS))
def test_interrupted_fork_hook_leaves_one_flusher(tmp_path, window):
    src = INTERRUPTED_FORK.replace("{window}", PARENT_WINDOWS[window])
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", "f")], own_group=True
    )
    assert parts.stdout.split() == ["2", "[]"], parts.stderr  # main + 1 flusher
    assert "KeyboardInterrupt" in parts.stderr and parts.returncode == -9
    assert all(recs[0]["k"] == "hdr" for recs in parts.records.values())
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    ids = [r["c"] for r in parts.records[main] if r["k"] == "call"]
    assert len(ids) == len(set(ids)) == 40_000


# The tracer started by the program itself (sitecustomize does not: the
# config variable is empty), after the program has registered at-fork hooks
# of its own, which then run before the tracer's.
STARTED_LATE = (
    "import json, os, sys\n"
    "sys.path.insert(0, os.environ['PYTHONPATH'].split(os.pathsep)[0])\n"
    "import _tdb_trace_agent as A\n"
    "with open(os.path.join(os.environ['TDB_TRACE_PARTS'], 'config.json')) as fh:\n"
    "    A.start(json.load(fh))\n"
)


def _run_started_late(tmp_path, hooks, rest):
    src = "import functools, os, signal, sys, time\n" + hooks + STARTED_LATE + rest
    return run_traced(
        tmp_path,
        src,
        [target(tmp_path / "prog.py", "f")],
        extra_env={"TDB_TRACE_CONFIG": ""},
        own_group=True,
    )


@pytest.mark.skipif(os.name == "nt", reason="fork, SIGALRM, SIGKILL")
def test_signal_pending_as_the_fork_returns_leaves_a_flusher(tmp_path):
    # A signal that arrives during the fork() call is handled at the first
    # Python code after it: the start of the tracer's first after-fork hook
    # in the parent, before any try.  C-level hooks that run before it make
    # one pending there (a timer, then a wait in C, with no signal check),
    # and its handler raises.  One fork only, then the SIGKILL.
    hooks = (
        "parent, armed = os.getpid(), [1]\n"
        "def handler(signum, frame):\n"
        "    if armed and os.getpid() == parent:\n"
        "        armed.pop(); raise KeyboardInterrupt\n"
        "signal.signal(signal.SIGALRM, handler)\n"
        "os.register_at_fork(after_in_parent=functools.partial(\n"
        "    signal.setitimer, signal.ITIMER_REAL, 0.001))\n"
        "os.register_at_fork(after_in_parent=functools.partial(sum, range(10_000_000)))\n"
    )
    rest = (
        "def f():\n    pass\n"
        "for _ in range(20_000):\n    f()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
        "for _ in range(20_000):\n    f()\n"
        "time.sleep(0.8)\n"  # only a running flusher can write these
        "tasks = '/proc/self/task'\n"
        "print(len(os.listdir(tasks)) if os.path.isdir(tasks) else 2, armed, flush=True)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    parts = _run_started_late(tmp_path, hooks, rest)
    assert parts.stdout.split() == ["2", "[]"], parts.stderr  # main + 1 flusher
    assert "KeyboardInterrupt" in parts.stderr and parts.returncode == -9
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    ids = [r["c"] for r in parts.records[main] if r["k"] == "call"]
    assert len(ids) == len(set(ids)) == 40_000


@pytest.mark.skipif(os.name == "nt", reason="fork")
def test_child_exiting_before_its_part_is_set_up_writes_nothing(tmp_path):
    # In the child, other at-fork hooks (threading's, for one) run before the
    # tracer's, while it still holds the parent's part and buffer.  A child
    # that exits there (here: a hook of the program's calls os._exit, as a
    # signal handler could) must write nothing into the parent's part.
    hooks = (
        "def exit_in_child():\n    os._exit(0)\n"
        "os.register_at_fork(after_in_child=exit_in_child)\n"
    )
    rest = (
        "def f():\n    pass\n"
        "for _ in range(1_000):\n    f()\n"
        "pid = os.fork()\n"
        "print(os.waitpid(pid, 0)[1], flush=True)\n"
        "f()\n"
    )
    parts = _run_started_late(tmp_path, hooks, rest)
    assert parts.stdout == "0\n" and parts.returncode == 0, parts.stderr
    (recs,) = parts.records.values()  # the child has no part
    assert recs[0]["k"] == "hdr" and recs[0]["ppid"] == os.getpid()
    ids = [r["c"] for r in recs if r["k"] == "call"]
    assert len(ids) == len(set(ids)) == 1_001
    assert [r["how"] for r in recs if r["k"] == "exit"] == ["atexit"]


# The same, in the child: it never writes to the parent's part, and a part
# it has starts with its header.
INTERRUPTED_CHILD = (
    "import os, _tdb_trace_agent as A\n"
    "def f():\n    pass\n"
    "parent, armed = os.getpid(), [1]\n"
    "{window}"
    "f()\npid = os.fork()\n"
    "if pid == 0:\n"
    "    f(); f(); print('armed', len(armed), flush=True); os._exit(0)\n"
    "os.waitpid(pid, 0)\nf()\nprint('child', pid)\n"
)
RAISE_IN_CHILD = "    if armed and os.getpid() != parent:\n        armed.pop(); raise KeyboardInterrupt\n"
CHILD_WINDOWS = {
    # making its header: it gets no part
    "header": "header = A._header\ndef raising_header():\n"
    + RAISE_IN_CHILD
    + "    return header()\nA._header = raising_header\n",
    # right after its header is written: no part either
    "written": "write = A._os_write\ndef write_then_raise(fd, data):\n"
    "    n = write(fd, data)\n" + RAISE_IN_CHILD + "    return n\n"
    "A._os_write = write_then_raise\n",
    # before both tries to start its flusher: final_flush writes it all
    "flusher": "start = A._start_new_thread\narmed.append(1)\n"
    "def raise_then_start(fn, args):\n"
    + RAISE_IN_CHILD
    + "    return start(fn, args)\n"
    "A._start_new_thread = raise_then_start\n",
}


@pytest.mark.skipif(os.name == "nt", reason="fork")
@pytest.mark.parametrize("window", list(CHILD_WINDOWS))
def test_interrupted_fork_hook_in_the_child(tmp_path, window):
    src = INTERRUPTED_CHILD.replace("{window}", CHILD_WINDOWS[window])
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", "f")], own_group=True
    )
    out = dict(line.split() for line in parts.stdout.splitlines())
    assert out["armed"] == "0" and "KeyboardInterrupt" in parts.stderr, parts.stderr
    assert parts.returncode == 0
    assert all(recs[0]["k"] == "hdr" for recs in parts.records.values())
    hdrs = {stem: recs[0] for stem, recs in parts.records.items()}
    main = next(s for s, h in hdrs.items() if h["ppid"] == os.getpid())
    assert [r["k"] for r in parts.records[main] if "c" in r] == ["call", "ret"] * 2
    assert [r["how"] for r in parts.records[main] if r["k"] == "exit"] == ["atexit"]
    child = [s for s, h in hdrs.items() if h["pid"] == int(out["child"])]
    if window == "flusher":  # traced, without a flusher
        (child,) = child
        kinds = [r["k"] for r in parts.records[child] if "c" in r or r["k"] == "exit"]
        assert kinds == ["call", "ret", "call", "ret", "exit"]
    else:  # untraced: no part at all, so none without its header
        assert child == [] and len(parts.records) == 1


@pytest.mark.skipif(os.name == "nt", reason="preexec_fn")
def test_subprocess_preexec_fn_child_has_no_part_of_its_own(tmp_path):
    # With a preexec_fn, subprocess forks with Python's at-fork hooks and
    # runs it, then executes the new program in that process: the program
    # is the one process there, with one part.
    files = {"child.py": "def in_child():\n    pass\nin_child()\n"}
    src = (
        "import os, subprocess, sys\n"
        "subprocess.run([sys.executable, 'child.py'], check=True, preexec_fn=os.getpid)\n"
    )
    parts = run_traced(
        tmp_path,
        src,
        [target(tmp_path / "child.py", "in_child")],
        files=files,
        own_group=True,
    )
    assert len(parts.calls("in_child")) == 1 and len(parts.records) == 2
    assert all(stem.isdigit() for stem in parts.records)  # no "<pid>-2" part


@pytest.mark.skipif(os.name == "nt", reason="SIGUSR1")
def test_os_exit_exits_when_a_signal_handler_raises_after_the_flush(tmp_path):
    # The program's handler raises at the first chance after the final
    # flush returns (here: forced, by a final_flush that ends with the
    # signal); os._exit must still exit.
    src = (
        "import os, signal, _tdb_trace_agent as A\n"
        "def f():\n    pass\n"
        "def handler(signum, frame):\n    raise TimeoutError('late')\n"
        "signal.signal(signal.SIGUSR1, handler)\n"
        "flush = A.final_flush\n"
        "def flush_then_signal(how):\n"
        "    flush(how)\n    signal.raise_signal(signal.SIGUSR1)\n"
        "A.final_flush = flush_then_signal\n"
        "f()\n"
        "try:\n    os._exit(3)\nexcept TimeoutError:\n    print('os._exit returned')\n"
    )
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "f")])
    assert parts.returncode == 3 and parts.stdout == ""
    (recs,) = parts.records.values()
    assert recs[-1]["k"] == "exit" and recs[-1]["how"] == "_exit"


def test_tracer_never_calls_functions_the_program_replaced(tmp_path):
    # As gevent's monkey-patching and mock.patch do: the program replaces
    # the os, time and _thread functions the tracer writes, sleeps and
    # starts threads with.  The tracer keeps using the real ones.
    fork = os.name != "nt"
    src = (
        "import _thread, os, time, _tdb_trace_agent as A\n"
        "def f():\n    pass\n"
        "used, sleep = [], time.sleep\n"
        "def spy(mod, name):\n"
        "    real = getattr(mod, name)\n"
        "    def wrapper(*args, **kwargs):\n"
        "        used.append(name)\n"
        "        return real(*args, **kwargs)\n"
        "    setattr(mod, name, wrapper)\n"
        "for mod, name in [(os, 'write'), (os, 'fstat'), (os, 'stat'), (os, 'open'),\n"
        "                  (os, 'close'), (os, 'getpid'), (time, 'sleep'),\n"
        "                  (_thread, 'allocate_lock'), (_thread, 'start_new_thread')]:\n"
        "    spy(mod, name)\n"
        "A._HIGH_WATER, A._LOW_WATER = 200, 100\n"  # backpressure: it sleeps
        "for _ in range(2_000):\n    f()\n"
        "sleep(0.6)\n"  # the flusher writes meanwhile
        + (
            "pid = os.fork()\n"  # stops, restarts the flusher; a new part
            "if pid == 0:\n    f(); sleep(0.6); print('child', used, flush=True); os._exit(0)\n"
            "os.waitpid(pid, 0)\n"
            if fork
            else ""
        )
        + "print('parent', used)\n"
    )
    parts = run_traced(
        tmp_path, src, [target(tmp_path / "prog.py", "f")], own_group=True
    )
    assert parts.returncode == 0, parts.stderr
    expected = ["child []", "parent []"] if fork else ["parent []"]
    assert parts.stdout.splitlines() == expected
    assert len(parts.calls("f")) == 2_000 + fork
    assert len(parts.records) == 1 + fork
