"""Detach for --terminal and attach sessions: a real disconnect."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from tdb.session.controller import DetachNotConfirmed
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
    _surviving(monkeypatch, ctrl, True)
    assert ctrl.used_terminal is True
    assert await ctrl.detach_disconnect() is True
    assert fake.calls_to("disconnect") == [("disconnect", False)]


async def test_attach_ignores_survives_disconnect(monkeypatch):
    ctrl, fake, _ = _make()
    ctrl._is_remote_attach = True
    _surviving(monkeypatch, ctrl, False)
    assert await ctrl.detach_disconnect() is True


def test_survives_disconnect_defaults_to_false():
    from tdb.languages.base import AdapterQuirks

    assert AdapterQuirks().survives_disconnect is False


async def test_attach_session_disconnects_without_terminating(capsys):
    ctl = AutoExit(0)
    ctl.is_remote_attach = True
    app = make_app(ctl, [("old\n", "stdout")])
    assert await asyncio.wait_for(_run(app), 5) == 0
    assert ctl.continues == 0  # detach_disconnect resumes the clients itself
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
    assert ctl.continues == 1
    assert ctl.stops == 1  # relay cleaned up when the program exited
    assert "detached" not in capsys.readouterr().err


async def test_unconfirmed_disconnect_is_reported_honestly(capsys):
    ctl = AutoExit(0)
    ctl.is_remote_attach = True
    ctl.confirmed = False
    assert await _run(make_app(ctl)) == 1
    err = capsys.readouterr().err
    assert err == "tdb: detached — could not confirm prog.py is still running\n"
    assert "keeps running" not in err


async def test_detach_disconnect_reports_unacknowledged_disconnect():
    ctrl, fake, _ = _make()
    ctrl._is_remote_attach = True
    fake.fail.add("disconnect")
    with pytest.raises(DetachNotConfirmed):
        await ctrl.detach_disconnect()
    assert fake.calls_to("stop") == [("stop",)]
    assert ctrl.state.phase is SessionPhase.TERMINATED


async def test_stop_still_swallows_disconnect_failure():
    ctrl, fake, _ = _make()
    fake.fail.add("disconnect")
    await ctrl.stop()
    assert ctrl.state.phase is SessionPhase.TERMINATED


async def test_resume_sent_to_every_client_before_close_or_disconnect():
    ctrl, fake, _ = _make()
    ctrl._is_remote_attach = True
    child = _FakeDAP("child")
    ctrl._child_clients[100] = child
    order: list[str] = []

    def tap(client, label):
        real = client.continue_nowait

        async def continue_nowait(tid):
            order.append(f"continue:{label}")
            await real(tid)

        client.continue_nowait = continue_nowait

    tap(fake, "parent")
    tap(child, "child")
    real_close = ctrl._children.close_all

    async def close_all():
        order.append("close")
        await real_close()

    ctrl._children.close_all = close_all
    real_disc = fake.disconnect

    async def disconnect(terminate=True):
        order.append("disconnect")
        await real_disc(terminate)

    fake.disconnect = disconnect
    assert await ctrl.detach_disconnect() is True
    assert order == ["continue:parent", "continue:child", "close", "disconnect"]


async def test_resume_sent_even_without_current_thread_id():
    ctrl, fake, _ = _make(thread_id=None)
    ctrl._is_remote_attach = True
    assert ctrl.state.current_thread_id is None
    await ctrl.detach_disconnect()
    assert fake.calls_to("continue_nowait") or fake.calls_to("continue")


async def _run(app):
    from tdb.detach_relay import run_after_tui

    return await run_after_tui(app, "prog.py")
