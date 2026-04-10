threading: Use for I/O bound tasks. Easy to write, but you must use Locks to prevent data corruption. Limited by the GIL (only 1 CPU core used).

multiprocessing: Use for CPU bound tasks. Bypasses the GIL, uses multiple CPU cores. Heavy memory footprint because it copies the entire Python environment.

asyncio: Use for massive I/O bound tasks (networking). Extremely lightweight, no Locks required (mostly), but requires writing code in an entirely different paradigm (async/await), and blocking code (time.sleep) ruins the entire system.
