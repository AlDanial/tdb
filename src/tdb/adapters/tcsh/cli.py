from __future__ import annotations

import asyncio
import os
import signal
import stat
import sys
import traceback
from collections import deque
from typing import BinaryIO

from tdb.adapters.tcsh.protocol import ProtocolError
from tdb.adapters.tcsh.server import DAPServer


class _StdoutWriter:
    """Adapt a binary stdout stream to the server's async writer surface."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._descriptor = stream.fileno()
        self._original_blocking = os.get_blocking(self._descriptor)
        os.set_blocking(self._descriptor, False)
        self._pending: deque[bytes] = deque()
        self._flush_task: asyncio.Task[None] | None = None
        self._drain_lock = asyncio.Lock()
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("stdout writer is closed")
        self._pending.append(bytes(data))

    async def drain(self) -> None:
        async with self._drain_lock:
            if self._closed:
                raise RuntimeError("stdout writer is closed")
            while self._pending or self._flush_task is not None:
                task = self._flush_task
                if task is None:
                    task = asyncio.create_task(self._write_all(self._pending.popleft()))
                    self._flush_task = task
                    task.add_done_callback(_consume_task_exception)
                try:
                    await asyncio.shield(task)
                finally:
                    if task.done() and self._flush_task is task:
                        self._flush_task = None

    async def close(self) -> None:
        """Cancel pending backpressure waits and restore the inherited descriptor mode."""

        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
        try:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        finally:
            try:
                os.set_blocking(self._descriptor, self._original_blocking)
            except OSError:
                pass

    async def _write_all(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._descriptor, view)
            except BlockingIOError:
                await self._wait_writable()
                continue
            except InterruptedError:
                continue
            if written == 0:
                raise BrokenPipeError("stdout accepted no DAP bytes")
            view = view[written:]

    async def _wait_writable(self) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        loop.add_writer(self._descriptor, _mark_ready, ready)
        try:
            await ready
        finally:
            loop.remove_writer(self._descriptor)


async def _run_adapter() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    transport: asyncio.ReadTransport | None = None
    if _is_devnull(sys.stdin.buffer):
        reader.feed_eof()
    else:
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    writer = _StdoutWriter(sys.stdout.buffer)
    server = DAPServer(reader, writer)
    shutdown_tasks: set[asyncio.Task[None]] = set()

    def request_shutdown() -> None:
        task = asyncio.create_task(server.stop())
        shutdown_tasks.add(task)
        task.add_done_callback(shutdown_tasks.discard)

    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(sig)
    try:
        await server.run()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        if shutdown_tasks:
            await asyncio.gather(*shutdown_tasks, return_exceptions=True)
        await writer.close()
        if transport is not None:
            transport.close()


def _mark_ready(ready: asyncio.Future[None]) -> None:
    if not ready.done():
        ready.set_result(None)


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


def _is_devnull(stream: BinaryIO) -> bool:
    try:
        stream_status = os.fstat(stream.fileno())
        devnull_status = os.stat(os.devnull)
    except (AttributeError, OSError):
        return False
    return (
        stat.S_ISCHR(stream_status.st_mode)
        and stream_status.st_dev == devnull_status.st_dev
        and stream_status.st_ino == devnull_status.st_ino
    )


def main() -> int:
    try:
        asyncio.run(_run_adapter())
    except ProtocolError as error:
        print(f"Protocol error: {error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0
