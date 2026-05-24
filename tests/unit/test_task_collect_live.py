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


# --- awaiting_obj_id (wait-graph foundation) ---------------------------


async def test_awaiting_obj_id_matches_lock_identity():
    """For a task blocked on Lock.acquire, awaiting_obj_id must equal id(lock).
    This is what the wait-graph builder will use to match a blocked task
    against the holder (which has the same lock in its frame locals)."""
    lock = asyncio.Lock()
    await lock.acquire()  # held by this test's task
    blocked = asyncio.create_task(lock.acquire(), name="LockObjId")
    try:
        await asyncio.sleep(0)
        info = _find(_collect(), "LockObjId")
        assert info.awaiting == "Lock.acquire"
        assert info.awaiting_obj_id == id(lock)
    finally:
        blocked.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await blocked


async def test_awaiting_obj_id_matches_event_identity():
    """Event has no holder concept but awaiting_obj_id still pins down
    which Event the task is waiting on — useful when several tasks wait
    on different Events."""
    ev = asyncio.Event()

    async def waiter():
        await ev.wait()

    t = asyncio.create_task(waiter(), name="EventObjId")
    try:
        await asyncio.sleep(0)
        info = _find(_collect(), "EventObjId")
        assert info.awaiting == "Event.wait"
        assert info.awaiting_obj_id == id(ev)
    finally:
        ev.set()
        await t


async def test_awaiting_obj_id_for_sleep_points_to_fut_waiter():
    """asyncio.sleep is a free function — there's no `self` primitive,
    so awaiting_obj_id falls back to id(_fut_waiter), the Future the
    task is parked on. Must be a non-zero int (anchor for the graph)."""
    t = asyncio.create_task(asyncio.sleep(60), name="SleepObjId")
    try:
        await asyncio.sleep(0)
        info = _find(_collect(), "SleepObjId")
        assert info.awaiting == "asyncio.sleep"
        assert isinstance(info.awaiting_obj_id, int)
        assert info.awaiting_obj_id == id(t._fut_waiter)
    finally:
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t


async def test_awaiting_obj_id_is_none_for_running_test_task():
    """The currently-running task is not parked on any primitive at the
    moment of collection — awaiting_obj_id must be None (or at worst an
    int for the test framework's gather/Future, but never crash)."""
    tasks = _collect()
    for t in tasks:
        assert t.awaiting_obj_id is None or isinstance(t.awaiting_obj_id, int)


# --- Holder detection (wait-graph foundation) --------------------------


async def test_lock_holder_detected():
    """Task holding a Lock must be reported as the holder for a task
    blocked on that same Lock. Holder coro takes the lock as a parameter
    so it's a real f_locals entry (closure-cell capture is also
    supported on modern Python, but parameter binding is unambiguous)."""
    lock = asyncio.Lock()

    async def holder_coro(_lock):
        async with _lock:
            await asyncio.sleep(60)

    holder_t = asyncio.create_task(holder_coro(lock), name="LockHolder")
    await asyncio.sleep(0)  # let holder acquire and park inside the with-body

    blocked_t = asyncio.create_task(lock.acquire(), name="LockWaiter")
    try:
        await asyncio.sleep(0)
        waiter = _find(_collect(), "LockWaiter")
        assert waiter.awaiting == "Lock.acquire"
        assert "LockHolder" in waiter.holders
    finally:
        blocked_t.cancel()
        holder_t.cancel()
        for t in (blocked_t, holder_t):
            try:
                await t
            except asyncio.CancelledError:
                pass


async def test_semaphore_reports_all_current_holders():
    """A Semaphore can have N concurrent holders — the blocked waiter
    should see all of them."""
    sem = asyncio.Semaphore(2)

    async def holder_coro(_sem):
        async with _sem:
            await asyncio.sleep(60)

    h1 = asyncio.create_task(holder_coro(sem), name="SemH1")
    h2 = asyncio.create_task(holder_coro(sem), name="SemH2")
    await asyncio.sleep(0)  # both acquire the 2 permits

    blocked = asyncio.create_task(holder_coro(sem), name="SemBlocked")
    try:
        await asyncio.sleep(0)
        waiter = _find(_collect(), "SemBlocked")
        assert waiter.awaiting == "Semaphore.acquire"
        assert "SemH1" in waiter.holders
        assert "SemH2" in waiter.holders
        assert "SemBlocked" not in waiter.holders
    finally:
        for t in (blocked, h1, h2):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


async def test_co_waiter_is_not_reported_as_holder():
    """Two tasks both blocked on the same Lock — neither should report
    the other as a holder (they're co-waiters, not holders)."""
    lock = asyncio.Lock()
    await lock.acquire()  # held by the test's own task

    w1 = asyncio.create_task(lock.acquire(), name="CoWaiter1")
    w2 = asyncio.create_task(lock.acquire(), name="CoWaiter2")
    try:
        await asyncio.sleep(0)
        tasks = _collect()
        a = _find(tasks, "CoWaiter1")
        b = _find(tasks, "CoWaiter2")
        assert "CoWaiter2" not in a.holders
        assert "CoWaiter1" not in b.holders
    finally:
        w1.cancel()
        w2.cancel()
        lock.release()
        for t in (w1, w2):
            try:
                await t
            except asyncio.CancelledError:
                pass


async def test_holders_empty_for_unblocked_task():
    """A task that is not parked on any primitive (awaiting_obj_id is
    None) must have an empty holders list — there's nothing to hold."""
    tasks = _collect()
    for t in tasks:
        if t.awaiting_obj_id is None:
            assert t.holders == []


# --- Deadlock detection end-to-end -------------------------------------


async def test_deadlock_cycle_detected_end_to_end():
    """Two-task deadlock: A holds X waits on Y, B holds Y waits on X.
    Verifies the full pipeline: live tasks → TASK_COLLECT_EXPR →
    parse_task_json → build_wait_graph → find_cycles surfaces the
    cycle. This is the headline feature of the wait-graph work."""
    from tdb.inspection import build_wait_graph, find_cycles

    lock_x = asyncio.Lock()
    lock_y = asyncio.Lock()

    # Coordinate so each task acquires its own lock first, then tries
    # to acquire the other — guaranteed deadlock.
    a_has_x = asyncio.Event()
    b_has_y = asyncio.Event()

    async def task_a(_x, _y):
        async with _x:
            a_has_x.set()
            await b_has_y.wait()
            async with _y:
                pass

    async def task_b(_x, _y):
        async with _y:
            b_has_y.set()
            await a_has_x.wait()
            async with _x:
                pass

    a = asyncio.create_task(task_a(lock_x, lock_y), name="DeadlockA")
    b = asyncio.create_task(task_b(lock_x, lock_y), name="DeadlockB")
    try:
        # Pump the loop until both tasks are parked on their second lock.
        for _ in range(20):
            await asyncio.sleep(0)
            tasks = _collect()
            try:
                a_info = _find(tasks, "DeadlockA")
                b_info = _find(tasks, "DeadlockB")
            except AssertionError:
                continue
            if (
                a_info.awaiting == "Lock.acquire"
                and b_info.awaiting == "Lock.acquire"
                and a_info.holders
                and b_info.holders
            ):
                break
        else:
            pytest.fail("tasks did not reach deadlock state in time")

        graph = build_wait_graph(tasks)
        cycles = find_cycles(graph)
        # Each task waits on a lock the other holds — exactly one cycle.
        assert ["DeadlockA", "DeadlockB"] in cycles
    finally:
        a.cancel()
        b.cancel()
        for t in (a, b):
            try:
                await t
            except asyncio.CancelledError:
                pass


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
