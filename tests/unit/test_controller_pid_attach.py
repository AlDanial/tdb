"""Native pid attach (`tdb -a PID` on gdb/lldb-dap): controller behavior.

gdb's `attach PID` reports its stop AFTER the attach response (the reverse
of `target remote`), and the live breakpoint hooks need a hidden function
breakpoint on `tdb_breakpoint_stop` installed before configurationDone.
Exercised here with a stub DAP client; the real adapters are covered by
tests/integration/test_native_breakpoint_hook.py.
"""

from __future__ import annotations

import asyncio

from tdb.dap.messages import Event, Response
from tdb.dap.types import Breakpoint, Capabilities, Thread
from tdb.languages.cpp import HOOK_STOP_FUNCTION, build_cpp_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


class _StubDAPClient:
    def __init__(self, *, stop_after_attach: bool = True, verified: bool = True):
        self.events: dict = {}
        self.reverse_handlers: dict = {}
        self.capabilities = Capabilities()
        self.calls: list[str] = []
        self.function_breakpoints: list[list[str]] = []
        self.resumed = 0
        self._stop_after_attach = stop_after_attach
        self._verified = verified

    def on_event(self, name, fn):
        self.events[name] = fn

    def on_reverse_request(self, name, fn):
        self.reverse_handlers[name] = fn

    async def set_exception_breakpoints(self, filters):
        self.calls.append("set_exception_breakpoints")

    async def set_function_breakpoints(self, names):
        self.calls.append("set_function_breakpoints")
        self.function_breakpoints.append(list(names))
        return [Breakpoint(id=1, verified=self._verified, line=0) for _ in names]

    async def configuration_done(self):
        self.calls.append("configuration_done")

    async def continue_nowait(self, thread_id):
        self.calls.append("continue")
        self.resumed += 1
        return None

    async def threads(self):
        return [Thread(id=1, name="main")]


def _make_controller(pause: bool, client: _StubDAPClient) -> DebugController:
    ctrl = DebugController(
        ServerEventHandler(), profile=build_cpp_profile(attach_pid=4242)
    )
    ctrl.client = client  # type: ignore[assignment]
    ctrl._setup_event_handlers()
    ctrl._is_remote_attach = True
    ctrl._pre_arm_pause = pause
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(
        Response(seq=1, request_seq=1, command="attach", success=True, body={})
    )
    ctrl._launch_future = fut
    return ctrl


def _late_attach_stop(
    ctrl: DebugController, client: _StubDAPClient, delay: float = 0.05
):
    """gdb order: the stopped(attach) event lands after the attach response."""

    async def fire():
        await asyncio.sleep(delay)
        client.events["stopped"](
            Event(seq=2, event="stopped", body={"threadId": 1, "reason": "attach"})
        )

    return asyncio.ensure_future(fire())


async def test_hook_function_breakpoint_installed_before_configuration_done():
    client = _StubDAPClient()
    ctrl = _make_controller(pause=True, client=client)
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert client.function_breakpoints == [[HOOK_STOP_FUNCTION]]
    assert client.calls.index("set_function_breakpoints") < client.calls.index(
        "configuration_done"
    )
    # Hidden: never enters the source-breakpoint table.
    assert ctrl.state.breakpoints == {}


async def test_pid_attach_with_pause_keeps_the_attach_stop():
    client = _StubDAPClient()
    ctrl = _make_controller(pause=True, client=client)
    handler = ctrl.event_handler
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert ctrl.state.phase == SessionPhase.STOPPED
    assert client.resumed == 0
    assert handler.last_stop_reason == "attach"  # reported to the UI


async def test_pid_attach_without_pause_resumes_and_hides_the_attach_stop():
    client = _StubDAPClient()
    ctrl = _make_controller(pause=False, client=client)
    handler = ctrl.event_handler
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert client.resumed == 1
    assert ctrl.state.phase == SessionPhase.RUNNING
    assert (
        handler.last_stop_reason is None
    )  # suppressed: the program is about to stop itself
    assert ctrl._suppress_next_stop is False  # reset for the hook's real stop


async def test_hook_function_breakpoint_unverified_is_not_an_error():
    """Stripped binary: gdb/lldb cannot resolve tdb_breakpoint_stop. Attach
    must still complete; the program simply runs on."""
    client = _StubDAPClient(verified=False)
    ctrl = _make_controller(pause=False, client=client)
    task = _late_attach_stop(ctrl, client)
    await ctrl.do_configure()
    await task
    assert client.resumed == 1


async def test_pid_attach_stop_wait_times_out_without_hanging(monkeypatch):
    """Attach refused (ptrace_scope, container): no stop ever arrives. The
    controller must give up on the wait and leave the session RUNNING, not
    hang forever."""
    import tdb.session.controller as controller_mod

    monkeypatch.setattr(controller_mod, "ATTACH_STOP_TIMEOUT", 0.05)
    client = _StubDAPClient(stop_after_attach=False)
    ctrl = _make_controller(pause=True, client=client)
    await asyncio.wait_for(ctrl.do_configure(), timeout=2)
    assert ctrl.state.phase == SessionPhase.RUNNING
    assert client.resumed == 0


async def test_remote_stub_attach_path_unchanged():
    """`--remote-attach` to gdbserver: stop lands before the response and
    is always resumed; no hook breakpoint (no pid)."""
    client = _StubDAPClient()
    ctrl = DebugController(ServerEventHandler(), profile=build_cpp_profile())
    ctrl.client = client  # type: ignore[assignment]
    ctrl._setup_event_handlers()
    ctrl._is_remote_attach = True
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    fut.set_result(
        Response(seq=1, request_seq=1, command="attach", success=True, body={})
    )
    ctrl._launch_future = fut
    client.events["stopped"](
        Event(seq=2, event="stopped", body={"threadId": 1, "reason": "attach"})
    )
    await ctrl.do_configure()
    assert client.function_breakpoints == []
    assert client.resumed == 1
    assert ctrl.state.phase == SessionPhase.RUNNING
