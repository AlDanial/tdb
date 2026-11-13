"""`q` then `d` in the TUI: a Perl script keeps running under tdb's relay
(output, stdin, exit code, Ctrl-C via the private `tdbSignal` request)."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from . import detach_harness
from .conftest import FIXTURES_DIR
from .detach_harness import run_detached
from .perl_adapter_harness import AdapterClient

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32"
        or shutil.which("perl") is None
        or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
        reason="needs a pty and perl >= 5.18",
    ),
    pytest.mark.xdist_group("pty"),
]

PROG = str(FIXTURES_DIR / "perl_detach_prog.pl")


@pytest.fixture
def env(tmp_path):
    return {
        "cwd": tmp_path,
        "env": {
            "TDB_LOG_DIR": str(tmp_path),
            "TDB_CONFIG_DIR": str(tmp_path),
            "TDB_PADWALKER_CACHE": str(tmp_path / "padwalker"),
        },
    }


def test_perl_detach_output_stdin_exit_code(env):
    out, code = run_detached([PROG], stdin_lines=["hello"], **env)
    assert "tdb: detached" in out
    assert "after-ran" in out
    assert "got:hello" in out
    assert code == 7
    assert detach_harness.leftovers == []


def test_perl_detach_ctrl_c_kills_loop(env):
    out, code = run_detached([PROG, "loop"], interrupt_after=1.5, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


def test_perl_detach_ctrl_c_runs_program_handler(env):
    out, code = run_detached([PROG, "trap"], interrupt_after=1.5, **env)
    assert "looping-now" in out
    assert "caught-int" in out
    assert code == 5
    assert detach_harness.leftovers == []


@pytest.mark.asyncio
async def test_tdbsignal_rejects_other_signals_and_missing_debuggee():
    client = AdapterClient()
    await client.start()
    try:
        resp = await client.request("tdbSignal", {"signal": "SIGINT"})
        assert resp["success"] is False  # no launch yet
        resp = await client.request("tdbSignal", {"signal": "SIGKILL"})
        assert resp["success"] is False
    finally:
        await client.stop()
