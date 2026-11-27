"""The trace agent: coroutines, tasks, generators, exception exits, crashes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from tests.integration.trace_helpers import run_traced, target

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")

CHAIN = (
    "import asyncio\nasync def inner():\n    await asyncio.sleep(0.01)\n    return 2\n"
    "async def outer():\n    return await inner()\n"
)


def kinds(parts, qual):
    (c,) = parts.calls(qual)
    return [r["k"] for r in parts.events_for(c["_stem"], c["c"])]


def test_coroutine_suspend_resume_in_task(tmp_path):
    parts = run_traced(tmp_path, CHAIN + "asyncio.run(outer())\n")
    assert kinds(parts, "inner") == ["call", "susp", "res", "ret"]
    assert kinds(parts, "outer") == ["call", "susp", "res", "ret"]
    (c,) = parts.calls("outer")
    assert c["task"] is not None
    assert {t["id"]: t["name"] for t in parts.all("task")}[c["task"]] == "Task-1"


def test_task_names_and_unique_ids(tmp_path):
    src = CHAIN + (
        "async def main():\n    for i in range(50):\n"
        "        await asyncio.create_task(outer(), name=f'fetch-{i}')\nasyncio.run(main())\n"
    )
    parts = run_traced(tmp_path, src)
    ids = {c["task"] for c in parts.calls("outer")}
    names = {t["id"]: t["name"] for t in parts.all("task")}
    assert len(ids) == 50 and {names[i] for i in ids} == {
        f"fetch-{i}" for i in range(50)
    }


def test_generator_runs(tmp_path):
    src = "def gen(n):\n    for i in range(n):\n        yield i\nlist(zip(gen(3), gen(3)))\n"
    parts = run_traced(tmp_path, src)
    assert len(parts.calls("gen")) == 2
    for c in parts.calls("gen"):
        ks = [r["k"] for r in parts.events_for(c["_stem"], c["c"])]
        assert (
            ks[0] == "call"
            and ks.count("susp") == ks.count("res")
            and ks[-1] in ("ret", "raise")
        )


def test_exception_exit_records_raise(tmp_path):
    parts = run_traced(
        tmp_path,
        "def boom():\n    raise ValueError('bad')\ntry:\n    boom()\nexcept ValueError:\n    pass\n",
    )
    assert kinds(parts, "boom") == ["call", "raise"]
    assert parts.all("raise")[0]["exc"] == "ValueError: bad"


def test_generator_close_records_resume_then_raise(tmp_path):
    parts = run_traced(
        tmp_path,
        "def gen():\n    yield 1\n    yield 2\ng = gen(); next(g); g.close()\n",
    )
    assert kinds(parts, "gen") == ["call", "susp", "res", "raise"]
    assert parts.all("raise")[0]["exc"] == "GeneratorExit"


def test_cancelled_coroutine(tmp_path):  # Review Focus 5
    src = CHAIN + (
        "async def main():\n    t = asyncio.create_task(outer())\n    await asyncio.sleep(0.001)\n"
        "    t.cancel()\n    try:\n        await t\n    except asyncio.CancelledError:\n        pass\n"
        "    try:\n        await asyncio.wait_for(outer(), 0.001)\n    except TimeoutError:\n        pass\n"
        "asyncio.run(main())\n"
    )
    parts = run_traced(tmp_path, src, [target(tmp_path / "prog.py", "inner", "outer")])
    # One of each per way of cancelling (task.cancel(), wait_for's timeout).
    assert len(parts.calls("inner")) == len(parts.calls("outer")) == 2
    for qual in ("inner", "outer"):
        for c in parts.calls(qual):
            evs = parts.events_for(c["_stem"], c["c"])
            assert [r["k"] for r in evs][-2:] == ["res", "raise"] and evs[-1][
                "exc"
            ].startswith("CancelledError")


def test_uncaught_exception_writes_crash(tmp_path):
    parts = run_traced(tmp_path, "x = 1\nraise KeyError('sku')\n")
    (cr,) = parts.all("crash")
    assert (
        cr["exc"] == "KeyError: 'sku'"
        and cr["where"].endswith("prog.py:2")
        and "Traceback" in cr["tb"]
    )
    assert parts.returncode == 1 and "KeyError" in parts.stderr


def test_thread_crash_recorded(tmp_path):
    src = (
        "import threading\ndef bad():\n    raise RuntimeError('t')\n"
        "t = threading.Thread(target=bad, name='w')\nt.start(); t.join()\n"
    )
    parts = run_traced(tmp_path, src)
    (cr,) = parts.all("crash")
    assert {r["tid"]: r["name"] for r in parts.all("th")}[cr["tid"]] == "w"
    assert "Exception in thread w" in parts.stderr


def _plain_run(tmp_path):
    """Run tmp_path/prog.py again, untraced."""
    return subprocess.run(
        [sys.executable, "prog.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_keyboard_interrupt_crash(parts):
    (cr,) = parts.all("crash")
    assert cr["exc"] == "KeyboardInterrupt" and cr["where"].endswith("prog.py:2")
    lines = cr["tb"].split("\n")
    assert lines[0] == "Traceback (most recent call last):"
    assert lines[-1] == "KeyboardInterrupt"
    assert any(ln.endswith('prog.py", line 2, in f') for ln in lines)


KEYBOARD_INTERRUPT = "def f():\n    raise KeyboardInterrupt\nf()\n"


@pytest.mark.skipif(os.name == "nt", reason="death by signal")
def test_uncaught_keyboard_interrupt_still_dies_by_sigint(tmp_path):
    # Python dies by SIGINT after an uncaught KeyboardInterrupt (shells
    # stop a loop on that).  On 3.12 any exec of a string, as importing
    # traceback for the crash record did, made it exit 1 instead.
    parts = run_traced(tmp_path, KEYBOARD_INTERRUPT)
    assert parts.returncode == -signal.SIGINT
    _assert_keyboard_interrupt_crash(parts)
    # A program reporting one itself is not dying.
    src = (
        "import sys\ntry:\n    raise KeyboardInterrupt\n"
        "except KeyboardInterrupt:\n    sys.excepthook(*sys.exc_info())\n"
    )
    assert run_traced(tmp_path, src).returncode == 0


@pytest.mark.skipif(os.name == "nt", reason="death by signal")
def test_keyboard_interrupt_through_a_chaining_program_hook(tmp_path):
    # The program's own hook calls ours (its previous hook), so our frame
    # is not the interpreter's direct callee: still death by SIGINT.
    src = (
        "def f():\n    raise KeyboardInterrupt\nimport sys\nprev = sys.excepthook\n"
        "def h(t, v, tb):\n    sys.stderr.write('hook\\n')\n    prev(t, v, tb)\n"
        "sys.excepthook = h\nf()\n"
    )
    parts = run_traced(tmp_path, src)
    plain = _plain_run(tmp_path)
    assert parts.returncode == plain.returncode == -signal.SIGINT
    assert parts.stderr == plain.stderr and parts.stderr.startswith("hook\n")
    _assert_keyboard_interrupt_crash(parts)


def test_crash_recorded_under_a_non_chaining_distro_hook(tmp_path):
    # Debian/Ubuntu's sitecustomize (apport) sets sys.excepthook without
    # calling the one before it.  It runs after the tracer starts, so the
    # tracer's hooks must be installed after it, around it.
    fake = (
        "import sys\ndef _hook(t, v, tb):\n"
        "    sys.stderr.write('distro hook: %s\\n' % t.__name__)\n"
        "sys.excepthook = _hook\n"
    )
    parts = run_traced(
        tmp_path,
        "def f():\n    raise ValueError('x')\nf()\n",
        files={"sitecustomize.py": fake},
        extra_env={"PYTHONPATH": str(tmp_path)},
    )
    assert [c["exc"] for c in parts.all("crash")] == ["ValueError: x"]
    assert parts.stderr == "distro hook: ValueError\n" and parts.returncode == 1


def test_crash_output_and_status_unchanged(tmp_path):
    # The hooks only add records: what the program prints and its exit
    # status are as without the tracer (SystemExit in a thread is silent).
    src = (
        "import sys, threading\ndef bad():\n    raise RuntimeError('t')\n"
        "def leave():\n    sys.exit(3)\n"
        "for f in (bad, leave):\n"
        "    t = threading.Thread(target=f, name=f.__name__)\n    t.start(); t.join()\n"
        "def main():\n    raise ValueError('main')\nmain()\n"
    )
    parts = run_traced(tmp_path, src)
    plain = _plain_run(tmp_path)
    assert (parts.returncode, parts.stderr) == (plain.returncode, plain.stderr)
    assert [c["exc"] for c in parts.all("crash")] == [
        "RuntimeError: t",
        "ValueError: main",
    ]
