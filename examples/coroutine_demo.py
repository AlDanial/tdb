#!/usr/bin/env python3
import asyncio
import time


async def download_file_coroutine(task_id, duration):
    print(f"doing long I/O task {task_id}...")
    x = task_id * 4 + 17
    await asyncio.sleep(duration)  # suspends the coroutine, allowos others to run
    print(f"finished long I/O task {task_id}...")
    return f"results from task {task_id}"


async def main():
    start_time = time.time()

    coro1 = download_file_coroutine(1, 3)  #  3 seconds
    coro2 = download_file_coroutine(2, 2)  #  2 seconds

    results = await asyncio.gather(coro1, coro2)  # run both concurrently

    print(f"\nAll I/O tasks completed: {results}")
    end_time = time.time()
    print(f"Total time taken: {end_time - start_time:.2f} seconds")


if __name__ == "__main__":
    asyncio.run(main())
