"""Real stopped Rust wait snapshots, including conservative findings."""

from __future__ import annotations

import pytest

from tdb.rust_concurrency.models import FindingKind
from tdb.session.inspect_service import InspectService
from tests.integration.rust_adapter_harness import (
    available_rust_adapters,
    launch_and_pause,
    require_supported_rust_concurrency,
    _rust_debug_binary,  # noqa: F401 - registers rust_debug_binary fixture
)


WAIT_SCENARIOS = (
    ("join", "join"),
    ("mutex", "mutex-lock"),
    ("rwlock-read", "rwlock-read"),
    ("rwlock-write", "rwlock-write"),
    ("condvar", "condvar-wait"),
    ("mpsc-send", "mpsc-send"),
    ("mpsc-recv", "mpsc-recv"),
    ("park", "park"),
)


async def _snapshot(case: str, adapter: str, rust_debug_binary):
    ctrl = await launch_and_pause(rust_debug_binary(case, adapter), adapter)
    try:
        return await InspectService(lambda: ctrl).collect_rust_concurrency()
    finally:
        await ctrl.stop()


@pytest.mark.parametrize("case,operation", WAIT_SCENARIOS)
@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_wait_scenario_identifies_exact_operation(
    case, operation, adapter, rust_debug_binary
):
    snapshot = await _snapshot(case, adapter, rust_debug_binary)

    assert any(edge.operation == operation for edge in snapshot.edges)


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_cycle_reports_confirmed_deadlock_when_layout_is_supported(
    adapter, rust_debug_binary
):
    require_supported_rust_concurrency()
    snapshot = await _snapshot("cycle", adapter, rust_debug_binary)

    assert snapshot.confirmed_deadlocks


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_incomplete_cycle_is_never_promoted_to_confirmed(
    adapter, rust_debug_binary
):
    snapshot = await _snapshot("incomplete-cycle", adapter, rust_debug_binary)

    assert snapshot.confirmed_deadlocks == ()
    assert any(
        finding.kind is FindingKind.WHOLE_PROGRAM_STALL
        for finding in snapshot.suspected_stalls
    )


@pytest.mark.parametrize("adapter", available_rust_adapters())
async def test_healthy_blocked_has_exactly_no_deadlock_or_stall_findings(
    adapter, rust_debug_binary
):
    snapshot = await _snapshot("healthy-blocked", adapter, rust_debug_binary)

    assert any(edge.operation == "park" for edge in snapshot.edges)
    assert snapshot.confirmed_deadlocks == ()
    assert snapshot.suspected_stalls == ()
