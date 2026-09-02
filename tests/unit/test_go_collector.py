"""tests/unit/test_go_collector.py"""

import pytest

from tdb.dap.types import Source, StackFrame, Thread
from tdb.go_concurrency.collector import GoConcurrencyCollector
from tdb.go_concurrency.models import GoroutineState
from tdb.session.errors import SessionGateError
from tdb.session.state import SessionPhase


class FakeClient:
    def __init__(self, threads, stacks, evals=None, fail_stacks=()):
        self._threads = threads
        self._stacks = stacks
        self._evals = evals or {}
        self._fail = set(fail_stacks)

    async def threads(self):
        return self._threads

    async def stack_trace(self, thread_id, levels=64):
        if thread_id in self._fail:
            raise RuntimeError("boom")
        return self._stacks[thread_id]

    async def evaluate_raw(self, expr, frame_id=None, context="watch"):
        return self._evals.get((frame_id, expr), {"result": "nil"})


class FakeState:
    is_terminated = False
    phase = SessionPhase.STOPPED


class FakeController:
    def __init__(self, client):
        self.client = client
        self.state = FakeState()


def _frames(*names):
    return [
        StackFrame(id=100 + i, name=n, source=Source(path="/w/m.go"), line=1)
        for i, n in enumerate(names)
    ]


@pytest.mark.asyncio
async def test_collect_classifies_and_extracts_channel():
    threads = [
        Thread(id=1, name="* [Go 1] main.main"),
        Thread(id=2, name="[Go 5] main.worker"),
    ]
    stacks = {
        1: _frames("main.main"),
        2: _frames("runtime.gopark", "runtime.chanrecv", "main.worker"),
    }
    # park frame for thread 2 is index 1 -> frame id 101; `c` evaluates
    # with a memoryReference carrying the channel address.
    evals = {
        (101, "c"): {
            "result": "*runtime.hchan {...}",
            "memoryReference": "0xc000024180",
        }
    }
    snap = await GoConcurrencyCollector().collect(
        FakeController(FakeClient(threads, stacks, evals))
    )
    by_id = {g.thread_id: g for g in snap.goroutines}
    assert by_id[1].state is GoroutineState.RUNNING
    assert by_id[1].goid == 1
    assert by_id[2].state is GoroutineState.CHAN_RECV
    assert by_id[2].resource_id == "chan:0xc000024180"
    assert snap.uncollected == 0


@pytest.mark.asyncio
async def test_collect_caps_and_reports_uncollected():
    threads = [Thread(id=i, name=f"[Go {i}] main.w") for i in range(1, 12)]
    stacks = {i: _frames("main.w") for i in range(1, 12)}
    snap = await GoConcurrencyCollector(max_goroutines=10).collect(
        FakeController(FakeClient(threads, stacks))
    )
    assert len(snap.goroutines) == 10
    assert snap.uncollected == 1


@pytest.mark.asyncio
async def test_stack_failure_degrades_to_unknown():
    threads = [Thread(id=1, name="[Go 1] main.main")]
    snap = await GoConcurrencyCollector().collect(
        FakeController(FakeClient(threads, {}, fail_stacks={1}))
    )
    assert snap.goroutines[0].state is GoroutineState.UNKNOWN
    assert snap.warnings  # degradation is surfaced


@pytest.mark.asyncio
async def test_gate_raises_when_running():
    class RunningState(FakeState):
        phase = SessionPhase.RUNNING

    ctrl = FakeController(FakeClient([], {}))
    ctrl.state = RunningState()
    with pytest.raises(SessionGateError):
        await GoConcurrencyCollector().collect(ctrl)
