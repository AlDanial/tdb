#!/usr/bin/env python3
"""
Gemini 3.1 Pro Preview 2026-04-10
Basics and Concurrent Gathering
Demonstrates async def, await, and running multiple tasks concurrently.
"""
import asyncio
import time

async def fetch_data(id, delay):
    print(f"Task {id}: Starting download...")
    # asyncio.sleep simulates network I/O. 
    # It yields control back to the event loop so other tasks can run.
    await asyncio.sleep(delay) 
    print(f"Task {id}: Download complete!")
    return f"Data {id}"

async def main():
    start = time.time()
    
    # asyncio.gather runs multiple awaitables concurrently
    print("Gathering tasks...")
    results = await asyncio.gather(
        fetch_data(1, 2), # Takes 2 seconds
        fetch_data(2, 1), # Takes 1 second
        fetch_data(3, 3)  # Takes 3 seconds
    )
    
    # Total time will be ~3 seconds (the longest task), not 6 seconds!
    print(f"Results: {results}")
    print(f"Total time: {time.time() - start:.2f} seconds")

if __name__ == "__main__":
    # The entry point for all asyncio programs
    asyncio.run(main())
