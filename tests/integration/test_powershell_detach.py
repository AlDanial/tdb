"""`q` then `d` in the TUI: a PowerShell script keeps running under tdb's
relay (output, stdin, exit code, Ctrl-C ends it)."""

from __future__ import annotations

import sys

import pytest

from . import detach_harness
from .conftest import FIXTURES_DIR
from .detach_harness import CONTINUE_THEN_DETACH, run_detached
from .powershell_adapter_harness import pwsh_ok

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32" or not pwsh_ok(),
        reason="needs a pty and pwsh + PSES",
    ),
    pytest.mark.xdist_group("pty"),
]

PROG = str(FIXTURES_DIR / "powershell_detach_prog.ps1")
SLOW = 90  # pwsh + PSES start slowly


@pytest.fixture
def env(tmp_path):
    return {
        "cwd": tmp_path,
        "env": {"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)},
    }


def test_powershell_detach_output_stdin_exit_code(env):
    out, code = run_detached([PROG], stdin_lines=["hello"], timeout=SLOW, **env)
    assert "tdb: detached" in out
    assert "after-ran" in out
    assert "got:hello" in out
    assert code == 7
    assert detach_harness.leftovers == []


def test_powershell_detach_ctrl_c_kills_loop(env):
    out, code = run_detached([PROG, "loop"], interrupt_after=3, timeout=SLOW, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


def test_powershell_detach_ctrl_c_kills_script_blocked_in_stdin(env):
    out, code = run_detached([PROG, "read"], interrupt_after=3, timeout=SLOW, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


def test_powershell_detach_while_running_ctrl_c_kills_loop(env):
    """`d` pressed while the script runs (after `c`)."""
    out, code = run_detached(
        [PROG, "loop"],
        keys=CONTINUE_THEN_DETACH,
        interrupt_after=3,
        timeout=SLOW,
        **env,
    )
    assert "tdb: detached" in out
    assert code == 130
    assert detach_harness.leftovers == []
