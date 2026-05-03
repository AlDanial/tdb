"""End-to-end framing test for dap.protocol: encode → read_message round trip."""

from __future__ import annotations

import asyncio

from tdb.dap.protocol import encode_message, read_message


def _reader_for(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_read_message_round_trip():
    msg = {"seq": 3, "type": "event", "event": "stopped", "body": {"reason": "step"}}
    reader = _reader_for(encode_message(msg))
    got = await read_message(reader)
    assert got == msg


async def test_read_message_handles_back_to_back():
    raw = encode_message({"seq": 1, "type": "event", "event": "a"})
    raw += encode_message({"seq": 2, "type": "event", "event": "b"})
    reader = _reader_for(raw)
    first = await read_message(reader)
    second = await read_message(reader)
    assert first["event"] == "a"
    assert second["event"] == "b"


async def test_read_message_unicode_payload():
    msg = {"seq": 1, "type": "event", "event": "output", "body": {"text": "héllo→世界"}}
    reader = _reader_for(encode_message(msg))
    got = await read_message(reader)
    assert got["body"]["text"] == "héllo→世界"
