#!/usr/bin/env python3
"""
Gemini 3.1 Pro Preview 2026-04-10
The easiest and most common way to use multiprocessing is the Pool,
which distributes a function across multiple CPU cores.

Note on Windows: multiprocessing code MUST be inside an
  if __name__ == '__main__': block
"""
import multiprocessing
import time
import math

def heavy_computation(number):
    # Simulate a CPU-heavy task
    result = 0
    for i in range(10000 * number):
        result += math.sqrt(i)
    return f"Result for {number} is done."

if __name__ == "__main__":
    numbers = [100, 200, 300, 400, 500, 600, 700, 800]
    
    start_time = time.time()
    
    # Create a pool of workers equal to the number of CPU cores
    print(f"Starting pool with {multiprocessing.cpu_count()} cores...")
    with multiprocessing.Pool(6) as pool:
        # map() blocks until all are done, maintaining order
        results = pool.map(heavy_computation, numbers)
        
    for r in results:
        print(r)
        
    print(f"Finished in {time.time() - start_time:.2f} seconds")
