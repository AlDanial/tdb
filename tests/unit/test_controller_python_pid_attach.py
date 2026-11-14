"""`remote_attach` runs the adapter's post-attach hook, which injects into
a Python target, and the front ends report what happened."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from tdb import _timeouts
from tdb.app import TdbApp
from tdb.dap.messages import Event, Response
from tdb.dap.types import Thread
from tdb.languages.python import (
    DebugpyAdapter,
    PidAttachDebugpyAdapter,
    build_python_profile,
)
from tdb.languages.python_attach import PidAttachCancelled, PidAttachError
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

    async def stop(self):
        self.log.append("stop")

    async def disconnect(self, terminate=True):
        self.log.append("disconnect")

    async def initialize(self):
        self.log.append("initialize")

    async def attach(self, **kw):
        self.log.append("attach")
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(
            Response(seq=1, request_seq=1, command="attach", success=True, body={})
        )
        return fut

    async def set_exception_breakpoints(self, filters):
        pass

    async def configuration_done(self):
        self.log.append("configurationDone")

    async def threads(self):
        return [Thread(id=1, name="main")]

    async def pause(self, thread_id):
        self.log.append("pause")


class _HookAdapter(DebugpyAdapter):
    """Stands in for injection: connects, then runs until told to finish."""

    quirks = PidAttachDebugpyAdapter.quirks

    def __init__(self, log, result="sys.remote_exec", error=None):
        self.log = log
        self.result = result
        self.error = error
        self.finish = asyncio.Event()
        self.cancelled = False

    async def after_attach_request(self, initialized, quitting):
        self.log.append("hook")
        if self.error:
            raise self.error
        try:
            await initialized.wait()
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.log.append("hook done")
        return self.result


def _controller(adapter=None, log=None):
    profile = build_python_profile()
    if adapter is not None:
        profile = dataclasses.replace(profile, adapter=adapter)
    ctrl = DebugController(ServerEventHandler(), profile=profile)
    ctrl.client = _Client(log if log is not None else [])  # type: ignore[assignment]
    return ctrl


async def _attached(ctrl, **kw):
    """remote_attach, with `initialized` arriving as it would."""
    task = asyncio.create_task(ctrl.remote_attach("127.0.0.1", 0, **kw))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    ctrl._on_initialized(Event(seq=1, event="initialized", body={}))
    await task


async def test_hook_runs_after_attach_request_sent():
    log: list[str] = []
    adapter = _HookAdapter(log)
    await _attached(_controller(adapter, log))
    assert log.index("hook") > log.index("attach")


async def test_remote_attach_returns_at_initialized_not_at_hook_end():
    # Headless calls do_configure after remote_attach, and the helper can't
    # finish until configurationDone: waiting here would deadlock.
    log: list[str] = []
    await asyncio.wait_for(_attached(_controller(_HookAdapter(log), log)), 2)
    assert "hook done" not in log


async def test_do_configure_waits_for_injection_then_pauses():
    log: list[str] = []
    adapter = _HookAdapter(log)
    ctrl = _controller(adapter, log)
    await _attached(ctrl)
    configure = asyncio.create_task(ctrl.do_configure())
    while "configurationDone" not in log:
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    assert "pause" not in log
    adapter.finish.set()
    await configure
    assert ctrl.attach_method == "sys.remote_exec"
    assert log.index("pause") > log.index("hook done")


async def test_no_pause_before_configuration_done():
    log: list[str] = []
    adapter = _HookAdapter(log)
    adapter.finish.set()
    ctrl = _controller(adapter, log)
    await _attached(ctrl)
    await ctrl.do_configure()
    assert log.index("pause") > log.index("configurationDone")


async def test_no_pause_when_pause_on_attach_is_off():
    log: list[str] = []
    adapter = _HookAdapter(log)
    adapter.finish.set()
    ctrl = _controller(adapter, log)
    await _attached(ctrl, pre_arm_pause=False)
    await ctrl.do_configure()
    assert "pause" not in log
    assert ctrl.attach_method == "sys.remote_exec"


async def test_hook_error_propagates_and_stops_adapter():
    log: list[str] = []
    ctrl = _controller(_HookAdapter(log, error=PidAttachError("nope")), log)
    with pytest.raises(PidAttachError):
        await ctrl.remote_attach("127.0.0.1", 0)
    assert "stop" in log


async def test_unresponsive_target_fails_clearly(monkeypatch):
    # A target already running debugpy loses its server to the injection
    # and answers nothing; a session there would only time out, slowly.
    monkeypatch.setattr(_timeouts, "PID_ATTACH_RESPONSE", 0.05)
    log: list[str] = []
    adapter = _HookAdapter(log)
    adapter.finish.set()
    ctrl = _controller(adapter, log)

    async def silent():
        await asyncio.sleep(60)

    ctrl.client.threads = silent
    await _attached(ctrl)
    with pytest.raises(PidAttachError, match="debugpy"):
        await ctrl.do_configure()
    assert "stop" in log


async def test_unanswered_pause_fails_clearly(monkeypatch):
    # The broken session can answer `threads` and still ignore a pause.
    # A blocked program answers the pause even though it can't stop.
    monkeypatch.setattr(_timeouts, "PID_ATTACH_RESPONSE", 0.05)
    log: list[str] = []
    adapter = _HookAdapter(log)
    adapter.finish.set()
    ctrl = _controller(adapter, log)

    async def silent(thread_id):
        await asyncio.sleep(60)

    ctrl.client.pause = silent
    await _attached(ctrl)
    with pytest.raises(PidAttachError, match="debugpy"):
        await ctrl.do_configure()
    assert "stop" in log


async def test_responsiveness_is_checked_after_the_pause():
    # A broken target answers `threads` at first and goes silent at the
    # pause; checking before the pause would miss it.
    log: list[str] = []
    adapter = _HookAdapter(log)
    adapter.finish.set()
    ctrl = _controller(adapter, log)
    answer = ctrl.client.threads

    async def threads():
        log.append("threads")
        return await answer()

    ctrl.client.threads = threads
    await _attached(ctrl)
    await ctrl.do_configure()
    assert log[-1] == "threads"
    assert "pause" in log


async def test_stop_before_target_connects_gives_up_after_the_limit(monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_QUIT", 0.05)
    log: list[str] = []
    adapter = _HookAdapter(log)
    ctrl = _controller(adapter, log)
    task = asyncio.create_task(ctrl.remote_attach("127.0.0.1", 0))
    await asyncio.sleep(0.01)
    await ctrl.stop()
    assert adapter.cancelled
    assert "configurationDone" not in log
    await asyncio.wait({task}, timeout=1)


async def test_stop_mid_handshake_finishes_it_before_disconnecting(monkeypatch):
    # pydevd waits for configurationDone with no timeout: dropping the
    # adapter here would leave the program frozen for good.
    log: list[str] = []
    adapter = _HookAdapter(log)
    ctrl = _controller(adapter, log)
    await _attached(ctrl)
    asyncio.get_running_loop().call_later(0.05, adapter.finish.set)
    await ctrl.stop()
    assert log.index("configurationDone") < log.index("hook done") < log.index("stop")
    assert not adapter.cancelled


async def test_stop_finishes_handshake_when_hook_already_returned():
    # remote_exec's hook returns at `initialized`, before configurationDone.
    log: list[str] = []
    adapter = _HookAdapter(log)
    adapter.finish.set()
    ctrl = _controller(adapter, log)
    await _attached(ctrl)
    await asyncio.sleep(0)
    assert ctrl._attach_hook.done()
    ctrl._launch_future = asyncio.get_running_loop().create_future()
    await_response = asyncio.get_running_loop().call_later(
        0.05,
        ctrl._launch_future.set_result,
        Response(seq=1, request_seq=1, command="attach", success=True, body={}),
    )
    await ctrl.stop()
    await_response.cancel()
    assert log.index("configurationDone") < log.index("disconnect")


async def test_stop_waits_for_attach_response_before_disconnecting():
    # The response is the only sign the target took configurationDone.
    log: list[str] = []
    adapter = _HookAdapter(log)
    adapter.finish.set()
    ctrl = _controller(adapter, log)
    await _attached(ctrl)
    pending = asyncio.get_running_loop().create_future()
    ctrl._launch_future = pending

    def respond():
        log.append("attach response")
        pending.set_result(
            Response(seq=1, request_seq=1, command="attach", success=True, body={})
        )

    asyncio.get_running_loop().call_later(0.05, respond)
    await ctrl.stop()
    assert log.index("attach response") < log.index("disconnect")


async def test_restart_sends_configuration_done_again():
    # A restarted session reuses the controller, with no attach hook.
    log: list[str] = []
    ctrl = _controller(log=log)
    ctrl._launch_future = asyncio.get_running_loop().create_future()
    ctrl._launch_future.set_result(
        Response(seq=1, request_seq=1, command="launch", success=True, body={})
    )
    await ctrl.do_configure()
    await ctrl.do_configure()
    assert log.count("configurationDone") == 2


async def test_stop_does_not_repeat_configuration_done():
    log: list[str] = []
    adapter = _HookAdapter(log)
    ctrl = _controller(adapter, log)
    await _attached(ctrl)
    configure = asyncio.create_task(ctrl.do_configure())
    while "configurationDone" not in log:
        await asyncio.sleep(0)
    asyncio.get_running_loop().call_later(0.05, adapter.finish.set)
    await ctrl.stop()
    await configure
    assert log.count("configurationDone") == 1


async def test_no_pause_when_quitting_mid_injection():
    log: list[str] = []
    adapter = _HookAdapter(log)
    ctrl = _controller(adapter, log)
    await _attached(ctrl)
    configure = asyncio.create_task(ctrl.do_configure())
    while "configurationDone" not in log:
        await asyncio.sleep(0)
    asyncio.get_running_loop().call_later(0.05, adapter.finish.set)
    await ctrl.stop()
    await configure
    assert "pause" not in log


async def test_default_hook_does_nothing_for_other_adapters():
    log: list[str] = []
    ctrl = _controller(log=log)
    await asyncio.wait_for(ctrl.remote_attach("127.0.0.1", 0), 2)
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


async def test_app_exits_quietly_if_the_user_quit_during_injection(monkeypatch):
    async def attach(self, *args, **kwargs):
        raise PidAttachCancelled("cannot attach to pid 4242: quitting")

    app = _app(monkeypatch, attach)
    async with app.run_test() as pilot:
        await pilot.pause()
    assert app._startup_error is None
    assert not app.return_code


def _record_notices(app, monkeypatch):
    notices: list[str] = []
    monkeypatch.setattr(app, "notify", lambda msg, **kw: notices.append(msg))
    return notices


async def _announce(monkeypatch, method, phase, pause_on_attach=True):
    """Notices the app shows after a session starts in `phase`."""
    monkeypatch.setattr(_timeouts, "PID_ATTACH_BLOCKED_NOTICE", 0.05)

    async def attach(self, **kw):
        pass

    app = _app(monkeypatch, attach)
    app._pause_on_attach = pause_on_attach
    notices = _record_notices(app, monkeypatch)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.controller.attach_method = method
        app.controller.state.transition_to(phase)
        app.announce_pid_attach()
        await _settle(pilot, 0.3)
    return notices


async def test_app_warns_when_program_stays_running(monkeypatch):
    notices = await _announce(monkeypatch, "debugpy injection", SessionPhase.RUNNING)
    assert any("debugpy injection" in n for n in notices)
    assert any("pause" in n for n in notices)


async def test_app_no_warning_once_stopped(monkeypatch):
    notices = await _announce(monkeypatch, "sys.remote_exec", SessionPhase.STOPPED)
    assert not any("pause" in n for n in notices)


async def test_app_no_warning_without_pause_on_attach(monkeypatch):
    notices = await _announce(
        monkeypatch, "sys.remote_exec", SessionPhase.RUNNING, pause_on_attach=False
    )
    assert not any("pause" in n for n in notices)


async def test_app_silent_for_ordinary_sessions(monkeypatch):
    notices = await _announce(monkeypatch, None, SessionPhase.RUNNING)
    assert not any("Attached using" in n for n in notices)


async def test_app_reports_failure_during_configure(monkeypatch):
    async def attach(self, **kw):
        pass

    async def configure(self):
        raise PidAttachError("cannot attach to pid 4242: silent")

    app = _app(monkeypatch, attach)
    monkeypatch.setattr(DebugController, "do_configure", configure)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._dap.do_configure()
        await pilot.pause()
    assert app._startup_error == "tdb: cannot attach to pid 4242: silent"
    assert app.return_code == 2


async def test_headless_reports_failure_during_configure(monkeypatch, capsys):
    async def attach(self, **kw):
        pass

    async def configure(self):
        raise PidAttachError("cannot attach to pid 4242: silent")

    monkeypatch.setattr(DebugController, "remote_attach", attach)
    monkeypatch.setattr(DebugController, "do_configure", configure)
    handler = ServerEventHandler()
    handler.initialized_event.set()
    monkeypatch.setattr("tdb.server.runner.ServerEventHandler", lambda: handler)
    with pytest.raises(SystemExit) as exc_info:
        await run_headless(
            program=None,
            attach_host="127.0.0.1",
            attach_port=0,
            profile=build_python_profile(),
            port=0,
        )
    assert exc_info.value.code == 2
    assert "tdb: cannot attach to pid 4242: silent" in capsys.readouterr().err


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
            port=0,
        )
    assert exc_info.value.code == 2
    assert "tdb: cannot attach to pid 4242: nope" in capsys.readouterr().err
