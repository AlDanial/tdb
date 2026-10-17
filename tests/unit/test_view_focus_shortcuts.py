"""Ctrl+<view> shortcuts: pressing a view's own shortcut toggles back to
the previous view, and the Evaluate / Console input lines pass Ctrl+E and
Ctrl+V through as view shortcuts instead of Input's end-of-line / paste."""

from __future__ import annotations

import pytest

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.widgets.console_view import ConsoleView


@pytest.fixture(autouse=True)
def _isolate_debugger_lifecycle(monkeypatch):
    async def no_op(self, *args, **kwargs):
        pass

    monkeypatch.setattr(DebugController, "start", no_op)
    monkeypatch.setattr(DebugController, "stop", no_op)


async def _press(pilot, *keys: str) -> None:
    await pilot.press(*keys)
    await pilot.pause()


def _view(app: TdbApp) -> str | None:
    return app._view_of(app.focused)


async def test_own_shortcut_toggles_back_to_previous_view():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await _press(pilot, "ctrl+c")
        assert _view(app) == "code-view"
        await _press(pilot, "ctrl+e")
        assert app.focused.id == "eval-input"
        await _press(pilot, "ctrl+e")
        assert _view(app) == "code-view"
        # And back again: the toggle is symmetric.
        await _press(pilot, "ctrl+c")
        assert app.focused.id == "eval-input"


@pytest.mark.parametrize(
    "key, view",
    [
        ("ctrl+o", "console-view"),
        ("ctrl+v", "variable-view"),
        ("ctrl+s", "stack-view"),
        ("ctrl+b", "breakpoint-view"),
    ],
)
async def test_every_view_shortcut_toggles(key, view):
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await _press(pilot, "ctrl+c", key)
        assert _view(app) == view
        await _press(pilot, key)
        assert _view(app) == "code-view"


async def test_previous_view_follows_non_shortcut_focus_changes():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await _press(pilot, "ctrl+c")
        app.query_one("#stack-view").focus()  # e.g. a mouse click
        await pilot.pause()
        await _press(pilot, "ctrl+v", "ctrl+v")
        assert _view(app) == "stack-view"


async def test_own_shortcut_without_history_stays_put():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await _press(pilot, "ctrl+c")
        app._previous_view = None
        await _press(pilot, "ctrl+c")
        assert _view(app) == "code-view"


async def test_evaluate_ctrl_v_goes_to_variables():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await _press(pilot, "ctrl+e")
        await _press(pilot, "ctrl+v")
        assert _view(app) == "variable-view"


@pytest.mark.parametrize(
    "key, view", [("ctrl+v", "variable-view"), ("ctrl+e", "eval-console")]
)
async def test_console_stdin_line_shortcuts(key, view):
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        console = app.query_one("#console-view", ConsoleView)
        console.set_stdin_enabled(True)
        await _press(pilot, "ctrl+o")
        assert app.focused.id == "console-stdin"
        await _press(pilot, key)
        assert _view(app) == view


@pytest.mark.parametrize(
    "key, view", [("ctrl+v", "variable-view"), ("ctrl+e", "eval-console")]
)
async def test_console_log_shortcuts(key, view):
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await _press(pilot, "ctrl+o")
        assert app.focused.id == "console-log"
        await _press(pilot, key)
        assert _view(app) == view
