#!/usr/bin/env python3
"""Tiny inventory service for the function-tracing walkthrough.

Answers ``CHECK sku qty`` lines with ``YES`` or ``NO``.  The stock lookup
runs on a thread pool (thread names start with ``db``), so a trace shows
asyncio task lanes and worker-thread lanes side by side.

    python inventory_service.py --port 0 --max-requests 4

Prints ``listening on PORT`` once it accepts connections and exits 0 after
``--max-requests`` requests (0 = run until Ctrl-C).  stdlib only.
"""

import argparse
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

STOCK = {"apple": 10, "pear": 5, "plum": 0, "fig": 3}
IO_TIMEOUT = 10  # seconds; no network wait is unbounded

pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db")


def lookup(sku, qty):
    time.sleep(0.01)  # stands in for a database round trip
    return STOCK.get(sku, 0) >= qty


async def handle_client(reader, writer, state):
    loop = asyncio.get_running_loop()
    try:
        line = await asyncio.wait_for(reader.readline(), IO_TIMEOUT)
        parts = line.decode().split()
        if len(parts) == 3 and parts[0] == "CHECK" and parts[2].isdigit():
            found = await loop.run_in_executor(pool, lookup, parts[1], int(parts[2]))
            reply = "YES" if found else "NO"
        else:
            reply = "NO"
        writer.write(reply.encode() + b"\n")
        await asyncio.wait_for(writer.drain(), IO_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        state["served"] += 1
        if state["served"] == state["max"]:
            state["done"].set()


async def main(port, max_requests):
    state = {"served": 0, "max": max_requests, "done": asyncio.Event()}
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, state), "127.0.0.1", port
    )
    print(f"listening on {server.sockets[0].getsockname()[1]}", flush=True)
    async with server:
        await state["done"].wait()
    pool.shutdown(wait=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=0, help="0 = ephemeral")
    ap.add_argument("--max-requests", type=int, default=0, help="0 = no limit")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.port, args.max_requests))
    except KeyboardInterrupt:
        pass
