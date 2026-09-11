"""End-to-end: `--replay-tui` drives a real debugpy session inside the TUI."""

import asyncio

import pytest

from tdb.cli import build_replay_tui_app
from tdb.persist import TdbConfig
from tdb.replay import load_recording
from tdb.widgets.evaluate_console import EvaluateConsole

from tests.integration.test_replay_session import make_recording


async def _no_sleep(_s):
    return None


@pytest.fixture(autouse=True)
def _no_breakpoint_persistence(monkeypatch):
    # quit saves breakpoints under the program key; keep the developer's
    # real ~/.config/tdb/breakpoints.json out of it.
    monkeypatch.setattr("tdb.app.save_breakpoints", lambda *a, **k: None)
    monkeypatch.setattr("tdb.app.load_breakpoints", lambda *a, **k: {})


async def test_replay_tui_performs_recording_in_the_tui(tmp_path):
    path, prog = make_recording(
        tmp_path,
        [
            ("set_breakpoint", [f"{prog_path(tmp_path)}:3"]),
            ("continue", []),
            ("evaluate", ["x + y"]),
            ("next", []),
            ("quit", []),
        ],
    )
    app, driver = build_replay_tui_app(
        load_recording(path), label="s.jsonl", config=TdbConfig()
    )
    driver.sleep = _no_sleep
    seen_eval = ""
    async with app.run_test() as pilot:
        await asyncio.wait_for(driver.finished.wait(), timeout=90)
        await pilot.pause()
        console = app.query_one("#eval-console", EvaluateConsole)
        seen_eval = "\n".join(s.text for s in console.query_one("#eval-output").lines)
    assert driver.errors == 0, driver.transcript
    assert ">>> x + y" in seen_eval
    assert "3" in seen_eval  # evaluate result shown in the console
    assert driver.transcript[1].endswith(f"{prog}:3")  # continue hit the breakpoint
    assert driver.transcript[3].endswith(f"{prog}:4")  # next advanced one line
    assert app._exit  # recorded quit closed the TUI


def prog_path(tmp_path):
    return str(tmp_path / "toy.py")
