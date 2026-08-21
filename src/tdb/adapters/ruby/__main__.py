"""python -m tdb.adapters.ruby — run the Ruby DAP proxy on stdio."""

import asyncio
import sys

from tdb.adapters.ruby.server import RubyDapServer


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
    await RubyDapServer(reader, writer).run()


if __name__ == "__main__":
    asyncio.run(main())
