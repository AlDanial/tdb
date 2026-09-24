"""Focusing the Breakpoints View triggers a sync with the native debugger.

Every way of switching to the view (Ctrl+B, mouse click, Tab cycling)
goes through widget focus, so the view posts SyncRequested from its
focus handler and the app asks the controller to reconcile, then
redraws the table from the (possibly updated) state.
"""

from __future__ import annotations

import pytest

from tdb.app import TdbApp
from tdb.dap.types import SourceBreakpoint
from tdb.persist import TdbConfig
from tdb.widgets.breakpoint_view import BreakpointView


@pytest.fixture
async def app_pilot():
    app = TdbApp(program="", config=TdbConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        yield app, pilot


async def test_focus_syncs_and_redraws_breakpoint_table(app_pilot, monkeypatch):
    app, pilot = app_pilot
    calls = []

    async def fake_sync():
        calls.append("sync")
        app.controller.state.breakpoints["/src/a.c"] = [SourceBreakpoint(line=83)]
        return True

    monkeypatch.setattr(app.controller, "sync_breakpoints_from_debugger", fake_sync)
    bp_view = app.query_one("#breakpoint-view", BreakpointView)
    assert bp_view.row_count == 0

    app.action_focus_breakpoints()
    await pilot.pause()
    await pilot.pause()

    assert calls == ["sync"]
    assert bp_view.row_count == 1
    assert bp_view._entries == [("/src/a.c", 83)]


async def test_focus_without_changes_does_not_redraw(app_pilot, monkeypatch):
    app, pilot = app_pilot
    app.controller.state.breakpoints["/src/a.c"] = [SourceBreakpoint(line=83)]
    bp_view = app.query_one("#breakpoint-view", BreakpointView)
    bp_view.update_breakpoints(app.controller.state.breakpoints)
    redraws = []
    orig = bp_view.update_breakpoints
    monkeypatch.setattr(
        bp_view, "update_breakpoints", lambda bps: (redraws.append(1), orig(bps))
    )

    async def fake_sync():
        return False

    monkeypatch.setattr(app.controller, "sync_breakpoints_from_debugger", fake_sync)
    app.action_focus_breakpoints()
    await pilot.pause()
    await pilot.pause()
    assert redraws == []
    assert bp_view.row_count == 1
