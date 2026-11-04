"""`remote_attach` runs the adapter's post-attach hook, which injects into
a Python target, and the front ends report what happened."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from tdb import _timeouts
from tdb.app import TdbApp
from tdb.dap.messages import Event
from tdb.languages.python import DebugpyAdapter, build_python_profile
from tdb.languages.python_attach import PidAttachError
from tdb.persist import TdbConfig
from tdb.server.event_handler import ServerEventHandler
from tdb.server.runner import run_headless
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


class _Client:
    def __init__(self, log):
        self.log = log
        self.capabilities = None

    def on_event(self, name, fn):
        pass

    def on_reverse_request(self, name, fn):
        pass

    async def connect(self, host, port):
        self.log.append("connect")

    async def start(self):
        self.log.append("start")

    async def initialize(self):
        self.log.append("initialize")

    async def attach(self, **kw):
        self.log.append("attach")
        return asyncio.get_event_loop().create_future()


class _HookAdapter(DebugpyAdapter):
    def __init__(self, log, result="sys.remote_exec", error=None):
        self.log = log
        self.result = result
        self.error = error
        self.event = None

    async def after_attach_request(self, initialized):
        self.log.append("hook")
        self.event = initialized
        if self.error:
            raise self.error
        return self.result


def _controller(adapter=None, log=None):
    profile = build_python_profile()
    if adapter is not None:
        profile = dataclasses.replace(profile, adapter=adapter)
    ctrl = DebugController(ServerEventHandler(), profile=profile)
    ctrl.client = _Client(log if log is not None else [])  # type: ignore[assignment]
    return ctrl


async def test_hook_runs_after_attach_request_sent():
    log: list[str] = []
    await _controller(_HookAdapter(log), log).remote_attach("127.0.0.1", 0)
    assert log.index("hook") > log.index("attach")


async def test_attach_method_recorded():
    ctrl = _controller(_HookAdapter([]))
    await ctrl.remote_attach("127.0.0.1", 0)
    assert ctrl.attach_method == "sys.remote_exec"


async def test_initialized_event_sets_hook_event():
    adapter = _HookAdapter([])
    ctrl = _controller(adapter)
    await ctrl.remote_attach("127.0.0.1", 0)
    assert not adapter.event.is_set()
    ctrl._on_initialized(Event(seq=1, event="initialized", body={}))
    assert adapter.event.is_set()


async def test_hook_error_propagates():
    ctrl = _controller(_HookAdapter([], error=PidAttachError("nope")))
    with pytest.raises(PidAttachError):
        await ctrl.remote_attach("127.0.0.1", 0)


async def test_default_hook_does_nothing_for_other_adapters():
    ctrl = _controller()
    await ctrl.remote_attach("127.0.0.1", 0)
    assert ctrl.attach_method is None


def _app(monkeypatch, attach):
    monkeypatch.setattr(DebugController, "remote_attach", attach)
    return TdbApp(
        program="",
        config=TdbConfig(),
        attach_host="127.0.0.1",
        attach_port=0,
        profile=build_python_profile(),
    )


async def _settle(pilot, seconds=0.0):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        await pilot.pause()
        if asyncio.get_running_loop().time() >= deadline:
            return
        await asyncio.sleep(0.02)


async def test_app_reports_injection_failure(monkeypatch):
    async def attach(self, **kw):
        raise PidAttachError("cannot attach to pid 4242: nope")

    app = _app(monkeypatch, attach)
    async with app.run_test() as pilot:
        for _ in range(20):
            if app._startup_error is not None:
                break
            await pilot.pause()
    assert app._startup_error == "tdb: cannot attach to pid 4242: nope"
    assert app.return_code == 2


def _record_notices(app, monkeypatch):
    notices: list[str] = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notices.append(msg))
    return notices


async def test_app_warns_when_program_stays_running(monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_BLOCKED_NOTICE", 0.05)

    async def attach(self, **kw):
        self.attach_method = "debugpy injection"
        self.state.transition_to(SessionPhase.RUNNING)

    app = _app(monkeypatch, attach)
    notices = _record_notices(app, monkeypatch)
    async with app.run_test() as pilot:
        await _settle(pilot, 0.3)
    assert any("debugpy injection" in n for n in notices)
    assert any("pause" in n for n in notices)


async def test_app_no_warning_once_stopped(monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_BLOCKED_NOTICE", 0.05)

    async def attach(self, **kw):
        self.attach_method = "sys.remote_exec"
        self.state.transition_to(SessionPhase.STOPPED)

    app = _app(monkeypatch, attach)
    notices = _record_notices(app, monkeypatch)
    async with app.run_test() as pilot:
        await _settle(pilot, 0.3)
    assert not any("pause" in n for n in notices)


async def test_app_no_warning_without_pause_on_attach(monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_BLOCKED_NOTICE", 0.05)

    async def attach(self, **kw):
        self.attach_method = "sys.remote_exec"
        self.state.transition_to(SessionPhase.RUNNING)

    app = _app(monkeypatch, attach)
    app._pause_on_attach = False
    notices = _record_notices(app, monkeypatch)
    async with app.run_test() as pilot:
        await _settle(pilot, 0.3)
    assert not any("pause" in n for n in notices)


async def test_headless_reports_injection_failure(monkeypatch, capsys):
    async def attach(self, **kw):
        raise PidAttachError("cannot attach to pid 4242: nope")

    monkeypatch.setattr(DebugController, "remote_attach", attach)
    with pytest.raises(SystemExit) as exc_info:
        await run_headless(
            program=None,
            attach_host="127.0.0.1",
            attach_port=0,
            profile=build_python_profile(),
        )
    assert exc_info.value.code == 2
    assert "tdb: cannot attach to pid 4242: nope" in capsys.readouterr().err
