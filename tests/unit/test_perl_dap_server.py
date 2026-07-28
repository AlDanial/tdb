import asyncio
import json

import pytest

from tdb.adapters.perl.server import PerlDapServer
from tdb.dap.protocol import encode_message


class SinkWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass


def _messages(writer: SinkWriter) -> list[dict]:
    blob = b"".join(writer.chunks)
    out = []
    while blob:
        header, _, rest = blob.partition(b"\r\n\r\n")
        length = int(header.split(b":")[1])
        out.append(json.loads(rest[:length]))
        blob = rest[length:]
    return out


async def _run_conversation(requests: list[dict]) -> list[dict]:
    reader = asyncio.StreamReader()
    for req in requests:
        reader.feed_data(encode_message(req))
    reader.feed_eof()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    await asyncio.wait_for(server.run(), timeout=5)
    return _messages(writer)


async def test_initialize_then_disconnect():
    out = await _run_conversation(
        [
            {
                "seq": 1,
                "type": "request",
                "command": "initialize",
                "arguments": {"adapterID": "perl-tdb"},
            },
            {"seq": 2, "type": "request", "command": "disconnect"},
        ]
    )
    init = out[0]
    assert init["type"] == "response" and init["command"] == "initialize"
    assert init["success"] is True
    assert init["body"]["supportsConfigurationDoneRequest"] is True
    assert init["body"]["supportsConditionalBreakpoints"] is True
    disc = [m for m in out if m.get("command") == "disconnect"][0]
    assert disc["success"] is True


async def test_unknown_command_errors_but_survives():
    out = await _run_conversation(
        [
            {"seq": 1, "type": "request", "command": "frobnicate"},
            {"seq": 2, "type": "request", "command": "disconnect"},
        ]
    )
    frob = [m for m in out if m.get("command") == "frobnicate"][0]
    assert frob["success"] is False
    assert "frobnicate" in frob["message"]
    assert any(m.get("command") == "disconnect" for m in out)


async def test_handler_exception_becomes_error_response():
    reader = asyncio.StreamReader()
    reader.feed_data(
        encode_message(
            {"seq": 1, "type": "request", "command": "initialize", "arguments": {}}
        )
    )
    reader.feed_data(
        encode_message({"seq": 2, "type": "request", "command": "disconnect"})
    )
    reader.feed_eof()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)

    async def boom(request):
        raise RuntimeError("kaput")

    server.handlers["initialize"] = boom
    await asyncio.wait_for(server.run(), timeout=5)
    init = [m for m in _messages(writer) if m.get("command") == "initialize"][0]
    assert init["success"] is False and "kaput" in init["message"]
