"""End-to-end: a program installed under site-packages (e.g. tdb's
packaged examples) still stops on entry and at breakpoints under
justMyCode.

pydevd classifies a file as library code from the *debuggee*
interpreter's site-packages, so the program goes into a throwaway
venv's purelib and runs on that venv's python.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import venv

import pytest

from tdb.run_mode import ConsoleRunHandler
from tdb.session.controller import DebugController


@pytest.fixture
def site_packages_program(tmp_path):
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=False, symlinks=sys.platform != "win32")
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    python = env_dir / bindir / ("python.exe" if sys.platform == "win32" else "python")
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    pkg = tmp_path.joinpath(purelib, "demo")
    pkg.mkdir(parents=True)
    prog = pkg / "prog.py"
    prog.write_text("x = 41\nx += 1\nprint(x)\n")
    return str(python), str(prog)


async def _first_of(*events, timeout=20.0):
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    for w in waiters:
        w.cancel()


async def test_entry_stop_for_program_in_site_packages(site_packages_program, tmp_path):
    python, prog = site_packages_program
    sink = ConsoleRunHandler()
    ctrl = DebugController(sink)
    try:
        await ctrl.start(
            program=prog, cwd=str(tmp_path), stop_on_entry=True, python=python
        )
        await asyncio.wait_for(sink.initialized.wait(), 20)
        await ctrl.do_configure()
        await _first_of(sink.stopped, sink.exited)
        assert sink.stopped.is_set(), "ran to completion without an entry stop"
        assert sink.last_stop[1] == "entry"
    finally:
        await ctrl.stop()
