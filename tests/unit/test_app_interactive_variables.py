"""The Evaluate console refreshes the Variables View after each entry.

Before this, `on_evaluate_console_evaluate_requested` only printed the
result: an assignment typed at the prompt stayed invisible until the
next stop. Now it routes through `controller.evaluate_console` (which
tracks created variables and re-fetches scopes) and redraws the tree.
"""

from __future__ import annotations

import pytest

from tdb.app import TdbApp
from tdb.dap.types import Scope, Variable
from tdb.persist import TdbConfig
from tdb.session.state import INTERACTIVE_SCOPE_REF
from tdb.widgets.evaluate_console import EvaluateConsole
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


async def test_evaluate_redraws_variables_with_interactive_scope(app, monkeypatch):
    state = app.controller.state

    async def fake_evaluate_console(expr):
        # What the controller does on a live session: refreshed scopes
        # plus the synthetic Interactive scope for the tracked name.
        state.scopes = [
            Scope(name="Locals", variables_reference=5),
            Scope(name="Interactive", variables_reference=INTERACTIVE_SCOPE_REF),
        ]
        state.variables = {
            5: [Variable(name="a", value="3")],
            INTERACTIVE_SCOPE_REF: [Variable(name="newvar", value="42")],
        }
        return ""

    monkeypatch.setattr(app.controller, "evaluate_console", fake_evaluate_console)
    await app.on_evaluate_console_evaluate_requested(
        EvaluateConsole.EvaluateRequested("newvar = 42")
    )
    view = app.query_one("#variable-view", VariableView)
    assert _labels(view) == {"Locals": ["a = 3"], "Interactive": ["newvar = 42"]}


async def test_evaluate_console_entry_uses_controller_evaluate_console(
    app, monkeypatch
):
    seen: list[str] = []

    async def fake_evaluate_console(expr):
        seen.append(expr)
        return "42"

    monkeypatch.setattr(app.controller, "evaluate_console", fake_evaluate_console)
    await app.on_evaluate_console_evaluate_requested(
        EvaluateConsole.EvaluateRequested("newvar")
    )
    assert seen == ["newvar"]
