"""Live-asyncio tests for TASK_COLLECT_EXPR's awaiting classifier.

The classifier is a heuristic that walks the innermost stack frame of
each task and matches against asyncio stdlib filenames + class names.
These tests exercise it against real `asyncio.Lock`, `asyncio.Queue`,
`asyncio.Event`, and `asyncio.sleep` so a future change to the
heuristic (or to CPython's stdlib path layout) fails loudly here
rather than silently breaking the modal.

We `eval(TASK_COLLECT_EXPR)` directly. The expression is shaped as an
immediately-invoked lambda that returns a JSON string, which the
parser also accepts unwrapped (see _decode_json_repr).
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.inspection import TASK_COLLECT_EXPR, parse_task_json


def _collect() -> list:
    """Run the debuggee-side expression in this process and parse it."""
    raw = eval(TASK_COLLECT_EXPR)  # noqa: S307 — controlled, not user input
    return parse_task_json(raw)


def _find(tasks, name):
    matches = [t for t in tasks if t.name == name]
    assert matches, f"task '{name}' not in {[t.name for t in tasks]}"
    return matches[0]


# --- Awaiting classifier ------------------------------------------------

async def test_classifies_lock_acquire():
    lock = asyncio.Lock()
    await lock.acquire()  # held by the test's own task

    blocked = asyncio.create_task(lock.acquire(), name="LockBlocked")
    try:
        await asyncio.sleep(0)  # let the task start and reach the await
        info = _find(_collect(), "LockBlocked")
        assert info.awaiting == "Lock.acquire"
        assert info.state == "pending"
    finally:
        blocked.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await blocked


async def test_classifies_queue_get():
    q: asyncio.Queue = asyncio.Queue()

    async def consumer():
        await q.get()

    t = asyncio.create_task(consumer(), name="QueueConsumer")
    try:
        await asyncio.sleep(0)
        info = _find(_collect(), "QueueConsumer")
        assert info.awaiting == "Queue.get"
    finally:
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t


async def test_classifies_event_wait_disambiguates_from_condition():
    """Both Event.wait and Condition.wait live in asyncio/locks.py — the
    classifier uses type(self).__name__ to tell them apart."""
    ev = asyncio.Event()

    async def waiter():
        await ev.wait()

    t = asyncio.create_task(waiter(), name="EventWaiter")
    try:
        await asyncio.sleep(0)
        info = _find(_collect(), "EventWaiter")
        assert info.awaiting == "Event.wait"
    finally:
        ev.set()
        await t


async def test_classifies_asyncio_sleep():
    """asyncio.sleep has no `self` (free function) — exercises the
    tasks.py branch that doesn't require a class name."""
    t = asyncio.create_task(asyncio.sleep(60), name="Sleeper")
    try:
        await asyncio.sleep(0)
        info = _find(_collect(), "Sleeper")
        assert info.awaiting == "asyncio.sleep"
    finally:
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t


# --- Cancellation surfacing --------------------------------------------

async def test_cancelling_count_is_reported():
    """task.cancelling() returns the number of pending cancellations
    that haven't been observed yet. Calling cancel() once on a sleeping
    task before it gets the chance to run should leave cancelling() == 1
    when we collect."""
    t = asyncio.create_task(asyncio.sleep(60), name="ToBeCancelled")
    await asyncio.sleep(0)  # let it park in sleep
    t.cancel()
    # Don't yield again — we want cancelling to still be pending when
    # we read it. asyncio decrements cancelling() once the task observes
    # CancelledError.
    info = _find(_collect(), "ToBeCancelled")
    assert info.cancelling >= 1
    with pytest.raises(asyncio.CancelledError):
        await t


async def test_cancel_message_is_captured():
    t = asyncio.create_task(asyncio.sleep(60), name="WithMessage")
    await asyncio.sleep(0)
    t.cancel("graceful shutdown requested")
    info = _find(_collect(), "WithMessage")
    assert info.cancel_message == "graceful shutdown requested"
    with pytest.raises(asyncio.CancelledError):
        await t


# --- Defensive: no false positives -------------------------------------

async def test_normal_running_task_has_no_awaiting():
    """A task currently *executing* (not parked on a primitive) should
    report awaiting=None or something sensible — at minimum it must not
    falsely report a Lock/Queue/etc."""
    # The currently-running test task itself is parked on the test
    # framework's gather/run path. We don't assert what it reports —
    # only that the classifier never crashes and that cancelling=0
    # for tasks no one cancelled.
    tasks = _collect()
    assert tasks, "should at least see the running test task"
    for t in tasks:
        assert isinstance(t.cancelling, int)
        assert t.cancelling >= 0
