"""Breakpoint gestures and the session-start dump produce records."""

import pytest

from tdb.app import TdbApp
from tdb.dap.types import SourceBreakpoint
from tdb.persist import TdbConfig
from tdb.widgets.breakpoint_view import BreakpointView
from tdb.widgets.code_view import CodeView

from tests.unit.record_helpers import CaptureRecorder


async def _noop(*a, **k):
    return None


@pytest.fixture
async def app_cap(monkeypatch):
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
        cap.records.clear()  # drop anything from mount
        yield app, cap, pilot


async def test_toggle_on_records_set(app_cap, monkeypatch):
    app, cap, _ = app_cap

    async def fake_toggle(path, line):
        app.controller.state.breakpoints[path] = [SourceBreakpoint(line=line)]

    monkeypatch.setattr(app.controller, "toggle_breakpoint", fake_toggle)
    await app.on_code_view_breakpoint_toggled(CodeView.BreakpointToggled("/x.py", 7))
    assert cap.records == [("set_breakpoint", ["/x.py:7"])]


async def test_toggle_off_records_remove(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.breakpoints["/x.py"] = [SourceBreakpoint(line=7)]

    async def fake_toggle(path, line):
        app.controller.state.breakpoints[path] = []

    monkeypatch.setattr(app.controller, "toggle_breakpoint", fake_toggle)
    await app.on_code_view_breakpoint_toggled(CodeView.BreakpointToggled("/x.py", 7))
    assert cap.records == [("remove_breakpoint", ["/x.py:7"])]


async def test_condition_apply_records_set_with_condition(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "set_breakpoint_condition", _noop)
    await app.on_tdb_app__apply_breakpoint_condition(
        app._ApplyBreakpointCondition("/x.py", 7, "n > 3", None)
    )
    assert cap.records == [("set_breakpoint", ["/x.py:7", "n > 3", ""])]


async def test_delete_from_breakpoint_view_records_remove(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "remove_breakpoint", _noop)
    await app.on_breakpoint_view_breakpoint_delete_requested(
        BreakpointView.BreakpointDeleteRequested("/x.py", 7)
    )
    assert cap.records == [("remove_breakpoint", ["/x.py:7"])]


async def test_clear_all_records_remove_per_breakpoint(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.breakpoints = {
        "/x.py": [SourceBreakpoint(line=3), SourceBreakpoint(line=9)],
        "/y.py": [SourceBreakpoint(line=1)],
    }
    monkeypatch.setattr(app.controller, "clear_all_breakpoints", _noop)
    await app.on_breakpoint_view_clear_all_requested(BreakpointView.ClearAllRequested())
    assert sorted(cap.records) == sorted(
        [
            ("remove_breakpoint", ["/x.py:3"]),
            ("remove_breakpoint", ["/x.py:9"]),
            ("remove_breakpoint", ["/y.py:1"]),
        ]
    )


async def test_disable_all_not_recorded(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "disable_all_breakpoints", _noop)
    monkeypatch.setattr(app.controller, "enable_all_breakpoints", _noop)
    await app.on_breakpoint_view_disable_all_requested(
        BreakpointView.DisableAllRequested()
    )
    assert cap.records == []


async def test_run_to_cursor_records_triple(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "run_to_cursor", _noop)
    await app.on_code_view_run_to_cursor(CodeView.RunToCursor("/x.py", 20))
    assert cap.records == [
        ("set_breakpoint", ["/x.py:20"]),
        ("continue", []),
        ("remove_breakpoint", ["/x.py:20"]),
    ]


async def test_run_to_cursor_on_existing_bp_records_continue_only(app_cap, monkeypatch):
    app, cap, _ = app_cap
    app.controller.state.breakpoints["/x.py"] = [SourceBreakpoint(line=20)]
    monkeypatch.setattr(app.controller, "run_to_cursor", _noop)
    await app.on_code_view_run_to_cursor(CodeView.RunToCursor("/x.py", 20))
    assert cap.records == [("continue", [])]


async def test_mount_dumps_initial_breakpoints_and_autocontinue(tmp_path):
    """-k/-t/persisted breakpoints become set_breakpoint records at start;
    a no-entry-stop session appends the auto-start continue record."""
    cap = CaptureRecorder()
    app = TdbApp(
        program="",
        config=TdbConfig(),
        stop_on_entry=False,
        cli_breakpoints=[("/x.py", 5, False)],
        recorder=cap,
    )
    async with app.run_test() as pilot:
        await pilot.pause()
    assert ("set_breakpoint", ["/x.py:5"]) in cap.records
    assert ("continue", []) in cap.records
    assert cap.records.index(("set_breakpoint", ["/x.py:5"])) < cap.records.index(
        ("continue", [])
    )


async def test_mount_no_autocontinue_when_stopping_on_entry():
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), stop_on_entry=True, recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
    assert ("continue", []) not in cap.records
