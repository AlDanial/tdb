#!/usr/bin/env python3
"""
Gemini 3.1 Pro Preview 2026-04-10
Shared State and Inter-Process Communication (IPC)
Because memory isn't shared, processes use multiprocessing.
Value for shared memory, or multiprocessing.Queue for messaging.
"""
import multiprocessing
import time

def worker_with_shared_memory(shared_counter, lock):
    # Processes need a lock to safely modify shared memory too!
    for _ in range(100):
        with lock:
            shared_counter.value += 1

def worker_with_queue(q, name):
    q.put(f"Message from {name}")

if __name__ == "__main__":
    # 1. SHARED MEMORY (Value)
    # 'i' means integer. It starts at 0.
    shared_counter = multiprocessing.Value('i', 0) 
    lock = multiprocessing.Lock()
    
    processes = [multiprocessing.Process(target=worker_with_shared_memory, args=(shared_counter, lock)) for _ in range(5)]
    for p in processes: p.start()
    for p in processes: p.join()
    
    print(f"Shared Counter Final Value: {shared_counter.value}")

    # 2. MESSAGE PASSING (Queue)
    # Multiprocessing has its own Queue, different from the threading Queue!
    mp_queue = multiprocessing.Queue()
    
    p1 = multiprocessing.Process(target=worker_with_queue, args=(mp_queue, "Process A"))
    p2 = multiprocessing.Process(target=worker_with_queue, args=(mp_queue, "Process B"))
    
    p1.start(); p2.start()
    p1.join(); p2.join()
    
    while not mp_queue.empty():
        print(mp_queue.get())
