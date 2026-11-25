"""The trace agent's core: ordinary functions, threads, flushing, exit, markers."""

from __future__ import annotations

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
