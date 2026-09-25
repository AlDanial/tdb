"""TdbApp wires VariableView display clicks to the controller.

`DisplayRequested` / `UndisplayRequested` (right-click a variable row,
left-click a Display row) go through `controller.set_display`, which
re-fetches scopes; the app then redraws the Variables View so the
Display scope appears or shrinks at once.
"""

from __future__ import annotations

import pytest

from tdb.app import TdbApp
from tdb.dap.types import Scope, Variable
from tdb.persist import TdbConfig
from tdb.session.state import DISPLAY_SCOPE_REF
from tdb.widgets.variable_view import VariableView

from tests.unit.record_helpers import CaptureRecorder


@pytest.fixture
async def app():
    app = TdbApp(program="", config=TdbConfig(), recorder=CaptureRecorder())
    async with app.run_test() as pilot:
        await pilot.pause()
        yield app


def _labels(view: VariableView) -> dict[str, list[str]]:
    return {
        str(scope.label): [str(child.label) for child in scope.children]
        for scope in view.root.children
    }


def _fake_set_display(state, calls):
    async def set_display(expr, on):
        calls.append((expr, on))
        if on:
            state.display.append(expr)
        else:
            state.display.remove(expr)
        state.scopes = [Scope(name="Locals", variables_reference=5)]
        state.variables = {5: [Variable(name="a", value="3")]}
        if on:
            state.scopes.insert(
                0, Scope(name="Display", variables_reference=DISPLAY_SCOPE_REF)
            )
            state.variables[DISPLAY_SCOPE_REF] = [Variable(name=expr, value="3")]

    return set_display


async def test_display_requested_adds_scope_and_redraws(app, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        app.controller, "set_display", _fake_set_display(app.controller.state, calls)
    )
    await app.on_variable_view_display_requested(VariableView.DisplayRequested("a"))
    assert calls == [("a", True)]
    view = app.query_one("#variable-view", VariableView)
    assert _labels(view) == {"Display": ["a = 3"], "Locals": ["a = 3"]}


async def test_undisplay_requested_removes_scope_and_redraws(app, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        app.controller, "set_display", _fake_set_display(app.controller.state, calls)
    )
    await app.on_variable_view_display_requested(VariableView.DisplayRequested("a"))
    await app.on_variable_view_undisplay_requested(VariableView.UndisplayRequested("a"))
    assert calls == [("a", True), ("a", False)]
    view = app.query_one("#variable-view", VariableView)
    assert _labels(view) == {"Locals": ["a = 3"]}


async def test_display_click_is_recorded_as_console_command(app, monkeypatch):
    """Record/replay sees a click exactly as the typed `display a`."""
    monkeypatch.setattr(
        app.controller, "set_display", _fake_set_display(app.controller.state, [])
    )
    await app.on_variable_view_display_requested(VariableView.DisplayRequested("a"))
    await app.on_variable_view_undisplay_requested(VariableView.UndisplayRequested("a"))
    evaluates = [r for r in app.recorder.records if r[0] == "evaluate"]
    assert evaluates == [
        ("evaluate", ["display a"]),
        ("evaluate", ["undisplay a"]),
    ]
