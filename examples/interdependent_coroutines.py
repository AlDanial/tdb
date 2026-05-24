#!/usr/bin/env python3
import asyncio
import random
import time


async def producer(queue: asyncio.Queue, num_items: int):
    """Produces items and puts them into the queue."""
    print("[Producer] Starting up...")
    for i in range(1, num_items + 1):
        await asyncio.sleep(random.uniform(0.1, 0.5))  # do fake work
        item = f"DataPacket-{i}"

        await queue.put(item)  # for the consumer to pick up
        print(f"[Producer] Generated and queued: {item}")

    await queue.put(None)  # None == sentinel value that tells the consumer to stop
    print("[Producer] Finished generating items.")


async def consumer(queue: asyncio.Queue):
    """Consumes items from the queue and processes them."""
    print("[Consumer] Waiting for items to process...")
    while True:
        item = await queue.get()  # pause until the producer provides data

        if item is None:  # None == the shutdown signal from the producer
            print("[Consumer] Received shutdown signal. Exiting.")
            queue.task_done()
            break

        print(f"[Consumer] Processing {item}...")  # do fake work
        await asyncio.sleep(random.uniform(0.3, 0.8))
        print(f"[Consumer] Successfully processed {item}.")

        # Tell the queue that the processing for this item is complete
        queue.task_done()  # this queue item is done, now producer can add more


async def main():
    queue = asyncio.Queue()

    # consumer starts immediately but blocks until the producer provides data
    producer_task = asyncio.create_task(producer(queue, num_items=3))
    consumer_task = asyncio.create_task(consumer(queue))

    await asyncio.gather(producer_task, consumer_task)
    print("\n[Main] All tasks completed successfully.")


if __name__ == "__main__":
    start_time = time.time()
    asyncio.run(main())
    print(f"Total execution time: {time.time() - start_time:.2f} seconds")
