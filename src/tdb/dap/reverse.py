"""Adapter-to-client (reverse) DAP requests, e.g. runInTerminal."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from tdb.dap.messages import Request, Response


class ReverseRequestError(Exception):
    """The client answered a reverse request with success=false."""


class ReverseRequester:
    """Send requests from a DAP adapter to its client and await replies."""

    def __init__(
        self,
        write: Callable[[dict[str, Any]], None],
        next_seq: Callable[[], int],
    ) -> None:
        self._write = write
        self._next_seq = next_seq
        self._pending: dict[int, asyncio.Future[Response]] = {}

    async def request(
        self, command: str, arguments: dict[str, Any], timeout: float = 30.0
    ) -> Response:
        seq = self._next_seq()
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        try:
            self._write(
                Request(seq=seq, command=command, arguments=arguments).to_dict()
            )
            response = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(seq, None)
        if not response.success:
            raise ReverseRequestError(
                response.message or f"{command} was refused by the client"
            )
        return response

    def route(self, msg: object) -> bool:
        if not isinstance(msg, Response):
            return False
        future = self._pending.get(msg.request_seq)
        if future is None:
            return False
        if not future.done():
            future.set_result(msg)
        return True
