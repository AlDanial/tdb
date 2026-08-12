import asyncio
import json
from collections.abc import Mapping


class ProtocolError(Exception):
    """Raised when a Debug Adapter Protocol message is malformed."""


class EndOfStream(EOFError):
    """Raised when input ends cleanly before the next DAP header."""


def encode_message(message: Mapping[str, object]) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


async def read_message(reader: asyncio.StreamReader) -> dict[str, object]:
    content_lengths: list[int] = []
    first_header = True

    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except asyncio.IncompleteReadError as error:
            if first_header and error.partial == b"":
                raise EndOfStream from error
            raise ProtocolError("Malformed headers") from error
        except asyncio.LimitOverrunError as error:
            raise ProtocolError("Malformed headers") from error
        first_header = False

        if line == b"\r\n":
            break
        if b":" not in line:
            raise ProtocolError("Malformed header")

        name, value = line[:-2].split(b":", 1)
        if not name:
            raise ProtocolError("Malformed header")
        if name.lower() == b"content-length":
            length_value = value.strip()
            if not length_value or not length_value.isdigit():
                raise ProtocolError("Invalid Content-Length")
            try:
                length = int(length_value)
            except ValueError as error:
                raise ProtocolError("Invalid Content-Length") from error
            if length < 0:
                raise ProtocolError("Invalid Content-Length")
            content_lengths.append(length)

    if len(content_lengths) != 1:
        raise ProtocolError("Expected exactly one Content-Length header")

    try:
        body = await reader.readexactly(content_lengths[0])
    except asyncio.IncompleteReadError as error:
        raise ProtocolError("Truncated message body") from error

    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("Invalid JSON message body") from error
    if not isinstance(message, dict):
        raise ProtocolError("Message body must be a JSON object")
    return message
