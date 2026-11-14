"""TdbApp detach flow: strip stops, save breakpoints, exit without
stopping the controller; availability rules; output capture."""

import pytest

from tdb.app import TdbApp
from tdb.persist import TdbConfig
from tdb.session.controller import DebugController
from tdb.session.event_bus import SwappableEventHandler
from tdb.session.state import SessionPhase
from tdb.widgets.modals import _QuitConfirmModal


@pytest.fixture
def calls(monkeypatch):
    log = {"strip": 0, "stop": 0, "continue": 0}

    async def no_op(self, *a, **k):
        pass

    async def strip(self):
        log["strip"] += 1

    async def stop(self):
        log["stop"] += 1

    async def cont(self, *a, **k):
        log["continue"] += 1

    monkeypatch.setattr(DebugController, "start", no_op)
    monkeypatch.setattr(DebugController, "strip_stops", strip)
    monkeypatch.setattr(DebugController, "stop", stop)
    monkeypatch.setattr(DebugController, "continue_", cont)
    monkeypatch.setattr(
        "tdb.app.save_breakpoints",
        lambda *a, **k: log.setdefault("saved", []).append(a),
    )
    return log


class _Rec:
    def __init__(self):
        self.events = []
        self.on_error = None

    def record(self, name, args):
        self.events.append((name, args))


def _app(program="prog.py"):
    app = TdbApp(program=program, config=TdbConfig(), recorder=_Rec())
    return app


def _ready(app):
    app.controller.state.transition_to(SessionPhase.RUNNING)


async def _settle(pilot, n=15):
    for _ in range(n):
        await pilot.pause()


async def test_detach_flow_strips_saves_and_exits_without_stop(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _ready(app)
        await app._detach_flow()
        await _settle(pilot)
    assert calls["strip"] == 1
    assert len(calls["saved"]) == 1
    assert calls["stop"] == 0
    assert app.run_detached is True
    assert ("detach", []) in app.recorder.events


async def test_detach_unavailable_when_terminated_or_post_mortem_or_adopted(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.detach_available() is False  # not started
        _ready(app)
        assert app.detach_available() is True
        app.controller.state.transition_to(SessionPhase.TERMINATED)
        assert app.detach_available() is False
        app.controller.state.phase = SessionPhase.POST_MORTEM
        assert app.detach_available() is False
        app.controller.state.phase = SessionPhase.RUNNING
        app._startup_error = "boom"
        assert app.detach_available() is False
        app._startup_error = None
        app._replay_driver = object()
        assert app.detach_available() is False
        app._replay_driver = None
        app._adopted = True
        assert app.detach_available() is False
        app._adopted = False
        app._is_quitting = True
        assert app.detach_available() is False
        app._is_quitting = False


async def test_detach_flow_hands_over_a_program_that_ended_under_the_modal(calls):
    """The CLI then exits with the program's own code (run_after_tui)."""
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _ready(app)
        app.controller.state.transition_to(SessionPhase.TERMINATED)
        await app._detach_flow()
        await _settle(pilot)
    assert calls["stop"] == 0
    assert app.run_detached is True


async def test_detach_flow_falls_back_to_quit_when_no_handover_is_possible(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _ready(app)
        app.controller.state.phase = SessionPhase.POST_MORTEM
        await app._detach_flow()
        await _settle(pilot)
    assert calls["stop"] == 1
    assert calls["strip"] == 0
    assert app.run_detached is False


async def test_detach_flow_holds_output_arriving_during_teardown(calls):
    """Right before exit() the controller's events go to a handler that
    keeps program output for run_after_tui instead of the closing app."""
    from tdb.detach_relay import DetachHandoffHandler

    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _ready(app)
        await app._detach_flow()
        handoff = app.event_switch.target
        assert isinstance(handoff, DetachHandoffHandler)
        app.event_switch.on_output("late\n", "stdout")
        app.event_switch.on_exited(4)
        await _settle(pilot)
    assert handoff.release() == [("late\n", "stdout")]
    assert handoff.exited.is_set() and handoff.exit_code == 4
    assert ("late\n", "stdout") not in app.program_output


async def test_detach_while_running_sends_no_continue(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _ready(app)
        assert app.controller.state.is_running
        await app._detach_flow()
        await _settle(pilot)
    assert calls["continue"] == 0


async def test_double_d_runs_flow_once(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _ready(app)
        app.action_confirm_quit()
        await pilot.pause()
        assert isinstance(app.screen, _QuitConfirmModal)
        await pilot.press("d", "d")
        await _settle(pilot)
    assert calls["strip"] == 1
    assert calls["stop"] == 0


async def test_program_output_captured_in_order_and_capped(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        from tdb.session.messages import DapOutput

        def h_on_output(text, cat):
            app.post_message(DapOutput(text, cat))

        h_on_output("a", "stdout")
        await pilot.pause()
        h_on_output("note", "console")
        h_on_output("b", "stderr")
        await pilot.pause()
        assert list(app.program_output) == [("a", "stdout"), ("b", "stderr")]
        assert app.program_output_truncated is False
        chunk = "x" * (1024 * 1024)
        for _ in range(6):
            h_on_output(chunk, "stdout")
            await pilot.pause()
        total = sum(len(t) for t, _ in app.program_output)
        assert app.program_output_truncated is True
        assert total <= 4 * 1024 * 1024


async def test_controller_handler_is_swappable_after_restart(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.event_switch, SwappableEventHandler)
        assert app.event_switch.target is app._event_handler
        old = app.event_switch
        await app._restart_session().wait()
        await _settle(pilot)
        assert app.event_switch is not old
        assert isinstance(app.event_switch, SwappableEventHandler)
        assert app.event_switch.target is app._event_handler
        assert list(app.program_output) == []


async def test_ctrl_q_still_quits_immediately(calls):
    app = _app()
    async with app.run_test() as pilot:
        await pilot.pause()
        _ready(app)
        await pilot.press("ctrl+q")
        await _settle(pilot)
    assert calls["stop"] == 1
    assert calls["strip"] == 0
    assert app.run_detached is False
