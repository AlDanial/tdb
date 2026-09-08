"""Real Rust run-mode pause through the public readiness callback."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal

import pytest

from tdb import run_mode
from tdb.languages.rust import build_rust_profile
from tdb.persist import TdbConfig
from tests.integration.rust_adapter_harness import (
    WAIT,
    available_rust_adapters,
    run_mode_pause_probe,
    _ready_listener,
    _rust_debug_binary,  # noqa: F401 - registers rust_debug_binary fixture
)

# With no adapter installed the parametrize lists below would be empty
# and pytest's default 'empty parameter set' skip hides the real reason.
pytestmark = pytest.mark.skipif(
    not available_rust_adapters(),
    reason="Rust debugging requires gdb >= 14 or lldb-dap (LLVM >= 17)",
)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_run_mode_pauses_blocked_rust_program(adapter, rust_debug_binary):
    result = await run_mode_pause_probe(rust_debug_binary("park", adapter), adapter)

    assert result.paused is True
    assert result.adopted is True
    assert result.resumed is True
    assert result.terminated is True
    assert result.episode_count >= 1


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_run_mode_examine_includes_rust_concurrency(
    adapter, rust_debug_binary, capfd
):
    """`tdb --run` + SIGUSR2 on a parked Rust program: the record carries
    the concurrency snapshot under `rust_concurrency` and no Go key."""
    target = rust_debug_binary("park", adapter)

    async def episode(controller, handler, console, config, program):
        return False  # terminate

    async with _ready_listener() as (port, fixture_ready):

        async def pulses():
            connection = await asyncio.wait_for(fixture_ready, WAIT)
            assert connection.scenario == target.scenario
            os.kill(os.getpid(), signal.SIGUSR2)
            await asyncio.sleep(5.0)  # native stacks are slower to walk
            os.kill(os.getpid(), signal.SIGUSR1)

        task = asyncio.create_task(pulses())
        try:
            await asyncio.wait_for(
                run_mode.run(
                    program=target.program,
                    args=target.arguments(port, control=True),
                    profile=build_rust_profile(adapter=adapter),
                    config=TdbConfig(),
                    tui_episode=episode,
                    examine_dests=["-"],
                ),
                WAIT * 3,
            )
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    recs = [
        json.loads(line)
        for line in capfd.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert len(recs) == 1, recs
    rec = recs[0]
    assert rec["status"] == "ok" and rec["language"] == "rust"
    assert "goroutines" not in rec
    assert "threads" in rec["rust_concurrency"]
    assert rec["processes"][0]["threads"], rec
