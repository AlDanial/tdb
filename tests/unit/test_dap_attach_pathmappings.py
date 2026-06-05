"""Unit tests for DAPClient.attach pathMappings serialization.

`--local-root` / `--remote-root` are paired in CLI order and threaded
through `controller.remote_attach` → `client.attach(path_mappings=...)`.
debugpy expects the wire shape `[{"localRoot": L, "remoteRoot": R}, ...]`
in the attach arguments; this file pins that contract.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from tdb.dap.client import DAPClient


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
