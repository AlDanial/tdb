"""Unit tests for TerminalLauncher's runInTerminal handler.

Pins the emulator-wrapping behavior with a fake `-e`-style terminal
emulator on PATH -- this is the piece every language's terminal-mode
launch (perl, bash, tcsh, C/C++ via lldb-dap) funnels through, so a
single fast unit test here covers the shared plumbing without needing
a real terminal emulator or a real debuggee.
"""

from __future__ import annotations

import asyncio
import os

from tdb.dap.messages import Request
from tdb.session.terminal import TerminalLauncher

FAKE_EMULATOR = """#!/bin/sh
# records argv then execs the payload after the -e flag
printf '%s\\n' "$@" > "$FAKEEM_LOG"
shift   # drop -e
exec "$@"
"""


async def test_launcher_wraps_command_in_emulator(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    exe = tmp_path / "fakeem"
    exe.write_text(FAKE_EMULATOR)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKEEM_LOG", str(log))
    monkeypatch.setattr(
        "tdb.session.terminal._TERMINAL_SPECS",
        {"fakeem": ("fakeem", ["-e"])},
    )
    marker = tmp_path / "ran"
    launcher = TerminalLauncher("fakeem")
    request = Request(
        seq=1,
        command="runInTerminal",
        arguments={
            "args": ["/bin/sh", "-c", f"echo done > {marker}"],
            "cwd": str(tmp_path),
        },
    )
    body = await launcher.handle_run_in_terminal(request)
    assert body == {}
    deadline = asyncio.get_running_loop().time() + 5
    while not marker.exists():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.02)
    assert log.read_text().splitlines()[0] == "-e"
