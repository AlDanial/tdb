"""Spec guarantees: strict mode, subshells, child processes, guards."""

import asyncio

import pytest

from tdb.adapters.bash.session import BashProtocolError, BashSession
from tests.integration.bash_adapter_harness import FIXTURES, bash_ok
from tests.integration.test_bash_session import Recorder, _launch

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")


@pytest.mark.asyncio
async def test_strict_mode_script_debugs_identically():
    """set -euo pipefail: breakpoint in a function, locals, step, finish."""
    rec = Recorder()
    fixture = FIXTURES / "bash_strict_functions.sh"
    session = await _launch(fixture, rec)
    await session.set_breakpoint(str(fixture), 5)  # count=$((count + n))
    session.resume("continue")
    reason, _, line = await rec.wait_stop()
    assert (reason, line) == ("breakpoint", 5)
    lv = {v.name: v for v in await session.locals()}
    assert "n" in lv
    session.resume("continue")  # second bump() hit
    await rec.wait_stop()
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert rec.exit_code == 0
    assert "count=5" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_no_stop_inside_subshells_and_pipelines():
    """Breakpoint on every line: whatever stops fire, none are inside a
    subshell (checked directly via $BASH_SUBSHELL at each stop), the
    script's output is complete, and no line double-stops."""
    rec = Recorder()
    fixture = FIXTURES / "bash_subshell.sh"
    session = await _launch(fixture, rec)
    for line in range(1, 5):
        await session.set_breakpoint(str(fixture), line)
    session.resume("continue")
    stopped_lines = []
    while len(stopped_lines) < 10:
        try:
            await asyncio.wait_for(rec.stop_event.wait(), 3)
        except asyncio.TimeoutError:
            break
        rec.stop_event.clear()
        stopped_lines.append(rec.stops[-1][2])
        rc, out = await session.evaluate("echo $BASH_SUBSHELL")
        assert out.strip() == "0"  # the actual spec guarantee
        session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert len(stopped_lines) >= 1  # top level did stop
    assert len(stopped_lines) == len(set(stopped_lines))  # no double-stops
    assert "value=from-subshell" in rec.stdout()
    assert "piped one" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_child_bash_runs_uninstrumented():
    rec = Recorder()
    fixture = FIXTURES / "bash_spawns_child.sh"
    session = await _launch(fixture, rec)
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert "child ran, BASH_ENV=unset" in rec.stdout()
    assert "parent done" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_debuggee_clobbering_debug_trap_degrades_to_free_run():
    """Documented v1 limitation: script completes, no hang, no crash."""
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_own_trap.sh", rec)
    session.resume("step")
    await rec.wait_stop()  # entry stop still works
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert "still ran to completion" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_missing_bash_reports_hint():
    rec = Recorder()
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    with pytest.raises(BashProtocolError, match="bash not found"):
        await session.launch(
            program="/tmp/x.sh",
            args=[],
            cwd="/tmp",
            env=None,
            bash="definitely-not-bash-xyz",
        )


@pytest.mark.asyncio
async def test_old_bash_reports_version_error_fast(tmp_path):
    """bash's BASH_VERSINFO is readonly, so real 3.2 can't be simulated;
    instead a fake `bash` behaves exactly as old bash + harness would
    (version line on stderr, exit 2) and must surface as a fast launch
    failure carrying that line — the session's died-before-ready path."""
    fake = tmp_path / "bash"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'tdb: bash >= 4.4 is required to debug (this is bash 3.2.57)' >&2\n"
        "exit 2\n"
    )
    fake.chmod(0o755)
    rec = Recorder()
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    with pytest.raises(BashProtocolError, match="4.4"):
        await asyncio.wait_for(
            session.launch(
                program=str(FIXTURES / "bash_hello.sh"),
                args=[],
                cwd="/tmp",
                env=None,
                bash=str(fake),
            ),
            5,  # must fail fast via _reap, not the 15s ready timeout
        )
