#!/usr/bin/env python3
"""
Classic two-lock asyncio deadlock — Worker-A and Worker-B each acquire
one lock, then try to acquire the other. Neither can make progress.

Running this directly will hang forever. The point is to run it under
tdb to inspect the deadlock:

    tdb examples/asyncio_deadlock.py

Workflow once tdb opens:

    1. Press `c` to continue past stop-on-entry. The workers deadlock.
    2. Press `p` to pause. Pause lands inside the Monitor task (see
       note below on why a Monitor task is needed).
    3. Press Alt+A (or click "Async Tasks" in the menu) to open the
       Async Tasks modal. Worker-A and Worker-B appear in red with
       "↻ deadlock" markers — the wait-graph cycle detector found them.
    4. Press `g` to switch the right pane to the wait graph. The
       "Deadlock cycles" section shows: Worker-A <-> Worker-B. Expand
       either task to see it parked on Lock.acquire with the other
       task as the holder, terminating in a "(cycle ref)" leaf.

Headless equivalent (when running with --server):

    curl -s -X POST http://127.0.0.1:8150/rpc \\
      -H 'Content-Type: application/json' \\
      -d '{"action":"wait_graph","params":[]}'

Why a Monitor task: a fully-deadlocked asyncio program has no Python
bytecode running — the main thread is blocked in epoll_wait waiting
for events that never come. debugpy's `pause` request only takes
effect inside Python's trace dispatcher, so without something keeping
the event loop alive, pressing `p` has no visible effect. The Monitor
task wakes every 0.5s on a wallclock timer (independent of any inter-
task signal) so debugpy always has a Python frame to land on.
"""

import asyncio


async def worker_a(
    first: asyncio.Lock,
    second: asyncio.Lock,
    ready: asyncio.Event,
    peer_ready: asyncio.Event,
) -> None:
    async with first:
        ready.set()
        # Wait until the peer has its own first lock — guarantees the
        # deadlock instead of one task racing through both acquires.
        await peer_ready.wait()
        async with second:
            print("Worker-A: never reaches here")


async def worker_b(
    first: asyncio.Lock,
    second: asyncio.Lock,
    ready: asyncio.Event,
    peer_ready: asyncio.Event,
) -> None:
    async with first:
        ready.set()
        await peer_ready.wait()
        async with second:
            print("Worker-B: never reaches here")


async def monitor() -> None:
    while True:
        await asyncio.sleep(0.5)


async def main() -> None:
    lock_x = asyncio.Lock()
    lock_y = asyncio.Lock()
    a_ready = asyncio.Event()
    b_ready = asyncio.Event()

    # A grabs X first, then tries Y. B grabs Y first, then tries X.
    # Opposite acquisition order is the textbook deadlock recipe.
    a = asyncio.create_task(
        worker_a(lock_x, lock_y, a_ready, b_ready),
        name="Worker-A",
    )
    b = asyncio.create_task(
        worker_b(lock_y, lock_x, b_ready, a_ready),
        name="Worker-B",
    )
    mon = asyncio.create_task(monitor(), name="Monitor")

    print("Both workers started — they will deadlock momentarily.")
    print(
        "In tdb: press `p` to pause, then Alt+A for Async Tasks, then `g` for the wait graph."
    )
    try:
        await asyncio.gather(a, b)  # never returns
    finally:
        mon.cancel()


#       pass


if __name__ == "__main__":
    asyncio.run(main())
