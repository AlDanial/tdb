"""run_after_tui: the handover from the closed TUI to the relay."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tdb.detach_relay import run_after_tui
from tdb.run_mode import ConsoleRunHandler

from .test_detach_relay import FakeController

NOTICE = "tdb: detached — prog.py running; Ctrl-C interrupts it"


class FakeSwitch:
    def __init__(self):
        self.target = None

    def retarget(self, handler):
        self.target = handler


class AutoExit(FakeController):
    """Exits the program shortly after a continue."""

    def __init__(self, code=0, **state):
        super().__init__()
        self.state = SimpleNamespace(
            is_running=state.get("is_running", False),
            is_terminated=state.get("is_terminated", False),
            last_exit_code=state.get("last_exit_code"),
        )
        self.code = code

    async def continue_(self):
        await super().continue_()
        self.state.is_running = True
        sw = self.switch
        asyncio.get_running_loop().call_later(0.02, sw.target.on_exited, self.code)


def make_app(ctl, output=(), truncated=False):
    ctl.switch = FakeSwitch()
    return SimpleNamespace(
        controller=ctl,
        event_switch=ctl.switch,
        program_output=list(output),
        program_output_truncated=truncated,
    )


async def test_retargets_then_replays_output_then_notice(capsys):
    app = make_app(AutoExit(3), [("out1\n", "stdout"), ("err1\n", "stderr")])
    assert await run_after_tui(app, "prog.py") == 3
    assert isinstance(app.event_switch.target, ConsoleRunHandler)
    cap = capsys.readouterr()
    assert cap.out == "out1\n"
    assert cap.err == "err1\n" + NOTICE + "\n"


async def test_truncation_note_printed(capsys):
    app = make_app(AutoExit(0), [("x\n", "stdout")], truncated=True)
    await run_after_tui(app, "prog.py")
    err = capsys.readouterr().err
    assert err.startswith("tdb: earlier program output truncated\n")


async def test_continues_only_when_stopped():
    stopped = AutoExit(0)
    await run_after_tui(make_app(stopped), "p")
    assert stopped.continues == 1

    running = AutoExit(0, is_running=True)
    running.switch = FakeSwitch()
    app = make_app(running)
    asyncio.get_running_loop().call_later(0.05, lambda: app.event_switch.target.on_exited(0))
    await run_after_tui(app, "p")
    assert running.continues == 0


async def test_program_already_ended_returns_its_code():
    ctl = AutoExit(is_terminated=True, last_exit_code=9)
    assert await asyncio.wait_for(run_after_tui(make_app(ctl), "p"), 5) == 9
    assert ctl.continues == 0

    ctl = AutoExit(is_terminated=True)
    assert await asyncio.wait_for(run_after_tui(make_app(ctl), "p"), 5) == 1


async def test_relay_error_stops_session(monkeypatch):
    ctl = AutoExit(0)

    async def boom(*a, **k):
        raise RuntimeError("relay broke")

    monkeypatch.setattr("tdb.detach_relay.relay", boom)
    with pytest.raises(RuntimeError, match="relay broke"):
        await run_after_tui(make_app(ctl), "p")
    assert ctl.stops == 1
