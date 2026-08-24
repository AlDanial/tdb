"""remote_attach must spawn the adapter subprocess (client.start) for
adapters with the attach_via_adapter quirk, instead of dialing the
debuggee's DAP port directly (the debugpy path)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from tdb.languages.base import (
    AdapterQuirks,
    AdapterSpec,
    LanguageProfile,
    Presentation,
    ProfileCapabilities,
)
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


class _MediatedAdapter(AdapterSpec):
    id = "mediated"
    quirks = AdapterQuirks(attach_via_adapter=True)

    def command(self):
        return ["true"]

    def launch_body(self, **kw):
        return {"request": "launch", "program": kw.get("program", "")}

    def attach_body(self, *, host, port, opts):
        return {"request": "attach", "host": host, "port": port}

    def pick_exception_filters(self, caps):
        return []


def _profile(quirk: bool) -> LanguageProfile:
    adapter = _MediatedAdapter()
    if not quirk:
        adapter.quirks = AdapterQuirks(attach_via_adapter=False)
    return LanguageProfile(
        id="x",
        display_name="X",
        adapter=adapter,
        presentation=Presentation(),
        capabilities=ProfileCapabilities(),
    )


async def _attach_with(profile: LanguageProfile):
    ctrl = DebugController(ServerEventHandler(), profile=profile)
    ctrl.client.start = AsyncMock()
    ctrl.client.connect = AsyncMock()
    ctrl.client.initialize = AsyncMock()
    ctrl.client.attach = AsyncMock(return_value=None)
    await ctrl.remote_attach(host="devbox", port=5678)
    return ctrl


async def test_quirk_true_spawns_adapter_not_tcp():
    ctrl = await _attach_with(_profile(quirk=True))
    ctrl.client.start.assert_awaited_once()
    ctrl.client.connect.assert_not_awaited()
    ctrl.client.attach.assert_awaited_once()


async def test_quirk_false_keeps_direct_tcp_connect():
    ctrl = await _attach_with(_profile(quirk=False))
    ctrl.client.connect.assert_awaited_once_with("devbox", 5678)
    ctrl.client.start.assert_not_awaited()


async def test_adapter_mediated_attach_forwards_local_program():
    ctrl = DebugController(ServerEventHandler(), profile=_profile(quirk=True))
    ctrl.client.start = AsyncMock()
    ctrl.client.initialize = AsyncMock()
    ctrl.client.attach = AsyncMock(return_value=None)

    await ctrl.remote_attach(host="devbox", port=5678, program="/local/app")

    ctrl.client.attach.assert_awaited_once_with(
        host="devbox", port=5678, path_mappings=None, program="/local/app"
    )
