"""DAP wire protocol: Content-Length framing over stdio.

Ported from the VS Code Python Debugger extension's protocolParser.ts.
Format: Content-Length: N\r\n\r\n{JSON body of N bytes}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

HEADER_SEPARATOR = b"\r\n\r\n"


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    """Read a single DAP message from the stream.

    Parses Content-Length header, then reads exactly that many bytes of JSON body.
    """
    # Read headers until we hit the \r\n\r\n separator
    header_data = b""
    while True:
        line = await reader.readline()
        if not line:
            raise ConnectionError("DAP stream closed")
        header_data += line
        if header_data.endswith(HEADER_SEPARATOR):
            break

    # Parse Content-Length from headers
    content_length = -1
    for line in header_data.decode("utf-8").split("\r\n"):
        if line.startswith("Content-Length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break

    if content_length < 0:
        raise ValueError(f"Missing Content-Length header in: {header_data!r}")

    # Read exactly content_length bytes
    body = await reader.readexactly(content_length)
    return json.loads(body.decode("utf-8"))


def encode_message(msg: dict[str, Any]) -> bytes:
    """Encode a DAP message with Content-Length framing."""
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    return header + body
