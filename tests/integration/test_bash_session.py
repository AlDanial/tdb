"""BashSession <-> tdb_harness.sh, no DAP layer involved."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tdb.adapters.bash.session import BashSession

FIXTURES = Path(__file__).parent / "fixtures"


def _bash_ok() -> bool:
    bash = shutil.which("bash")
    if not bash:
        return False
    out = subprocess.run(
        [bash, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
    ).stdout.strip()
    major, minor = (int(p) for p in out.split("."))
    return (major, minor) >= (4, 4)


pytestmark = pytest.mark.skipif(not _bash_ok(), reason="needs bash >= 4.4")


class Recorder:
    def __init__(self):
        self.output: list[tuple[str, str]] = []
        self.stops: list[tuple[str, str, int]] = []
        self.exit_code = None
        self.stop_event = asyncio.Event()
        self.exit_event = asyncio.Event()

    def on_output(self, text, category):
        self.output.append((text, category))

    def on_stop(self, reason, path, line):
        self.stops.append((reason, path, line))
        self.stop_event.set()

    def on_exit(self, code):
        self.exit_code = code
        self.exit_event.set()

    async def wait_stop(self):
        await asyncio.wait_for(self.stop_event.wait(), 10)
        self.stop_event.clear()
        return self.stops[-1]

    def stdout(self):
        return "".join(t for t, c in self.output if c == "stdout")


async def _launch(program, rec, args=None):
    session = BashSession(rec.on_output, rec.on_stop, rec.on_exit)
    await session.launch(
        program=str(program), args=args or [], cwd=str(program.parent), env=None
    )
    return session


@pytest.mark.asyncio
async def test_launch_reaches_config_phase():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    assert session.stopped is True  # config phase counts as stopped
    await session.stop()


@pytest.mark.asyncio
async def test_stop_on_entry_then_continue_to_exit():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    session.resume("step")  # stopOnEntry = arm step before line 1
    reason, path, line = await rec.wait_stop()
    assert reason == "entry"
    assert path.endswith("bash_hello.sh")
    assert line == 1
    assert session.stopped is True
    session.resume("continue")
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert rec.exit_code == 7
    assert "hello from bash" in rec.stdout()
    assert "x is 1" in rec.stdout()
    await session.stop()


@pytest.mark.asyncio
async def test_continue_from_config_runs_to_exit():
    rec = Recorder()
    session = await _launch(FIXTURES / "bash_hello.sh", rec)
    session.resume("continue")  # stopOnEntry=false path
    await asyncio.wait_for(rec.exit_event.wait(), 10)
    assert rec.exit_code == 7
    assert rec.stops == []
    await session.stop()
