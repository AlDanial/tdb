"""Regression: remote-attach may opt out of the pre-armed `pause`.

`do_configure` pre-arms a `pause` before `configurationDone` so that a
plain `debugpy.listen()` + `wait_for_client()` program stops at its first
statement (debugpy ignores `stopOnEntry` for attach). That is wrong for a
debuggee that will stop on its own — `tdb.breakpoint()` calls
`debugpy.breakpoint()` right after `wait_for_client()` returns. The
pre-armed pause can land *inside* `debugpy.breakpoint()` (in the stdlib
frames it runs through after its "is a client connected?" check but
before it arms its own step). The user sees a normal-looking stop; on
quit, `disconnect` resumes the thread, `debugpy.breakpoint()` finishes
arming, and the thread suspends a second time with no client left to
resume it. The program hangs forever.

Observed 3/3 runs on Python 3.12 (sys.monitoring) in a RHEL 8.10
container; timing-dependent elsewhere.

`remote_attach(pre_arm_pause=False)` turns the pause off; the hook passes
`--no-pause-on-attach` to get it.
"""

from __future__ import annotations

import asyncio

from tdb.dap.messages import Response
from tdb.dap.types import Capabilities
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


class _StubDAPClient:
    """DAP-client surface exercised by remote_attach + do_configure."""

    def __init__(self) -> None:
        self.capabilities = Capabilities()
        self.pause_calls: list[int] = []

    def on_event(self, name, fn):
        return None

    def on_reverse_request(self, name, fn):
        return None

    async def connect(self, host, port):
        return None

    async def initialize(self):
        return None

    async def attach(self, **kwargs):
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result(
            Response(seq=1, request_seq=1, command="attach", success=True, body={})
        )
        return fut

    async def set_exception_breakpoints(self, filters):
        return None

    async def threads(self):
        from types import SimpleNamespace

        return [SimpleNamespace(id=1, name="MainThread")]

    async def pause(self, thread_id):
        self.pause_calls.append(thread_id)

    async def configuration_done(self):
        return None


def _make_controller() -> tuple[DebugController, _StubDAPClient]:
    ctrl = DebugController(ServerEventHandler())
    client = _StubDAPClient()
    ctrl.client = client  # type: ignore[assignment]
    return ctrl, client


async def test_remote_attach_pre_arms_pause_by_default():
    """Plain remote attach: the debuggee won't stop on its own, so we pause."""
    ctrl, client = _make_controller()
    assert ctrl.profile.adapter.quirks.pre_arm_pause_on_attach
    await ctrl.remote_attach("127.0.0.1", 5678)
    await ctrl.do_configure()
    assert client.pause_calls == [1]


async def test_remote_attach_can_opt_out_of_pre_armed_pause():
    """tdb.breakpoint() sessions must NOT get the pause: it races the
    hook's own debugpy.breakpoint() and orphans the thread on quit."""
    ctrl, client = _make_controller()
    await ctrl.remote_attach("127.0.0.1", 5678, pre_arm_pause=False)
    await ctrl.do_configure()
    assert client.pause_calls == [], (
        "pre-armed pause sent despite pre_arm_pause=False; a "
        "tdb.breakpoint() debuggee can re-suspend after disconnect"
    )
