#!/usr/bin/env python3
"""
Gemini 3.1 Pro Preview 2026-04-10
Basics and Race Conditions (Locks)
Demonstrates how to spawn threads, and why shared memory requires a Lock.
"""

import threading
import time

# Shared memory variable
counter = 0
counter_lock = threading.Lock()


def increment_counter(thread_name, use_lock):
    global counter
    for _ in range(100000):
        if use_lock:
            with counter_lock:  # Acquires and releases the lock automatically
                counter += 1
        else:
            counter += 1  # Unsafe! Leads to race conditions


def main():
    global counter
    print("--- Running WITHOUT Lock ---")
    counter = 0
    threads = [
        threading.Thread(target=increment_counter, args=(f"T{i}", False))
        for i in range(5)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()  # Wait for all to finish
    print(f"Expected: 500000, Actual: {counter}")  # Will likely NOT be 500k!

    print("\n--- Running WITH Lock ---")
    counter = 0
    threads = [
        threading.Thread(target=increment_counter, args=(f"T{i}", True))
        for i in range(5)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"Expected: 500000, Actual: {counter}")  # Will be exactly 500k


if __name__ == "__main__":
    main()
