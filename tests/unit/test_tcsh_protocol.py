import asyncio
import json

import pytest

from tdb.adapters.tcsh.protocol import ProtocolError, encode_message, read_message


def test_encode_message_uses_compact_utf8_body() -> None:
    body = json.dumps(
        {"seq": 1, "type": "request", "command": "é"}, separators=(",", ":")
    ).encode()
    encoded = encode_message({"seq": 1, "type": "request", "command": "é"})
    assert encoded == f"Content-Length: {len(body)}\r\n\r\n".encode() + body


@pytest.mark.asyncio
async def test_read_message_accepts_case_insensitive_content_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(
        b'content-length: 26\r\nX-Test: yes\r\n\r\n{"type":"request","seq":1}'
    )
    reader.feed_eof()
    assert await read_message(reader) == {"type": "request", "seq": 1}


@pytest.mark.asyncio
async def test_read_message_rejects_missing_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"X-Test: yes\r\n\r\n{}")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Content-Length"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_malformed_header() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length 2\r\n\r\n{}")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Malformed header"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_duplicate_content_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="exactly one Content-Length"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_negative_content_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: -1\r\n\r\n")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Invalid Content-Length"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_non_decimal_content_length() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 1_0\r\n\r\n{}")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Invalid Content-Length"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_translates_header_limit_errors() -> None:
    reader = asyncio.StreamReader(limit=8)
    reader.feed_data(b"X-Test: a very long header\r\n\r\n")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Malformed headers"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_truncated_body() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 2\r\n\r\n{")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Truncated message body"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_invalid_utf8_body() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 1\r\n\r\n\xff")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Invalid JSON message body"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_invalid_json_body() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 1\r\n\r\n{")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="Invalid JSON message body"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_non_object_json_body() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 2\r\n\r\n[]")
    reader.feed_eof()
    with pytest.raises(ProtocolError, match="JSON object"):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_reports_clean_eof_only_before_a_new_header() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()

    with pytest.raises(EOFError):
        await read_message(reader)


@pytest.mark.asyncio
async def test_read_message_rejects_eof_after_complete_headers_before_body() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"Content-Length: 5\r\n\r\n")
    reader.feed_eof()

    with pytest.raises(ProtocolError, match="Truncated message body"):
        await read_message(reader)
