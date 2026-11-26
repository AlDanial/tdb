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


@pytest.mark.skipif(os.name == "nt", reason="death by signal")
def test_uncaught_keyboard_interrupt_still_dies_by_sigint(tmp_path):
    # Python dies by SIGINT after an uncaught KeyboardInterrupt (shells
    # stop a loop on that).  On 3.12 any exec of a string, as importing
    # traceback for the crash record does, made it exit 1 instead.
    parts = run_traced(tmp_path, "def f():\n    raise KeyboardInterrupt\nf()\n")
    assert parts.returncode == -signal.SIGINT
    assert [c["exc"] for c in parts.all("crash")] == ["KeyboardInterrupt"]
    # ... but only when the interpreter reports it, not the program.
    src = (
        "import sys\ntry:\n    raise KeyboardInterrupt\n"
        "except KeyboardInterrupt:\n    sys.excepthook(*sys.exc_info())\n"
    )
    assert run_traced(tmp_path, src).returncode == 0


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
    plain = subprocess.run(
        [sys.executable, "prog.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (parts.returncode, parts.stderr) == (plain.returncode, plain.stderr)
    assert [c["exc"] for c in parts.all("crash")] == [
        "RuntimeError: t",
        "ValueError: main",
    ]
