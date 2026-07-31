"""python -m tdb.adapters.perl — run the Perl DAP adapter on stdio."""

import asyncio
import sys

from tdb.adapters.perl.server import PerlDapServer


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout.buffer
    )
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    await PerlDapServer(reader, writer).run()


if __name__ == "__main__":
    asyncio.run(main())
