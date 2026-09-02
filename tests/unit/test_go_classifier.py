"""tests/unit/test_go_classifier.py"""

from tdb.go_concurrency.classifier import classify_stack
from tdb.go_concurrency.models import GoroutineState

CHAN_RECV = [
    "runtime.gopark",
    "runtime.chanrecv",
    "runtime.chanrecv1",
    "main.worker",
    "runtime.goexit",
]
CHAN_SEND = ["runtime.gopark", "runtime.chansend", "runtime.chansend1", "main.feed"]
SELECT = ["runtime.gopark", "runtime.selectgo", "main.mux"]
MUTEX = [
    "runtime.gopark",
    "runtime.goparkunlock",
    "runtime.semacquire1",
    "sync.runtime_SemacquireMutex",
    "sync.(*Mutex).lockSlow",
    "sync.(*Mutex).Lock",
    "main.critical",
]
WAITGROUP = [
    "runtime.gopark",
    "runtime.semacquire1",
    "sync.runtime_Semacquire",
    "sync.(*WaitGroup).Wait",
    "main.main",
]
SLEEP = ["runtime.gopark", "time.Sleep", "main.napper"]
SYSCALL = [
    "syscall.Syscall6",
    "internal/poll.(*FD).Read",
    "os.(*File).Read",
    "main.reader",
]
RUNNING = ["main.crunch", "main.main"]
GC = ["runtime.gopark", "runtime.gcBgMarkWorker"]


def _c(frames):
    return classify_stack(frames)


def test_chan_recv():
    c = _c(CHAN_RECV)
    assert c.state is GoroutineState.CHAN_RECV
    assert c.operation == "recv"
    assert CHAN_RECV[c.park_frame_index] == "runtime.chanrecv"
    assert c.target_expr == "c"


def test_chan_send():
    c = _c(CHAN_SEND)
    assert c.state is GoroutineState.CHAN_SEND
    assert c.operation == "send"
    assert c.target_expr == "c"


def test_select_has_state_but_no_target():
    c = _c(SELECT)
    assert c.state is GoroutineState.SELECT
    assert c.target_expr is None  # scases enumeration deferred (spec)


def test_mutex():
    c = _c(MUTEX)
    assert c.state is GoroutineState.MUTEX_WAIT
    assert MUTEX[c.park_frame_index] == "sync.runtime_SemacquireMutex"
    assert c.target_expr == "addr"


def test_waitgroup():
    c = _c(WAITGROUP)
    assert c.state is GoroutineState.WAITGROUP_WAIT
    assert c.target_expr == "addr"


def test_sleep_syscall_running_runtime():
    assert _c(SLEEP).state is GoroutineState.SLEEP
    assert _c(SYSCALL).state is GoroutineState.SYSCALL
    assert _c(RUNNING).state is GoroutineState.RUNNING
    assert _c(GC).state is GoroutineState.RUNTIME


def test_empty_stack_is_unknown():
    assert _c([]).state is GoroutineState.UNKNOWN


def test_snapshot_to_dict_roundtrips():
    from tdb.go_concurrency.models import (
        Confidence,
        GoFinding,
        GoFindingKind,
        GoResource,
        GoWaitEdge,
        GoroutineInfo,
        GoroutineSnapshot,
    )

    snap = GoroutineSnapshot(
        goroutines=(
            GoroutineInfo(
                thread_id=3,
                goid=5,
                function="main.worker",
                state=GoroutineState.CHAN_RECV,
                operation="recv",
                resource_id="chan:0xc000024180",
                frames=("runtime.gopark", "main.worker"),
                is_runtime=False,
            ),
        ),
        resources=(GoResource("chan:0xc000024180", "channel", "chan 0xc000024180"),),
        edges=(GoWaitEdge(3, "chan:0xc000024180", "recv"),),
        findings=(
            GoFinding(
                GoFindingKind.STUCK_CHANNEL,
                (3,),
                "1 goroutine receiving on chan 0xc000024180 with no sender",
                Confidence.PROBABLE,
            ),
        ),
        uncollected=0,
        warnings=(),
    )
    d = snap.to_dict()
    assert d["goroutines"][0]["state"] == "chan_recv"
    assert d["findings"][0]["kind"] == "stuck_channel"
    assert d["findings"][0]["confidence"] == "probable"
    assert d["uncollected"] == 0
