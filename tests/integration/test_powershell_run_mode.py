"""Run mode (`tdb --run`) against a real PSES session: headless run with
no TUI episode, exit-code passthrough, and a fatal error's exit 1.

The Python signal-pause episode test in test_run_mode.py
(`test_signal_pause_episode_detach_and_terminate`) is not parameterized
by profile -- it drives an inline Python LOOP_SCRIPT -- so it is not
ported here; controller-level pause is covered by
test_powershell_session.py::test_pause_while_running_then_evaluate.
"""

from __future__ import annotations

import asyncio

import pytest

from tdb import run_mode
from tdb.languages.powershell import build_powershell_profile
from tdb.persist import TdbConfig

from tests.integration.powershell_adapter_harness import FIXTURES, pwsh_ok

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")

TIMEOUT = 90.0


async def test_powershell_runs_headless_without_tui_episode(tmp_path, capfd):
    p = tmp_path / "hello.ps1"
    p.write_text('Write-Host "pshello"\n')
    episodes = []

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        return False

    code = await asyncio.wait_for(
        run_mode.run(
            program=str(p),
            config=TdbConfig(),
            profile=build_powershell_profile(),
            tui_episode=fake_episode,
        ),
        timeout=TIMEOUT,
    )
    assert episodes == [], "spurious TUI episode during headless PowerShell run"
    assert code == 0
    assert "pshello" in capfd.readouterr().out


async def test_powershell_exit_code_passthrough(capfd):
    code = await asyncio.wait_for(
        run_mode.run(
            program=str(FIXTURES / "exit7.ps1"),
            config=TdbConfig(),
            profile=build_powershell_profile(),
        ),
        timeout=TIMEOUT,
    )
    assert code == 7
    assert "bye" in capfd.readouterr().out


async def test_powershell_fatal_error_exit_1(capfd):
    code = await asyncio.wait_for(
        run_mode.run(
            program=str(FIXTURES / "throws.ps1"),
            config=TdbConfig(),
            profile=build_powershell_profile(),
        ),
        timeout=TIMEOUT,
    )
    assert code == 1
    out = capfd.readouterr()
    assert "kaboom" in out.out + out.err
