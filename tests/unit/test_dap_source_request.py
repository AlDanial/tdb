"""Unit tests for DAPClient.source and controller.fetch_source_content.

The DAP `source` request is the fallback path when a stack-frame's file
isn't on the tdb host (typical `tdb -r HOST:PORT` against a debuggee on
another machine). The client method must serialise the request shape
debugpy expects, and the controller wrapper must short-circuit on
post-mortem / no-client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tdb.dap.client import DAPClient
from tdb.dap.messages import Response
from tdb.dap.types import Source
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase


def _resp(body: dict) -> Response:
    return Response(
        seq=1,
        request_seq=1,
        success=True,
        command="source",
        body=body,
    )


# ---- DAPClient.source ---------------------------------------------------


@pytest.mark.asyncio
async def test_source_request_carries_reference_only_when_no_source_object():
    client = DAPClient()
    client._send = AsyncMock(return_value=_resp({"content": "print(1)\n"}))
    text = await client.source(source_reference=42)
    assert text == "print(1)\n"
    client._send.assert_awaited_once_with("source", {"sourceReference": 42})


@pytest.mark.asyncio
async def test_source_request_carries_full_source_object_when_provided():
    client = DAPClient()
    client._send = AsyncMock(return_value=_resp({"content": "x = 1\n"}))
    src = Source(path="/remote/app.py", name="app.py", source_reference=0)
    text = await client.source(0, src)
    assert text == "x = 1\n"
    client._send.assert_awaited_once_with(
        "source",
        {
            "sourceReference": 0,
            "source": {
                "sourceReference": 0,
                "path": "/remote/app.py",
                "name": "app.py",
            },
        },
    )


@pytest.mark.asyncio
async def test_source_request_omits_path_when_none():
    client = DAPClient()
    client._send = AsyncMock(return_value=_resp({"content": "..."}))
    src = Source(path=None, name=None, source_reference=99)
    await client.source(99, src)
    args = client._send.await_args[0][1]
    assert args == {
        "sourceReference": 99,
        "source": {"sourceReference": 99},
    }


@pytest.mark.asyncio
async def test_source_returns_empty_string_on_exception():
    client = DAPClient()
    client._send = AsyncMock(side_effect=RuntimeError("not supported"))
    text = await client.source(7)
    assert text == ""


@pytest.mark.asyncio
async def test_source_returns_empty_string_when_body_has_no_content():
    client = DAPClient()
    client._send = AsyncMock(return_value=_resp({}))
    text = await client.source(7)
    assert text == ""


@pytest.mark.asyncio
async def test_source_normalises_null_content_to_empty():
    # Some adapters return {"content": null} on missing files.
    client = DAPClient()
    client._send = AsyncMock(return_value=_resp({"content": None}))
    text = await client.source(7)
    assert text == ""


# ---- Controller.fetch_source_content ------------------------------------


def _controller() -> DebugController:
    return DebugController(ServerEventHandler())


@pytest.mark.asyncio
async def test_fetch_source_content_returns_empty_in_post_mortem():
    ctrl = _controller()
    ctrl.state.transition_to(SessionPhase.POST_MORTEM)
    # Even with a mock client, post-mortem short-circuits.
    ctrl._active_client = DAPClient()
    ctrl._active_client.source = AsyncMock(return_value="never called")
    text = await ctrl.fetch_source_content(
        Source(path="/x.py", source_reference=0),
    )
    assert text == ""
    ctrl._active_client.source.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_source_content_returns_empty_when_no_active_client():
    ctrl = _controller()
    ctrl._active_client = None
    text = await ctrl.fetch_source_content(
        Source(path="/x.py", source_reference=0),
    )
    assert text == ""


@pytest.mark.asyncio
async def test_fetch_source_content_delegates_to_active_client():
    ctrl = _controller()
    fake = DAPClient()
    fake.source = AsyncMock(return_value="def f(): pass\n")
    ctrl._active_client = fake
    src = Source(path="/srv/app.py", source_reference=0)
    text = await ctrl.fetch_source_content(src)
    assert text == "def f(): pass\n"
    fake.source.assert_awaited_once_with(0, src)
