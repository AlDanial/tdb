"""Adopted-session TUI episodes (tdb --run): the app reuses a live
controller, retargets the swappable handler, routes EVERY quit path
through the detach/terminate dialog, and never calls controller.stop()
on detach (the debuggee must keep running)."""

import pytest

from tdb.app import TdbApp
from tdb.dap.types import SourceBreakpoint
from tdb.persist import TdbConfig
from tdb.run_mode import ConsoleRunHandler
from tdb.session.controller import DebugController
from tdb.session.event_bus import SwappableEventHandler
from tdb.widgets.modals import _DetachQuitModal


@pytest.fixture
def adopted(monkeypatch):
    console = ConsoleRunHandler()
    handler = SwappableEventHandler(console)
    controller = DebugController(handler)
    controller.adopted_session = True
    controller.state.enter_stop(1, "pause")
    stops = []

    async def fake_fetch_stop_info():
        stops.append(True)

    async def fake_push_all_breakpoints():
        pass

    stopped_calls = []

    async def fake_stop():
        stopped_calls.append(True)

    monkeypatch.setattr(controller, "fetch_stop_info", fake_fetch_stop_info)
    monkeypatch.setattr(controller, "push_all_breakpoints", fake_push_all_breakpoints)
    monkeypatch.setattr(controller, "stop", fake_stop)
    app = TdbApp(
        program="",
        config=TdbConfig(),
        profile=controller.profile,
        adopted_controller=controller,
        adopted_handler=handler,
        adopted_stop=(1, "pause", None, None),
    )
    return app, handler, controller, stopped_calls


async def test_adoption_retargets_handler_and_shows_stop(adopted):
    app, handler, controller, _ = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.controller is controller
        assert handler.target is app._textual_handler
    assert app.detach_and_resume is False


async def test_q_detach_path_keeps_debuggee_alive(adopted):
    app, handler, controller, stopped_calls = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_confirm_quit()
        await pilot.pause()
        assert isinstance(app.screen, _DetachQuitModal)
        await pilot.press("d")
        for _ in range(20):
            await pilot.pause()
    assert app.detach_and_resume is True
    assert stopped_calls == []  # detach must NOT stop the controller


async def test_ctrl_q_routes_to_dialog_and_terminate_stops(adopted):
    app, handler, controller, stopped_calls = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert isinstance(app.screen, _DetachQuitModal)
        await pilot.press("t")
        for _ in range(20):
            await pilot.pause()
    assert app.detach_and_resume is False
    assert stopped_calls == [True]


async def test_escape_cancels_dialog(adopted):
    app, handler, controller, stopped_calls = adopted
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_confirm_quit()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, _DetachQuitModal)
        assert app._is_quitting is False
    assert stopped_calls == []


def _make_adopted_app(monkeypatch, program: str, pre_existing_breakpoints: dict):
    """Build an adopted TdbApp with a real `program` path (so on_mount's
    program_key is non-empty and load_breakpoints gets called) and a
    given starting `controller.state.breakpoints`."""
    console = ConsoleRunHandler()
    handler = SwappableEventHandler(console)
    controller = DebugController(handler)
    controller.adopted_session = True
    controller.state.enter_stop(1, "pause")
    controller.state.breakpoints = pre_existing_breakpoints

    async def fake_fetch_stop_info():
        pass

    async def fake_push_all_breakpoints():
        pass

    async def fake_stop():
        pass

    monkeypatch.setattr(controller, "fetch_stop_info", fake_fetch_stop_info)
    monkeypatch.setattr(controller, "push_all_breakpoints", fake_push_all_breakpoints)
    monkeypatch.setattr(controller, "stop", fake_stop)
    app = TdbApp(
        program=program,
        config=TdbConfig(),
        profile=controller.profile,
        adopted_controller=controller,
        adopted_handler=handler,
        adopted_stop=(1, "pause", None, None),
    )
    return app, controller


async def test_episode2_does_not_remerge_saved_breakpoints(monkeypatch, tmp_path):
    """Episode 2+ inherits live state from episode 1: on_mount's adopted
    branch must NOT overwrite/merge already-populated
    controller.state.breakpoints with whatever is on disk."""
    program = str(tmp_path / "prog.py")
    live_set = {"prog.py": [SourceBreakpoint(line=5)]}
    disk_set = {"other.py": [SourceBreakpoint(line=99)]}

    monkeypatch.setattr("tdb.app.load_breakpoints", lambda program: disk_set)

    app, controller = _make_adopted_app(monkeypatch, program, dict(live_set))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert controller.state.breakpoints == live_set


async def test_episode1_loads_saved_breakpoints_when_state_empty(monkeypatch, tmp_path):
    """With no pre-existing live breakpoints (episode 1 / first adoption),
    saved (persisted) breakpoints ARE loaded into state."""
    program = str(tmp_path / "prog.py")
    disk_set = {"other.py": [SourceBreakpoint(line=99)]}

    monkeypatch.setattr("tdb.app.load_breakpoints", lambda program: disk_set)

    app, controller = _make_adopted_app(monkeypatch, program, {})
    async with app.run_test() as pilot:
        await pilot.pause()
        assert controller.state.breakpoints == disk_set
