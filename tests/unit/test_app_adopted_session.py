"""Adopted-session TUI episodes (tdb --run): the app reuses a live
controller, retargets the swappable handler, routes EVERY quit path
through the detach/terminate dialog, and never calls controller.stop()
on detach (the debuggee must keep running)."""

import pytest

from tdb.app import TdbApp
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
