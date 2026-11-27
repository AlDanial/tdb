#!/usr/bin/env python3
"""Driver for the function-tracing walkthrough: send COUNT orders to
orders_service.py concurrently and print each reply.

    python send_orders.py --port 8002 --count 4
"""

import argparse
import asyncio

IO_TIMEOUT = 10  # seconds
SKUS = ["apple", "pear", "plum", "fig"]


async def send_order(port, n):
    sku = SKUS[n % len(SKUS)]
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), IO_TIMEOUT
    )
    try:
        writer.write(f"ORDER {sku} {n + 1}\n".encode())
        await asyncio.wait_for(writer.drain(), IO_TIMEOUT)
        reply = await asyncio.wait_for(reader.readline(), IO_TIMEOUT)
    finally:
        writer.close()
    return f"order {n + 1} ({sku} x{n + 1}): {reply.decode().strip()}"


async def main(port, count):
    for line in await asyncio.gather(*(send_order(port, n) for n in range(count))):
        print(line, flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, required=True, help="orders service port")
    ap.add_argument("--count", type=int, default=4)
    args = ap.parse_args()
    asyncio.run(main(args.port, args.count))
