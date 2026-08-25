"""Unit tests for DAPClient.attach pathMappings serialization.

`--local-root` / `--remote-root` are paired in CLI order and threaded
through `controller.remote_attach` → `client.attach(path_mappings=...)`.
debugpy expects the wire shape `[{"localRoot": L, "remoteRoot": R}, ...]`
in the attach arguments; this file pins that contract.
"""

from __future__ import annotations

import asyncio
from importlib import resources
from unittest.mock import AsyncMock

import pytest

from tdb.dap.client import DAPClient
from tdb.dap.messages import Response
from tdb.dap.types import Capabilities
from tdb.dap.types import Thread
from tdb.languages.rust import RustLldbAdapter
from tdb.languages.rust import build_rust_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


@pytest.mark.asyncio
async def test_attach_omits_path_mappings_when_none():
    client = DAPClient()
    client._send_raw = AsyncMock(return_value=None)
    await client.attach(host="1.2.3.4", port=5678)
    args = client._send_raw.await_args[0][1]
    assert "pathMappings" not in args


@pytest.mark.asyncio
async def test_attach_omits_path_mappings_when_empty_list():
    client = DAPClient()
    client._send_raw = AsyncMock(return_value=None)
    await client.attach(host="1.2.3.4", port=5678, path_mappings=[])
    args = client._send_raw.await_args[0][1]
    assert "pathMappings" not in args


@pytest.mark.asyncio
async def test_attach_emits_single_path_mapping():
    client = DAPClient()
    client._send_raw = AsyncMock(return_value=None)
    await client.attach(
        host="1.2.3.4",
        port=5678,
        path_mappings=[("/local/code", "/srv/code")],
    )
    args = client._send_raw.await_args[0][1]
    assert args["pathMappings"] == [
        {"localRoot": "/local/code", "remoteRoot": "/srv/code"},
    ]


@pytest.mark.asyncio
async def test_attach_emits_multiple_path_mappings_in_order():
    client = DAPClient()
    client._send_raw = AsyncMock(return_value=None)
    await client.attach(
        host="1.2.3.4",
        port=5678,
        path_mappings=[
            ("/local/a", "/srv/A"),
            ("/local/b", "/srv/B"),
        ],
    )
    args = client._send_raw.await_args[0][1]
    assert args["pathMappings"] == [
        {"localRoot": "/local/a", "remoteRoot": "/srv/A"},
        {"localRoot": "/local/b", "remoteRoot": "/srv/B"},
    ]


@pytest.mark.asyncio
async def test_attach_threads_program_to_rust_adapter():
    client = DAPClient(RustLldbAdapter())
    client._send_raw = AsyncMock(return_value=None)

    await client.attach(
        host="devbox",
        port=2345,
        program="/local/app",
        path_mappings=[("/local/src", "/remote/src")],
    )

    args = client._send_raw.await_args[0][1]
    assert args == {
        "program": "/local/app",
        "gdb-remote-host": "devbox",
        "gdb-remote-port": 2345,
        "sourceMap": [["/remote/src", "/local/src"]],
        "initCommands": [
            "command script import "
            f'"{resources.files("tdb.rust_concurrency.probes").joinpath("lldb_script.py")}"'
        ],
    }


def _successful_attach_response() -> asyncio.Future[Response]:
    response: asyncio.Future[Response] = asyncio.get_event_loop().create_future()
    response.set_result(Response(1, 1, "attach", True, None))
    return response


@pytest.mark.asyncio
async def test_rust_gdb_configures_source_mappings_before_breakpoints():
    controller = DebugController(
        ServerEventHandler(), profile=build_rust_profile(adapter="gdb")
    )
    client = AsyncMock()
    client.capabilities = Capabilities()
    controller.client = client
    controller._launch_future = _successful_attach_response()
    controller._is_remote_attach = True
    controller._attach_path_mappings = [("/local src", "/remote src")]

    await controller.do_configure()

    client.evaluate.assert_awaited_once_with(
        'set substitute-path "/remote src" "/local src"', context="repl"
    )
    client.configuration_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_gdb_remote_attach_resumes_inferior_stopped_by_stub():
    """gdb's DAP attach (`target remote`) leaves the inferior stopped at
    the stub's entry point and never resumes it — unlike lldb-dap, which
    resumes on its own after attach. The controller must normalize this:
    attach means "join a running program", so after the attach response
    lands with the debuggee stopped, kick it off."""
    controller = DebugController(
        ServerEventHandler(), profile=build_rust_profile(adapter="gdb")
    )
    client = AsyncMock()
    client.capabilities = Capabilities()
    client.threads.return_value = [Thread(id=1, name="main")]
    controller.client = client
    controller._launch_future = _successful_attach_response()
    controller._is_remote_attach = True
    # The attach stopped event has already landed by the time the
    # deferred attach response resolves.
    controller.state.transition_to(SessionPhase.RUNNING)
    controller.state.transition_to(SessionPhase.STOPPED)

    await controller.do_configure()

    client.continue_nowait.assert_awaited_once_with(1)
    # State must flip eagerly, not wait for gdb's continued event — the
    # debuggee visibly resumes before that event reaches the read loop.
    assert controller.state.phase is SessionPhase.RUNNING


@pytest.mark.asyncio
async def test_lldb_remote_attach_does_not_send_extra_continue():
    """lldb-dap resumes the debuggee itself after attach; the controller
    must not stack a second continue on top."""
    controller = DebugController(
        ServerEventHandler(), profile=build_rust_profile(adapter="lldb-dap")
    )
    client = AsyncMock()
    client.capabilities = Capabilities()
    controller.client = client
    controller._launch_future = _successful_attach_response()
    controller._is_remote_attach = True

    await controller.do_configure()

    client.continue_nowait.assert_not_awaited()


@pytest.mark.asyncio
async def test_rust_gdb_mapping_failure_stops_attach_configuration():
    controller = DebugController(
        ServerEventHandler(), profile=build_rust_profile(adapter="gdb")
    )
    client = AsyncMock()
    client.capabilities = Capabilities()
    client.evaluate.side_effect = RuntimeError("mapping rejected")
    controller.client = client
    controller._launch_future = _successful_attach_response()
    controller._is_remote_attach = True
    controller._attach_path_mappings = [("/local src", "/remote src")]

    with pytest.raises(RuntimeError, match="mapping rejected"):
        await controller.do_configure()

    client.configuration_done.assert_not_awaited()
