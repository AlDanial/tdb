"""End-to-end run mode against a real debugpy session, in-process:
exit-code passthrough, output streaming, SIGUSR1 -> pause -> episode ->
detach -> resume -> second episode -> terminate."""

import asyncio
import os
import shutil
import signal
import subprocess

import pytest

from tdb import run_mode
from tdb.persist import TdbConfig
from tdb.session.state import SessionPhase
from tests.integration.ruby_adapter_harness import rdbg_ok

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="signal-driven run mode tests are POSIX-only"
)

perl_available = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

ruby_available = pytest.mark.skipif(
    not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)"
)


# `os._exit` (not `sys.exit`) deliberately: debugpy's `userUnhandled`
# exception filter ignores SystemExit(0)/SystemExit(None) but NOT other
# codes (pydevd_process_net_command_json.py's `ignore_system_exit_codes
# = [0, None]` default) — `sys.exit(7)` would trip a real exception-stop
# and open a TUI episode instead of exiting cleanly, which is correct
# debugpy behavior but not what this passthrough test wants to exercise.
# `sys.stdout.flush()` matters because debugpy's output redirection
# still sees Python-level writes, but os._exit skips interpreter
# finalization (which would otherwise flush automatically).
EXIT_SCRIPT = "import os, sys\nprint('bye')\nsys.stdout.flush()\nos._exit(7)\n"
LOOP_SCRIPT = "import time\ni = 0\nwhile True:\n    i += 1\n    time.sleep(0.01)\n"


async def _wait_until(pred, timeout=20.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.05)


async def test_exit_code_and_output_passthrough(tmp_path, capfd):
    p = tmp_path / "exit7.py"
    p.write_text(EXIT_SCRIPT)
    code = await run_mode.run(program=str(p), config=TdbConfig())
    assert code == 7
    assert "bye" in capfd.readouterr().out


@perl_available
async def test_perl_compile_phase_runs_headless_without_tui_episode(tmp_path, capfd):
    """Regression: `tdb --run prog.pl` where prog.pl has compile-time
    statements (`use` lines). The compile-phase shim's re-trap after the
    adapter's entry `c` surfaced as a spurious "step" stop, which run
    mode treated as a signal to open the TUI. The program must instead
    run straight to completion with no episode."""
    from tdb.languages.perl import build_perl_profile

    p = tmp_path / "hello.pl"
    p.write_text('use strict;\nuse warnings;\nprint "phello\\n";\n')
    episodes = []

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        return False

    code = await asyncio.wait_for(
        run_mode.run(
            program=str(p),
            config=TdbConfig(),
            profile=build_perl_profile(),
            tui_episode=fake_episode,
        ),
        timeout=60.0,
    )
    assert episodes == [], "spurious TUI episode during headless perl run"
    assert code == 0
    assert "phello" in capfd.readouterr().out


async def test_signal_pause_episode_detach_and_terminate(tmp_path):
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    box = {}
    episodes = []

    def ready(controller):
        box["controller"] = controller

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        assert controller.state.phase is SessionPhase.STOPPED
        assert console.last_stop is not None
        # Episode 1 detaches; episode 2 terminates.
        return len(episodes) == 1

    async def pulses():
        await _wait_until(
            lambda: (
                box.get("controller") is not None
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)
        await _wait_until(
            lambda: (
                len(episodes) == 1
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)

    pulse_task = asyncio.ensure_future(pulses())
    try:
        code = await asyncio.wait_for(
            run_mode.run(
                program=str(p),
                config=TdbConfig(),
                tui_episode=fake_episode,
                on_session_ready=ready,
            ),
            timeout=90.0,
        )
    finally:
        pulse_task.cancel()
    assert episodes and len(episodes) == 2
    assert code == 0
    assert box["controller"].state.is_terminated


async def test_run_cleans_up_when_tui_episode_raises(tmp_path):
    """A TUI episode that raises must not orphan the adapter+debuggee:
    run() should stop the controller before re-raising."""
    p = tmp_path / "loop.py"
    p.write_text(LOOP_SCRIPT)
    box = {}

    def ready(controller):
        box["controller"] = controller

    async def raising_episode(controller, handler, console, config, program):
        raise RuntimeError("boom")

    async def pulse():
        await _wait_until(
            lambda: (
                box.get("controller") is not None
                and box["controller"].state.phase is SessionPhase.RUNNING
            )
        )
        os.kill(os.getpid(), signal.SIGUSR1)

    pulse_task = asyncio.ensure_future(pulse())
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await asyncio.wait_for(
                run_mode.run(
                    program=str(p),
                    config=TdbConfig(),
                    tui_episode=raising_episode,
                    on_session_ready=ready,
                ),
                timeout=90.0,
            )
    finally:
        pulse_task.cancel()
    assert box["controller"].state.is_terminated


@ruby_available
async def test_ruby_runs_headless_without_tui_episode(tmp_path, capfd):
    from tdb.languages.ruby import build_ruby_profile

    p = tmp_path / "hello.rb"
    p.write_text('puts "rhello"\n')
    episodes = []

    async def fake_episode(controller, handler, console, config, program):
        episodes.append(controller.state.phase)
        return False

    code = await asyncio.wait_for(
        run_mode.run(
            program=str(p),
            config=TdbConfig(),
            profile=build_ruby_profile(),
            tui_episode=fake_episode,
        ),
        timeout=60.0,
    )
    assert episodes == [], "spurious TUI episode during headless ruby run"
    assert code == 0
    assert "rhello" in capfd.readouterr().out


@ruby_available
async def test_ruby_exit_code_passthrough(tmp_path, capfd):
    from tdb.languages.ruby import build_ruby_profile

    p = tmp_path / "exit7.rb"
    p.write_text('puts "rbye"\n$stdout.flush\nexit 7\n')
    code = await asyncio.wait_for(
        run_mode.run(program=str(p), config=TdbConfig(), profile=build_ruby_profile()),
        timeout=60.0,
    )
    assert code == 7
    assert "rbye" in capfd.readouterr().out
