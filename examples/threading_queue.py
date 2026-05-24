#!/usr/bin/env python3
"""
Gemini 3.1 Pro Preview 2026-04-10
Thread Communication (Events and Queues)
Demonstrates how threads talk to each other safely using
queue.Queue and threading.Event.
"""

import threading
import queue
import time


def producer(q, stop_event):
    for i in range(5):
        time.sleep(0.5)  # Simulate work
        item = f"Item-{i}"
        q.put(item)
        print(f"[Producer] Created {item}")

    print("[Producer] Finished making items. Signaling stop.")
    stop_event.set()  # Signal the consumer to stop


def consumer(q, stop_event):
    # Loop until the stop event is set AND the queue is empty
    while not stop_event.is_set() or not q.empty():
        try:
            # Block for up to 1 second waiting for an item
            item = q.get(timeout=1)
            print(f"[Consumer] Processed {item}")
            q.task_done()
        except queue.Empty:
            continue
    print("[Consumer] Shutting down.")


def main():
    work_queue = queue.Queue()
    stop_event = threading.Event()

    prod_thread = threading.Thread(target=producer, args=(work_queue, stop_event))
    cons_thread = threading.Thread(target=consumer, args=(work_queue, stop_event))

    prod_thread.start()
    cons_thread.start()

    prod_thread.join()
    cons_thread.join()


if __name__ == "__main__":
    main()
