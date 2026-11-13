"""Detach for --terminal and attach sessions: a real disconnect."""

from __future__ import annotations

import asyncio
import dataclasses

from tdb.session.state import SessionPhase

from .test_cli_detach_handover import AutoExit, make_app
from tests.unit.test_controller_actions import _FakeDAP, _make


def _surviving(monkeypatch, ctrl, value):
    adapter = ctrl.profile.adapter
    monkeypatch.setattr(
        adapter, "quirks", dataclasses.replace(adapter.quirks, survives_disconnect=value)
    )


async def test_detach_disconnect_closes_children_first():
    ctrl, fake, _ = _make()
    ctrl._is_remote_attach = True
    order: list[str] = []
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    real_close = ctrl._children.close_all

    async def close_all():
        order.append("children")
        await real_close()

    ctrl._children.close_all = close_all
    real_disconnect = fake.disconnect

    async def disconnect(terminate=True):
        order.append("disconnect")
        await real_disconnect(terminate)

    fake.disconnect = disconnect
    assert await ctrl.detach_disconnect() is True
    assert order == ["children", "disconnect"]
    assert fake.calls_to("disconnect") == [("disconnect", False)]
    assert fake.calls_to("stop") == [("stop",)]
    assert ctrl.state.phase is SessionPhase.TERMINATED


async def test_detach_disconnect_refuses_when_adapter_does_not_survive(monkeypatch):
    ctrl, fake, _ = _make()
    ctrl._terminal = "xterm"
    _surviving(monkeypatch, ctrl, False)
    assert await ctrl.detach_disconnect() is False
    assert fake.calls == []
    assert ctrl.state.phase is not SessionPhase.TERMINATED


async def test_detach_disconnect_terminal_surviving_adapter(monkeypatch):
    ctrl, fake, _ = _make()
    ctrl._terminal = "xterm"
    assert ctrl.used_terminal is True
    assert await ctrl.detach_disconnect() is True
    assert fake.calls_to("disconnect") == [("disconnect", False)]


async def test_attach_ignores_survives_disconnect(monkeypatch):
    ctrl, fake, _ = _make()
    ctrl._is_remote_attach = True
    _surviving(monkeypatch, ctrl, False)
    assert await ctrl.detach_disconnect() is True


def test_quirk_false_for_unverified_terminal_adapters():
    from tdb.languages import bash, perl, powershell, ruby, tcsh

    for mod, cls in [
        (perl, "PerlAdapter"),
        (bash, "BashAdapter"),
        (tcsh, "TcshAdapter"),
        (ruby, "RdbgAdapter"),
        (powershell, "PsesAdapter"),
    ]:
        adapter = getattr(mod, cls)
        assert adapter.quirks.survives_disconnect is False, cls


async def test_attach_session_disconnects_without_terminating(capsys):
    ctl = AutoExit(0)
    ctl.is_remote_attach = True
    app = make_app(ctl, [("old\n", "stdout")])
    assert await asyncio.wait_for(_run(app), 5) == 0
    assert ctl.continues == 1
    assert ctl.disconnects == 1
    assert ctl.stops == 0
    cap = capsys.readouterr()
    assert cap.out == ""  # no output replay
    assert cap.err == "tdb: detached — prog.py keeps running\n"


async def test_terminal_session_non_surviving_adapter_falls_back_to_relay(capsys):
    ctl = AutoExit(5)
    ctl.used_terminal = True
    ctl.survives = False
    app = make_app(ctl)
    assert await asyncio.wait_for(_run(app), 5) == 5
    assert ctl.disconnects == 0
    assert ctl.stops == 1  # relay cleaned up when the program exited
    assert "detached" not in capsys.readouterr().err


async def test_running_attach_session_is_not_continued():
    ctl = AutoExit(0, is_running=True)
    ctl.is_remote_attach = True
    assert await _run(make_app(ctl)) == 0
    assert ctl.continues == 0
    assert ctl.disconnects == 1


async def _run(app):
    from tdb.detach_relay import run_after_tui

    return await run_after_tui(app, "prog.py")
