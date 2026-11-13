"""`q` then `d` in the TUI: a Ruby script keeps running under tdb's relay
(output, stdin, exit code, Ctrl-C via the private `tdbSignal` request)."""

from __future__ import annotations

import sys

import pytest

from . import detach_harness
from .conftest import FIXTURES_DIR
from .detach_harness import run_detached
from .ruby_adapter_harness import launch_stopped, rdbg_ok, start_ruby_adapter

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32" or not rdbg_ok(),
        reason="needs a pty and rdbg (debug gem >= 1.9)",
    ),
    pytest.mark.xdist_group("pty"),
]

PROG = str(FIXTURES_DIR / "ruby_detach_prog.rb")


@pytest.fixture
def env(tmp_path):
    return {"cwd": tmp_path, "env": {"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)}}


def test_ruby_detach_output_stdin_exit_code(env):
    out, code = run_detached([PROG], stdin_lines=["hello"], **env)
    assert "tdb: detached" in out
    assert "after-ran" in out
    assert "got:hello" in out
    assert code == 7
    assert detach_harness.leftovers == []


def test_ruby_detach_ctrl_c_kills_loop(env):
    out, code = run_detached([PROG, "loop"], interrupt_after=1.5, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


def test_ruby_detach_ctrl_c_runs_program_handler(env):
    out, code = run_detached([PROG, "trap"], interrupt_after=1.5, **env)
    assert "looping-now" in out
    assert "caught-int" in out
    assert code == 5
    assert detach_harness.leftovers == []


def test_ruby_detach_ctrl_c_raises_interrupt(env):
    out, code = run_detached([PROG, "rescue"], interrupt_after=1.5, **env)
    assert "rescued-int" in out
    assert code == 6
    assert detach_harness.leftovers == []


def test_ruby_detach_ctrl_c_kills_script_blocked_in_stdin(env):
    out, code = run_detached([PROG, "read"], interrupt_after=1.5, timeout=15, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


@pytest.mark.asyncio
async def test_tdbsignal_rejects_other_signals_and_missing_debuggee():
    client = await start_ruby_adapter()
    try:
        resp = await client.request("tdbSignal", {"signal": "SIGINT"})
        assert resp["success"] is False  # no launch yet
        await launch_stopped(client, PROG, stop_on_entry=False)
        resp = await client.request("tdbSignal", {"signal": "SIGKILL"})
        assert resp["success"] is False
    finally:
        await client.stop()
