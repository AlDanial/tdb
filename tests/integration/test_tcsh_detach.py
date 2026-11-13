"""`q` then `d` in the TUI: a tcsh script keeps running under tdb's relay
(output, stdin, exit code, Ctrl-C via the private `tdbSignal` request)."""

from __future__ import annotations

import shutil
import sys

import pytest

from . import detach_harness
from .conftest import FIXTURES_DIR
from .detach_harness import run_detached
from .tcsh_dap_client import DAPClient

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("tcsh") is None,
        reason="needs a pty and tcsh",
    ),
    pytest.mark.xdist_group("pty"),
]

PROG = str(FIXTURES_DIR / "tcsh_detach_prog.csh")


@pytest.fixture
def env(tmp_path):
    return {
        "cwd": tmp_path,
        "env": {"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)},
    }


def test_tcsh_detach_output_stdin_exit_code(env):
    out, code = run_detached([PROG], stdin_lines=["hello"], **env)
    assert "tdb: detached" in out
    assert "after-ran" in out
    assert "got:hello" in out
    assert code == 7
    assert detach_harness.leftovers == []


def test_tcsh_detach_ctrl_c_kills_loop(env):
    out, code = run_detached([PROG, "loop"], interrupt_after=1.5, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


def test_tcsh_detach_ctrl_c_runs_program_onintr_label(env):
    out, code = run_detached([PROG, "trap"], interrupt_after=1.5, **env)
    assert "looping-now" in out
    assert "caught-int" in out
    assert code == 5
    assert detach_harness.leftovers == []


def test_tcsh_detach_ctrl_c_respects_onintr_ignore(env):
    out, code = run_detached([PROG, "ignore"], interrupt_after=1.5, **env)
    assert "looping-now" in out
    assert "survived-int" in out
    assert code == 6
    assert detach_harness.leftovers == []


def test_tcsh_detach_ctrl_c_kills_script_blocked_in_stdin(env):
    out, code = run_detached([PROG, "read"], interrupt_after=1.5, timeout=15, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


@pytest.mark.asyncio
async def test_tdbsignal_rejects_other_signals_and_missing_debuggee(
    dap_client: DAPClient,
) -> None:
    resp = await dap_client.request("tdbSignal", {"signal": "SIGINT"})
    assert resp["success"] is False  # no launch yet
    resp = await dap_client.request("tdbSignal", {"signal": "SIGKILL"})
    assert resp["success"] is False
