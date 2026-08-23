"""Stepping/continue/pause/stack/restart/quit gestures produce records."""

import pytest
from unittest.mock import Mock

from tdb.app import TdbApp
from tdb.persist import TdbConfig
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
        cap.records.clear()  # drop the session-start breakpoint/continue dump
        for name in ("continue_", "step_over", "step_in", "step_out"):
            monkeypatch.setattr(app.controller, name, _noop)
        yield app, cap, pilot


@pytest.mark.parametrize(
    "gesture,expected",
    [
        ("continue_", "continue"),
        ("step_over", "next"),
        ("step_in", "step_in"),
        ("step_out", "step_out"),
    ],
)
async def test_step_gestures_record(app_cap, gesture, expected):
    app, cap, _ = app_cap
    await app.on_code_view_debug_action(CodeView.DebugAction(gesture))
    assert cap.records == [(expected, [])]


async def test_pause_records(app_cap, monkeypatch):
    app, cap, _ = app_cap

    async def fake_pause(*a, **k):
        return True

    monkeypatch.setattr(app.controller, "pause", fake_pause)
    await app.on_code_view_debug_action(CodeView.DebugAction("pause"))
    assert cap.records == [("pause", [])]


async def test_stack_nav_records_only_on_success(app_cap, monkeypatch):
    app, cap, _ = app_cap
    results = iter([True, False])

    async def fake_nav(up):
        return next(results)

    monkeypatch.setattr(app.controller, "navigate_stack", fake_nav)
    await app.on_code_view_debug_action(CodeView.DebugAction("stack_up"))
    await app.on_code_view_debug_action(CodeView.DebugAction("stack_up"))
    assert cap.records == [("stack_up", [])]  # boundary attempt not recorded


async def test_restart_gesture_records(app_cap, monkeypatch):
    app, cap, _ = app_cap
    # Run the REAL _restart_session (the hook lives at the top of it) but
    # stub the controller-heavy remainder so no session actually starts.
    # app_cap's TdbApp defaults to stop_on_entry=False (a non-entry-stop
    # session), so restart must ALSO record the auto-continue that
    # reproduces "replay always relaunches parked at entry" — same
    # predicate as on_mount's startup dump, via _should_auto_continue.
    monkeypatch.setattr(app.controller, "stop", _noop)
    monkeypatch.setattr(app, "_start_session", lambda: None)
    worker = app._restart_session()
    await worker.wait()
    assert cap.records == [("restart", []), ("continue", [])]


async def test_restart_on_stop_on_entry_app_records_only_restart(monkeypatch):
    cap = CaptureRecorder()
    app = TdbApp(program="", config=TdbConfig(), stop_on_entry=True, recorder=cap)
    async with app.run_test() as pilot:
        await pilot.pause()
        cap.records.clear()  # drop the session-start breakpoint dump
        monkeypatch.setattr(app.controller, "stop", _noop)
        monkeypatch.setattr(app, "_start_session", lambda: None)
        worker = app._restart_session()
        await worker.wait()
        assert cap.records == [("restart", [])]


async def test_restart_unsupported_records_nothing(app_cap, monkeypatch):
    app, cap, _ = app_cap
    # Remote-attach / tdb.breakpoint() sessions can't restart; the guard
    # in _restart_session must run BEFORE any recording so a replay never
    # sees a `restart` it can't execute (action_restart would KeyError on
    # an attach controller's empty _launch_params).
    monkeypatch.setattr(
        type(app.controller), "supports_restart", property(lambda self: False)
    )
    monkeypatch.setattr(app.controller, "stop", _noop)
    monkeypatch.setattr(app, "_start_session", lambda: None)
    worker = app._restart_session()
    await worker.wait()
    assert cap.records == []


async def test_restart_dismisses_live_rust_workspace(app_cap, monkeypatch):
    """Restart cleanup must close the screen as well as forget its reference."""
    app, _, _ = app_cap
    modal = Mock()
    app.panels.rust_concurrency = modal
    monkeypatch.setattr(app.controller, "stop", _noop)
    worker = app._restart_session(start_immediately=False)
    await worker.wait()

    modal.dismiss.assert_called_once_with()
    assert app.panels.rust_concurrency is None


async def test_file_open_restart_not_recorded_but_notifies(
    app_cap, monkeypatch, tmp_path
):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "stop", _noop)
    monkeypatch.setattr(app, "_start_session", lambda: None)
    notes = []
    monkeypatch.setattr(app, "notify", lambda *a, **k: notes.append(a))
    prog = tmp_path / "other.py"
    prog.write_text("x = 1\n")
    worker = app._restart_session(new_program=str(prog), start_immediately=False)
    await worker.wait()
    assert ("restart", []) not in cap.records
    assert notes  # user warned the recording won't reflect File > Open


async def test_quit_records_once(app_cap, monkeypatch):
    app, cap, _ = app_cap
    monkeypatch.setattr(app.controller, "stop", _noop)
    await app.action_quit_debugger()
    await app.action_quit_debugger()  # second press: _is_quitting guard
    assert cap.records.count(("quit", [])) == 1
