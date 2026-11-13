"""`q` then `d` in the TUI: the Python program keeps running under tdb's
relay (output, stdin, exit code, Ctrl-C). Drives tdb on a pty.

If these prove flaky under `-n auto`, run with `--dist loadgroup`.
"""

from __future__ import annotations

import sys

import pytest

from . import detach_harness
from .conftest import FIXTURES_DIR
from .detach_harness import run_detached

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="needs a pty"),
    pytest.mark.xdist_group("pty"),
]

PROG = str(FIXTURES_DIR / "detach_prog.py")


@pytest.fixture
def env(tmp_path):
    return {"cwd": tmp_path, "env": {"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)}}


def test_python_detach_output_stdin_exit_code(env):
    out, code = run_detached([PROG], stdin_lines=["hello"], **env)
    assert "tdb: detached" in out
    assert "after-ran" in out
    assert "got:hello" in out
    assert code == 7
    assert detach_harness.leftovers == []


def test_python_detach_ctrl_c_kills_looping_program(env):
    out, code = run_detached([PROG, "loop"], interrupt_after=1.0, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


def test_python_detach_saved_breakpoint_does_not_stop(env):
    out, code = run_detached(["-k", "6", PROG], stdin_lines=["hi"], **env)
    assert "got:hi" in out
    assert code == 7


def test_q_q_still_kills_program(env):
    out, _ = run_detached([PROG], keys=b"qq", **env)
    assert "after-ran" not in out
    assert "tdb: detached" not in out
    assert detach_harness.leftovers == []


def test_q_escape_keeps_tui(env):
    out, code = run_detached([PROG], keys=b"q\x1bqq", **env)
    assert "tdb: detached" not in out
    assert "after-ran" not in out
    assert code == 0
