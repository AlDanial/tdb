"""Mouse routing for the gdb-style Display scope in VariableView.

Right-click on a variable row asks for it to be displayed; left-click
on a row under the "Display" scope asks for it to be removed. Ordinary
left-clicks keep Tree's cursor behaviour, and only same-button clicks
count toward the double-click that opens Full Contents.
"""

from __future__ import annotations

import pytest
from textual.app import App
from textual.message import Message

from tdb.dap.types import Scope, Variable
from tdb.session.state import DISPLAY_SCOPE_REF
from tdb.widgets.variable_view import VariableView


class _Harness(App):
    class LazyLoadVariables(Message):
        """Stand-in for TdbApp's; VariableView posts it on expand."""

        def __init__(self, *args) -> None:
            super().__init__()

    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def compose(self):
        yield VariableView(id="variable-view")

    def on_variable_view_display_requested(self, m) -> None:
        self.messages.append(("display", m.expression))

    def on_variable_view_undisplay_requested(self, m) -> None:
        self.messages.append(("undisplay", m.expression))

    def on_variable_view_show_full_contents(self, m) -> None:
        self.messages.append(("full", m.label))


def _populate(view: VariableView) -> None:
    view.update_variables(
        [
            Scope(name="Display", variables_reference=DISPLAY_SCOPE_REF),
            Scope(name="Locals", variables_reference=5),
        ],
        {
            DISPLAY_SCOPE_REF: [Variable(name="x", value="1", evaluate_name="x")],
            5: [
                Variable(name="x", value="1", evaluate_name="x"),
                Variable(
                    name="obj", value="Obj", variables_reference=9, evaluate_name="obj"
                ),
            ],
        },
    )


# Tree rows (show_root=False), each one line high, with no border on
# the bare widget:
#   0 Display
#   1   x = 1
#   2 Locals
#   3   x = 1
#   4   obj = Obj   (container)
#   5     ...       (placeholder, once expanded)
ROW_DISPLAY_X = 1
ROW_LOCALS_X = 3
ROW_LOCALS_OBJ = 4


@pytest.fixture
async def harness():
    app = _Harness()
    async with app.run_test() as pilot:
        _populate(app.query_one(VariableView))
        await pilot.pause()
        yield app, pilot


async def test_right_click_on_variable_requests_display(harness):
    app, pilot = harness
    await pilot.click(VariableView, offset=(6, ROW_LOCALS_X), button=3)
    await pilot.pause()
    assert app.messages == [("display", "x")]


async def test_right_click_on_scope_header_does_nothing(harness):
    app, pilot = harness
    await pilot.click(VariableView, offset=(2, 2), button=3)
    await pilot.pause()
    assert app.messages == []


async def test_right_click_on_display_row_does_nothing(harness):
    app, pilot = harness
    await pilot.click(VariableView, offset=(6, ROW_DISPLAY_X), button=3)
    await pilot.pause()
    assert app.messages == []


async def test_left_click_on_display_row_requests_undisplay(harness):
    app, pilot = harness
    await pilot.click(VariableView, offset=(6, ROW_DISPLAY_X), button=1)
    await pilot.pause()
    assert app.messages == [("undisplay", "x")]


async def test_left_click_on_ordinary_row_moves_cursor_only(harness):
    app, pilot = harness
    await pilot.click(VariableView, offset=(6, ROW_LOCALS_X), button=1)
    await pilot.pause()
    assert app.messages == []
    assert app.query_one(VariableView).cursor_line == ROW_LOCALS_X


async def test_nested_child_uses_evaluate_name(harness):
    app, pilot = harness
    view = app.query_one(VariableView)
    node = view.get_node_at_line(ROW_LOCALS_OBJ)
    view.load_children(
        node, [Variable(name="count", value="3", evaluate_name="obj.count")]
    )
    node.expand()
    await pilot.pause()
    await pilot.click(VariableView, offset=(8, ROW_LOCALS_OBJ + 1), button=3)
    await pilot.pause()
    assert app.messages == [("display", "obj.count")]


async def test_right_then_left_click_is_not_a_double_click(harness):
    app, pilot = harness
    await pilot.click(VariableView, offset=(6, ROW_LOCALS_OBJ), button=3)
    await pilot.click(VariableView, offset=(6, ROW_LOCALS_OBJ), button=1)
    await pilot.pause()
    assert ("full", "obj = Obj") not in app.messages
    assert app.messages == [("display", "obj")]


async def test_two_left_clicks_still_open_full_contents(harness):
    app, pilot = harness
    await pilot.click(VariableView, offset=(6, ROW_LOCALS_OBJ), button=1)
    await pilot.click(VariableView, offset=(6, ROW_LOCALS_OBJ), button=1)
    await pilot.pause()
    assert app.messages == [("full", "obj = Obj")]
