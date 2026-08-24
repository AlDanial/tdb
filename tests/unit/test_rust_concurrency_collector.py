"""Bounded, stopped-state Rust concurrency collection contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tdb.dap.types import Scope, StackFrame, Thread, Variable
from tdb.languages.rust import build_rust_profile
from tdb.rust_concurrency.collector import RustConcurrencyCollector
from tdb.session.inspect_service import InspectService, SessionGateError
from tdb.session.state import DebugState, SessionPhase


def _controller(client: object) -> SimpleNamespace:
    state = DebugState()
    state.transition_to(SessionPhase.STOPPED)
    return SimpleNamespace(
        client=client,
        state=state,
        profile=build_rust_profile(adapter="gdb"),
    )


def _client() -> SimpleNamespace:
    async def stack_trace(thread_id, start_frame=0, levels=20):
        await asyncio.sleep(0)
        return [
            StackFrame(
                thread_id * 10,
                "std::sync::poison::mutex::Mutex<T>::lock"
                if thread_id == 1
                else "worker::run",
            )
        ]

    return SimpleNamespace(
        threads=AsyncMock(return_value=[Thread(2, "worker-2"), Thread(1, "worker-1")]),
        stack_trace=AsyncMock(side_effect=stack_trace),
        scopes=AsyncMock(return_value=[Scope("Locals", 11)]),
        variables=AsyncMock(return_value=[Variable("mutex", "0x10", "Mutex<u8>")]),
    )


async def test_collector_fetches_bounded_frames_for_every_thread():
    client = _client()
    result = await RustConcurrencyCollector(max_frames=32, max_variables=128).collect(
        _controller(client)
    )

    assert [thread.thread_id for thread in result.threads] == [1, 2]
    client.stack_trace.assert_any_await(1, start_frame=0, levels=32)
    # Only the Rust wait frame is expanded; the ordinary application frame is not.
    assert client.scopes.await_count == 1
    assert client.variables.await_count == 1


async def test_collector_preserves_partial_thread_failures_as_warnings():
    client = _client()
    client.stack_trace.side_effect = RuntimeError("stack unavailable")

    result = await RustConcurrencyCollector().collect(_controller(client))

    assert [thread.thread_id for thread in result.threads] == [1, 2]
    assert any("thread 1" in warning for warning in result.warnings)
    assert all(thread.frames == () for thread in result.threads)


async def test_collector_discards_result_if_session_resumes():
    client = _client()
    controller = _controller(client)

    async def resume() -> list[Thread]:
        await asyncio.sleep(0)
        controller.state.transition_to(SessionPhase.RUNNING)
        return [Thread(1, "worker-1")]

    client.threads.side_effect = resume
    with pytest.raises(SessionGateError, match="running"):
        await InspectService(lambda: controller).collect_rust_concurrency()


async def test_collector_discards_result_if_session_resumes_and_stops_again():
    client = _client()
    controller = _controller(client)

    async def resume_and_stop() -> list[Thread]:
        controller.state.transition_to(SessionPhase.RUNNING)
        controller.state.transition_to(SessionPhase.STOPPED)
        return [Thread(1, "worker-1")]

    client.threads.side_effect = resume_and_stop
    with pytest.raises(SessionGateError, match="running"):
        await RustConcurrencyCollector().collect(controller)


async def test_probe_timeout_preserves_base_snapshot():
    client = _client()
    controller = _controller(client)

    async def slow_probe(_client):
        await asyncio.sleep(1)

    snapshot = await RustConcurrencyCollector(
        probe=slow_probe, probe_timeout=0.01
    ).collect_and_analyze(controller)

    assert snapshot.threads
    assert "probe timed out" in snapshot.warnings[0]


async def test_collector_discards_result_if_session_resumes_during_probe():
    client = _client()
    controller = _controller(client)

    async def resuming_probe(_client):
        controller.state.transition_to(SessionPhase.RUNNING)
        return None

    with pytest.raises(SessionGateError, match="running"):
        await RustConcurrencyCollector(probe=resuming_probe).collect_and_analyze(
            controller
        )


async def test_collector_enforces_variable_cap_when_adapter_over_returns():
    client = _client()
    client.variables.return_value = [
        Variable("first", "0x10", "Mutex<u8>"),
        Variable("second", "0x20", "Mutex<u8>"),
    ]

    raw = await RustConcurrencyCollector(max_variables=1).collect(_controller(client))

    assert [variable.name for variable in raw.threads[0].frames[0].variables] == [
        "first"
    ]


async def test_collector_sorts_threads_before_applying_cap():
    client = _client()
    client.threads.return_value = [
        Thread(3, "three"),
        Thread(1, "one"),
        Thread(2, "two"),
    ]

    raw = await RustConcurrencyCollector(max_threads=2).collect(_controller(client))

    assert [thread.thread_id for thread in raw.threads] == [1, 2]


async def test_collector_propagates_cancellation():
    client = _client()
    controller = _controller(client)
    task = asyncio.create_task(RustConcurrencyCollector().collect(controller))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_whole_snapshot_deadline_bounds_hung_stack_request():
    client = _client()

    async def hung_stack(*_args, **_kwargs):
        await asyncio.Event().wait()

    client.stack_trace.side_effect = hung_stack
    snapshot = await RustConcurrencyCollector(
        snapshot_timeout=0.03
    ).collect_and_analyze(_controller(client))

    assert [thread.thread_id for thread in snapshot.threads] == [1, 2]
    assert "snapshot timed out after 0.03s" in snapshot.warnings


async def test_whole_snapshot_deadline_bounds_hung_scope_request():
    client = _client()

    async def hung_scopes(_frame_id):
        await asyncio.Event().wait()

    client.scopes.side_effect = hung_scopes
    snapshot = await RustConcurrencyCollector(
        snapshot_timeout=0.03
    ).collect_and_analyze(_controller(client))

    assert [thread.thread_id for thread in snapshot.threads] == [1, 2]
    assert "snapshot timed out after 0.03s" in snapshot.warnings


async def test_resume_cancels_hung_scope_request_promptly():
    client = _client()
    controller = _controller(client)
    scope_started = asyncio.Event()

    async def hung_scopes(_frame_id):
        scope_started.set()
        await asyncio.Event().wait()

    client.scopes.side_effect = hung_scopes
    task = asyncio.create_task(
        RustConcurrencyCollector(snapshot_timeout=5).collect_and_analyze(controller)
    )
    await asyncio.wait_for(scope_started.wait(), timeout=1)
    controller.state.transition_to(SessionPhase.RUNNING)

    with pytest.raises(SessionGateError, match="running"):
        await asyncio.wait_for(task, timeout=0.2)
