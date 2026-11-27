#!/usr/bin/env python3
"""Tiny orders service for the function-tracing walkthrough.

Answers ``ORDER sku qty`` lines with ``OK`` or ``NO`` after asking
inventory_service.py whether the stock is there.  A daemon thread named
``heartbeat`` runs alongside the event loop.

    python orders_service.py --port 0 --inventory-port 9001 --max-requests 4

Prints ``listening on PORT`` once it accepts connections and exits 0 after
``--max-requests`` requests (0 = run until Ctrl-C).  stdlib only.
"""

import argparse
import asyncio
import threading
import time

IO_TIMEOUT = 10  # seconds; no network wait is unbounded


def beat():
    time.sleep(0.001)


def heartbeat():
    while True:
        beat()
        time.sleep(0.05)


async def ask_inventory(port, sku, qty):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port), IO_TIMEOUT
    )
    try:
        writer.write(f"CHECK {sku} {qty}\n".encode())
        await asyncio.wait_for(writer.drain(), IO_TIMEOUT)
        reply = await asyncio.wait_for(reader.readline(), IO_TIMEOUT)
        return reply.decode().strip() == "YES"
    finally:
        writer.close()


async def handle_order(reader, writer, state):
    try:
        line = await asyncio.wait_for(reader.readline(), IO_TIMEOUT)
        parts = line.decode().split()
        reply = "NO"
        if len(parts) == 3 and parts[0] == "ORDER" and parts[2].isdigit():
            if await ask_inventory(state["inventory_port"], parts[1], int(parts[2])):
                reply = "OK"
        writer.write(reply.encode() + b"\n")
        await asyncio.wait_for(writer.drain(), IO_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()
        state["served"] += 1
        if state["served"] == state["max"]:
            state["done"].set()


async def main(port, inventory_port, max_requests):
    state = {
        "inventory_port": inventory_port,
        "served": 0,
        "max": max_requests,
        "done": asyncio.Event(),
    }
    threading.Thread(target=heartbeat, name="heartbeat", daemon=True).start()
    server = await asyncio.start_server(
        lambda r, w: handle_order(r, w, state), "127.0.0.1", port
    )
    print(f"listening on {server.sockets[0].getsockname()[1]}", flush=True)
    async with server:
        await state["done"].wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=0, help="0 = ephemeral")
    ap.add_argument("--inventory-port", type=int, required=True)
    ap.add_argument("--max-requests", type=int, default=0, help="0 = no limit")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.port, args.inventory_port, args.max_requests))
    except KeyboardInterrupt:
        pass
