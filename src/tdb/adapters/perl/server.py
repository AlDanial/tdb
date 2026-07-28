"""DAP stdio server for the Perl adapter.

One request at a time is dispatched from the read loop; handlers are
`_on_<command>` methods collected into `self.handlers` so later tasks
extend the surface by adding methods. Events may be emitted at any
time via send_event (the session driver calls it from its own task).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from tdb.dap.messages import Event, Request, Response, parse_message
from tdb.dap.protocol import encode_message, read_message

log = logging.getLogger(__name__)

CAPABILITIES = {
    "supportsConfigurationDoneRequest": True,
    "supportsConditionalBreakpoints": True,
    "supportsTerminateRequest": True,
}


class PerlDapServer:
    def __init__(self, reader: asyncio.StreamReader, writer: Any) -> None:
        self._reader = reader
        self._writer = writer
        self._seq = 0
        self._done = asyncio.Event()
        self.handlers: dict[str, Callable[[Request], Awaitable[None]]] = {}
        for name in dir(self):
            if name.startswith("_on_"):
                self.handlers[name[4:]] = getattr(self, name)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _write(self, msg: dict) -> None:
        self._writer.write(encode_message(msg))

    def send_response(self, request: Request, body: dict | None = None) -> None:
        self._write(
            Response(
                seq=self._next_seq(),
                request_seq=request.seq,
                command=request.command,
                success=True,
                body=body or {},
            ).to_dict()
        )

    def send_error(self, request: Request, message: str) -> None:
        self._write(
            Response(
                seq=self._next_seq(),
                request_seq=request.seq,
                command=request.command,
                success=False,
                message=message,
            ).to_dict()
        )

    def send_event(self, event: str, body: dict | None = None) -> None:
        self._write(Event(seq=self._next_seq(), event=event, body=body or {}).to_dict())

    async def run(self) -> None:
        while not self._done.is_set():
            try:
                raw = await read_message(self._reader)
            except (ConnectionError, asyncio.IncompleteReadError, EOFError):
                break
            msg = parse_message(raw)
            if not isinstance(msg, Request):
                continue
            handler = self.handlers.get(msg.command)
            if handler is None:
                self.send_error(msg, f"unsupported command: {msg.command}")
                continue
            try:
                await handler(msg)
            except Exception as e:
                log.exception("handler %s failed", msg.command)
                self.send_error(msg, str(e))
            await self._writer.drain()
        await self._writer.drain()

    async def _on_initialize(self, request: Request) -> None:
        self.send_response(request, CAPABILITIES)

    async def _on_disconnect(self, request: Request) -> None:
        self.send_response(request)
        self._done.set()

    async def _on_terminate(self, request: Request) -> None:
        self.send_response(request)
        self._done.set()
