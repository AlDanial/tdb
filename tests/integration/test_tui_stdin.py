"""End-to-end through the TUI: a real debugpy session, the prompt shows
up in the Console view, keystrokes typed into the Console's stdin line
reach the program, and its reply comes back into the same log.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input, RichLog

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.widgets.console_view import ConsoleView

from tests.unit.record_helpers import CaptureRecorder

PROMPT_SCRIPT = 'x = input("enter a value: ")\nprint(f"You entered: {x}")\n'


def _console_text(app: TdbApp) -> str:
    log = app.query_one("#console-view", ConsoleView).query_one(RichLog)
    return "\n".join(strip.text for strip in log.lines)


async def _wait_until(pilot, pred, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not met in time"
        await pilot.pause(0.1)


async def test_console_stdin_round_trip(tmp_path):
    prog = tmp_path / "prompt.py"
    prog.write_text(PROMPT_SCRIPT)
    recorder = CaptureRecorder()
    app = TdbApp(program=str(prog), config=TdbConfig(), recorder=recorder)
    async with app.run_test() as pilot:
        await _wait_until(pilot, lambda: "enter a value: " in _console_text(app))
        console = app.query_one("#console-view", ConsoleView)
        assert console.query_one(Input).display, "stdin line should be visible"
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert console.query_one(Input).has_focus
        await pilot.press("4", "2", "enter")
        await _wait_until(pilot, lambda: "You entered: 42" in _console_text(app))
        await _wait_until(
            pilot, lambda: "Process exited with code 0" in _console_text(app)
        )
        # Program gone: the stdin line is withdrawn again.
        assert not console.query_one(Input).display
        assert ("stdin", ["42"]) in recorder.records
        await app.action_quit_debugger()
