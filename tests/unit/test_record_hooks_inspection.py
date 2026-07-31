"""Evaluate, frame-selection, and variable-expansion recording."""

import pytest

from tdb.app import TdbApp
from tdb.dap.types import Source, StackFrame, Variable
from tdb.persist import TdbConfig
from tdb.widgets.evaluate_console import EvaluateConsole
from tdb.widgets.stack_view import StackView
from tdb.widgets.variable_view import VariableView

from tests.unit.record_helpers import CaptureRecorder


async def _noop(*a, **k):
    return None


def _frames():
    return [
        StackFrame(id=101, name="inner", line=3, source=Source(path="/x.py")),
        StackFrame(id=102, name="mid", line=9, source=Source(path="/x.py")),
        StackFrame(id=103, name="outer", line=20, source=Source(path="/x.py")),
    ]


@pytest.fixture
async def app_cap(monkeypatch):
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
        cap.records.clear()
        yield app, cap, pilot


async def test_evaluate_entry_records(app_cap, monkeypatch):
    app, cap, _ = app_cap

    async def fake_eval(expr):
        return "42"

    monkeypatch.setattr(app.controller, "evaluate", fake_eval)
    await app.on_evaluate_console_evaluate_requested(
        EvaluateConsole.EvaluateRequested("len(data)")
    )
    assert cap.records == [("evaluate", ["len(data)"])]


async def test_frame_click_down_the_stack_records_stack_ups(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.set_stack(_frames())  # current = id 101 (index 0)
    monkeypatch.setattr(app.controller, "select_frame", _noop)
    await app.on_stack_view_frame_selected(
        StackView.FrameSelected(103, None, 20)  # index 2: toward caller
    )
    assert cap.records == [("stack_up", []), ("stack_up", [])]


async def test_frame_click_back_toward_top_records_stack_downs(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.set_stack(_frames())
    app.controller.state.current_frame_id = 103  # user is at index 2
    monkeypatch.setattr(app.controller, "select_frame", _noop)
    await app.on_stack_view_frame_selected(StackView.FrameSelected(102, None, 9))
    assert cap.records == [("stack_down", [])]


async def test_frame_click_on_synthetic_stack_not_recorded(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.set_stack(_frames())
    app.controller.state.displayed_frames_are_synthetic = True
    monkeypatch.setattr(app.controller, "select_frame", _noop)
    await app.on_stack_view_frame_selected(StackView.FrameSelected(103, None, 20))
    assert cap.records == []


async def test_variable_expand_records_inspect_with_evaluate_name(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.variables = {
        5: [
            Variable(
                name="data",
                value="{...}",
                variables_reference=7,
                evaluate_name="data['x']",
            )
        ]
    }

    class FakeClient:
        async def variables(self, ref):
            return []

    monkeypatch.setattr(
        type(app.controller), "active_client", property(lambda self: FakeClient())
    )
    var_view = app.query_one("#variable-view", VariableView)
    await app.on_tdb_app_lazy_load_variables(app.LazyLoadVariables(7, var_view.root))
    assert cap.records == [("inspect", ["data['x']"])]


async def test_variable_expand_without_evaluate_name_records_nothing(
    app_cap, monkeypatch
):
    app, cap, _ = app_cap
    app.controller.state.variables = {
        5: [Variable(name="%h", value="HASH", variables_reference=7)]
    }

    class FakeClient:
        async def variables(self, ref):
            return []

    monkeypatch.setattr(
        type(app.controller), "active_client", property(lambda self: FakeClient())
    )
    var_view = app.query_one("#variable-view", VariableView)
    await app.on_tdb_app_lazy_load_variables(app.LazyLoadVariables(7, var_view.root))
    assert cap.records == []
