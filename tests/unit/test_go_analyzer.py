"""tests/unit/test_go_analyzer.py"""

from tdb.go_concurrency.analyzer import analyze
from tdb.go_concurrency.models import (
    Confidence,
    GoFindingKind,
    GoroutineInfo,
    GoroutineState,
)


def _g(tid, state, op=None, res=None, func="main.f", runtime=False):
    return GoroutineInfo(
        thread_id=tid,
        goid=tid,
        function=func,
        state=state,
        operation=op,
        resource_id=res,
        frames=(),
        is_runtime=runtime,
    )


CH = "chan:0xc000024180"


def test_edges_and_resources_built():
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH), _g(2, GoroutineState.RUNNING)],
        uncollected=0,
        warnings=(),
    )
    assert [e.to_dict() for e in snap.edges] == [
        {"thread_id": 1, "resource_id": CH, "operation": "recv"}
    ]
    assert snap.resources[0].kind == "channel"


def test_matched_send_recv_is_not_a_finding():
    snap = analyze(
        [
            _g(1, GoroutineState.CHAN_RECV, "recv", CH),
            _g(2, GoroutineState.CHAN_SEND, "send", CH),
        ],
        uncollected=0,
        warnings=(),
    )
    assert snap.findings == ()


def test_stuck_channel_confirmed_when_everyone_blocked():
    snap = analyze(
        [
            _g(1, GoroutineState.CHAN_RECV, "recv", CH),
            _g(2, GoroutineState.CHAN_RECV, "recv", CH),
            _g(3, GoroutineState.RUNTIME, runtime=True),
        ],
        uncollected=0,
        warnings=(),
    )
    (f,) = snap.findings
    assert f.kind is GoFindingKind.STUCK_CHANNEL
    assert f.confidence is Confidence.CONFIRMED
    assert set(f.thread_ids) == {1, 2}


def test_stuck_channel_suppressed_when_something_still_runs():
    # Spec: STUCK_CHANNEL fires only when no runnable/running non-runtime
    # goroutine remains -- if something else can still run, the waiters
    # aren't confidently "stuck" (that goroutine could yet unblock them).
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH), _g(2, GoroutineState.RUNNING)],
        uncollected=0,
        warnings=(),
    )
    assert snap.findings == ()


def test_uncollected_downgrades_confidence():
    snap = analyze(
        [_g(1, GoroutineState.CHAN_RECV, "recv", CH)],
        uncollected=5,
        warnings=(),
    )
    (f,) = snap.findings
    assert f.confidence is Confidence.PROBABLE  # unseen goroutines may hold the sender


def test_mutex_convoy():
    sem = "sem:0xc00001c0a8"
    gs = [_g(i, GoroutineState.MUTEX_WAIT, "mutex", sem) for i in (1, 2, 3)]
    snap = analyze(gs, uncollected=0, warnings=())
    assert any(f.kind is GoFindingKind.MUTEX_CONVOY for f in snap.findings)


def test_waitgroup_wait_cluster_is_not_a_mutex_convoy():
    # Regression: WaitGroup.Wait resolves to a semaphore address just like
    # a real mutex (collector prefixes both "sem:"), but its operation is
    # "waitgroup" not "mutex" -- 3+ goroutines fanning in on one WaitGroup
    # is ordinary synchronization, not a mutex convoy.
    wg = "sem:0xc00001c0a8"
    gs = [_g(i, GoroutineState.WAITGROUP_WAIT, "waitgroup", wg) for i in (1, 2, 3)]
    snap = analyze(gs, uncollected=0, warnings=())
    assert all(f.kind is not GoFindingKind.MUTEX_CONVOY for f in snap.findings)


def test_likely_leak_needs_a_cluster():
    gs = [_g(i, GoroutineState.CHAN_RECV, "recv", CH) for i in range(1, 6)] + [
        _g(99, GoroutineState.RUNNING)
    ]
    snap = analyze(gs, uncollected=0, warnings=())
    kinds = {f.kind for f in snap.findings}
    assert GoFindingKind.LIKELY_LEAK in kinds
