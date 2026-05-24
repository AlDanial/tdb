#!/usr/bin/env python3
"""
Gemini 3.1 Pro Preview 2026-04-10
Background Tasks, Timeouts, and Cancellation
Demonstrates advanced Event Loop control.
"""

import asyncio


async def background_worker():
    try:
        count = 0
        while True:
            print(f"[Worker] Heartbeat {count}")
            count += 1
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        print("[Worker] I was cancelled! Cleaning up...")
        raise  # Good practice to re-raise CancelledError


async def unreliable_network_call():
    print("[Network] Attempting to connect...")
    await asyncio.sleep(5)  # Simulating a slow server
    return "Success!"


async def main():
    # 1. Create a background task that runs independently
    bg_task = asyncio.create_task(background_worker())

    # 2. Run a task with a timeout
    try:
        print("Main: Waiting for network call (max 2 seconds)...")
        # This will fail because the call takes 5 seconds
        result = await asyncio.wait_for(unreliable_network_call(), timeout=2.0)
    except asyncio.TimeoutError:
        print("Main: Network call timed out!")

    # 3. Cancel the background task
    print("Main: Cancelling background worker...")
    bg_task.cancel()

    # Wait for the task to actually finish its cancellation process
    try:
        await bg_task
    except asyncio.CancelledError:
        print("Main: Worker confirmed cancelled.")


if __name__ == "__main__":
    asyncio.run(main())
