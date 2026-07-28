"""python -m tdb.adapters.perl — run the Perl DAP adapter on stdio."""

import asyncio
import os
import sys

from tdb.adapters.perl.server import PerlDapServer


class _FdWriter:
    """Wrapper for writing to a file descriptor."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def write(self, data: bytes) -> None:
        os.write(self._fd, data)

    async def drain(self) -> None:
        pass


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()

    # Try to connect read pipe; fall back to direct reading if it fails.
    try:
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
    except (ValueError, RuntimeError):
        # In testing/piped environments, connect_read_pipe may fail.
        # Fall back to manual file descriptor reading.
        async def read_from_stdin() -> None:
            loop = asyncio.get_running_loop()
            while True:
                try:
                    data = await loop.run_in_executor(None, sys.stdin.buffer.read, 4096)
                    if not data:
                        reader.feed_eof()
                        break
                    reader.feed_data(data)
                except Exception:
                    reader.feed_eof()
                    break

        asyncio.create_task(read_from_stdin())

    # Use a simple file descriptor wrapper for stdout.
    writer = _FdWriter(sys.stdout.fileno())
    await PerlDapServer(reader, writer).run()


if __name__ == "__main__":
    asyncio.run(main())
