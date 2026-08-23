"""Graph analysis contracts for Rust concurrency snapshots."""

from __future__ import annotations

from tdb.rust_concurrency.analyzer import analyze, find_confirmed_cycles, find_suspected_stalls
from tdb.rust_concurrency.models import (
    Confidence,
    Evidence,
    FindingKind,
    ProbePrimitiveState,
    ProbeResult,
    ProbeThread,
    RawFrame,
    RawSnapshot,
    RawThread,
    RawVariable,
)


def _mutex_thread(thread_id: int, address: str) -> RawThread:
    return RawThread(
        thread_id=thread_id,
        name=f"worker-{thread_id}",
        os_thread_id=str(thread_id),
        frames=(
            RawFrame(
                frame_id=0,
                name="std::sync::poison::mutex::Mutex<T>::lock",
                source_path=None,
                line=0,
                variables=(RawVariable("mutex", address, "Mutex<u8>"),),
            ),
        ),
    )


def raw_two_mutex_cycle(
    confidences: tuple[str, str],
) -> tuple[RawSnapshot, ProbeResult]:
    """Build explicit waiter and owner evidence for a two-mutex cycle."""
    first, second = (Confidence(value) for value in confidences)
    raw = RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=(_mutex_thread(1, "0x10"), _mutex_thread(2, "0x20")),
    )
    probe = ProbeResult(
        rust_version="1.98.0",
        primitive_states=(
            ProbePrimitiveState(
                primitive_id="mutex:0x10",
                owner_os_thread_ids=("2",),
                raw_state="locked",
                evidence=(Evidence(first, "probe-owner", "owner thread 2"),),
            ),
            ProbePrimitiveState(
                primitive_id="mutex:0x20",
                owner_os_thread_ids=("1",),
                raw_state="locked",
                evidence=(Evidence(second, "probe-owner", "owner thread 1"),),
            ),
        ),
    )
    return raw, probe


def raw_all_blocked_with_unknown_owners() -> tuple[RawSnapshot, ProbeResult]:
    """Build blocked waits with no owner links; analysis must not invent them."""
    raw = RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=(_mutex_thread(1, "0x10"), _mutex_thread(2, "0x20")),
    )
    probe = ProbeResult(
        rust_version="1.98.0",
        primitive_states=(
            ProbePrimitiveState(
                primitive_id="mutex:0x10",
                owner_os_thread_ids=(),
                raw_state="locked",
                evidence=(Evidence(Confidence.UNKNOWN, "probe-owner", "owner unavailable"),),
            ),
            ProbePrimitiveState(
                primitive_id="mutex:0x20",
                owner_os_thread_ids=(),
                raw_state="locked",
                evidence=(Evidence(Confidence.UNKNOWN, "probe-owner", "owner unavailable"),),
            ),
        ),
    )
    return raw, probe


def raw_with_housekeeping_thread(
    name: str,
) -> tuple[RawSnapshot, ProbeResult]:
    """Build a blocked application thread plus explicit LLDB housekeeping."""
    raw, probe = raw_all_blocked_with_unknown_owners()
    return (
        RawSnapshot(
            adapter="lldb-dap",
            platform="linux",
            rust_version=raw.rust_version,
            threads=(
                raw.threads[0],
                RawThread(thread_id=99, name=name, os_thread_id="99", frames=()),
            ),
        ),
        ProbeResult(
            rust_version=probe.rust_version,
            primitive_states=(probe.primitive_states[0],),
        ),
    )


def test_confirmed_cycle_requires_every_confirmed_edge():
    raw, probe = raw_two_mutex_cycle(("confirmed", "confirmed"))

    snapshot = analyze(raw, probe)

    assert len(snapshot.confirmed_deadlocks) == 1
    assert snapshot.confirmed_deadlocks[0].thread_ids == (1, 2)
    assert snapshot.suspected_stalls == ()


def test_probable_cycle_is_suspected_not_confirmed():
    raw, probe = raw_two_mutex_cycle(("confirmed", "probable"))

    snapshot = analyze(raw, probe)

    assert snapshot.confirmed_deadlocks == ()
    assert snapshot.suspected_stalls[0].kind is FindingKind.SUSPECTED_CYCLE
    assert snapshot.suspected_stalls[0].evidence_gaps == (
        "thread 2 -> mutex:0x20 has only probable evidence",
    )


def test_all_application_threads_blocked_is_whole_program_stall():
    raw, probe = raw_all_blocked_with_unknown_owners()

    snapshot = analyze(raw, probe)

    assert snapshot.suspected_stalls[0].kind is FindingKind.WHOLE_PROGRAM_STALL
    assert snapshot.suspected_stalls[0].thread_ids == (1, 2)
    assert snapshot.suspected_stalls[0].evidence_gaps == (
        "thread 1 -> mutex:0x10 has no observed owner",
        "thread 2 -> mutex:0x20 has no observed owner",
    )


def test_runtime_housekeeping_thread_does_not_prevent_stall():
    raw, probe = raw_with_housekeeping_thread(
        "lldb.process.internal-state-coordinator"
    )

    snapshot = analyze(raw, probe)

    assert snapshot.suspected_stalls
    assert snapshot.suspected_stalls[0].kind is FindingKind.WHOLE_PROGRAM_STALL
    assert snapshot.suspected_stalls[0].thread_ids == (1,)


def test_confirmed_cycle_does_not_hide_other_blocked_application_threads():
    raw, probe = raw_two_mutex_cycle(("confirmed", "confirmed"))
    extra = _mutex_thread(3, "0x30")
    raw = RawSnapshot(
        adapter=raw.adapter,
        platform=raw.platform,
        rust_version=raw.rust_version,
        threads=raw.threads + (extra,),
    )
    probe = ProbeResult(
        rust_version=probe.rust_version,
        primitive_states=probe.primitive_states
        + (
            ProbePrimitiveState(
                primitive_id="mutex:0x30",
                owner_os_thread_ids=(),
                raw_state="locked",
                evidence=(
                    Evidence(
                        Confidence.UNKNOWN,
                        "probe-owner",
                        "owner unavailable",
                    ),
                ),
            ),
        ),
    )

    snapshot = analyze(raw, probe)

    assert len(snapshot.confirmed_deadlocks) == 1
    assert any(
        finding.kind is FindingKind.WHOLE_PROGRAM_STALL
        and finding.thread_ids == (1, 2, 3)
        for finding in snapshot.suspected_stalls
    )


def test_unresolved_probe_owner_never_creates_a_wait_relationship():
    raw, probe = raw_two_mutex_cycle(("confirmed", "confirmed"))
    probe = ProbeResult(
        rust_version=probe.rust_version,
        primitive_states=(
            ProbePrimitiveState(
                primitive_id="mutex:0x10",
                owner_os_thread_ids=("missing",),
                raw_state="locked",
                evidence=(Evidence(Confidence.CONFIRMED, "probe-owner", "unknown"),),
            ),
        ),
    )

    snapshot = analyze(raw, probe)

    assert all(edge.owner_thread_id is None for edge in snapshot.edges)
    assert snapshot.confirmed_deadlocks == ()


def test_public_graph_helpers_return_deterministic_findings():
    raw, probe = raw_two_mutex_cycle(("confirmed", "confirmed"))
    snapshot = analyze(raw, probe)

    assert find_confirmed_cycles(snapshot.edges) == snapshot.confirmed_deadlocks
    assert find_suspected_stalls(
        snapshot.threads, snapshot.edges, snapshot.confirmed_deadlocks
    ) == ()


def test_snapshot_graph_order_is_independent_of_debugger_thread_order():
    raw, probe = raw_two_mutex_cycle(("confirmed", "confirmed"))
    reversed_raw = RawSnapshot(
        adapter=raw.adapter,
        platform=raw.platform,
        rust_version=raw.rust_version,
        threads=tuple(reversed(raw.threads)),
    )

    snapshot = analyze(reversed_raw, probe)

    assert [thread.thread_id for thread in snapshot.threads] == [1, 2]
    assert [edge.waiter_thread_id for edge in snapshot.edges] == [1, 2]


def test_confirmed_cycle_search_keeps_alternate_cycles_through_shared_nodes():
    from tdb.rust_concurrency.models import WaitEdge

    confirmed = (Evidence(Confidence.CONFIRMED, "probe-owner", "owner"),)
    edges = tuple(
        WaitEdge(waiter, f"mutex:0x{index}", owner, "mutex-lock", confirmed)
        for index, (waiter, owner) in enumerate(
            ((1, 2), (1, 3), (2, 3), (3, 1)), start=1
        )
    )

    findings = find_confirmed_cycles(edges)

    assert {finding.thread_ids for finding in findings} == {(1, 2, 3), (1, 3)}


def test_duplicate_thread_names_do_not_resolve_probe_owner_hint():
    raw = RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=(
            RawThread(1, "worker", frames=_mutex_thread(1, "0x10").frames),
            RawThread(2, "worker", frames=_mutex_thread(2, "0x20").frames),
        ),
    )
    probe = ProbeResult(
        rust_version="1.98.0",
        threads=(ProbeThread("worker", "os-owner"),),
        primitive_states=(
            ProbePrimitiveState(
                primitive_id="mutex:0x10",
                owner_os_thread_ids=("os-owner",),
                raw_state="locked",
                evidence=(Evidence(Confidence.CONFIRMED, "probe-owner", "owner"),),
            ),
        ),
    )

    snapshot = analyze(raw, probe)

    assert snapshot.edges[0].owner_thread_id is None
    assert snapshot.confirmed_deadlocks == ()


def test_dense_owner_graph_caps_cycles_and_warns():
    size = 8
    raw = RawSnapshot(
        adapter="gdb",
        platform="linux",
        rust_version="1.98.0",
        threads=tuple(_mutex_thread(index, f"0x{index:x}") for index in range(1, size + 1)),
    )
    owners = tuple(str(index) for index in range(1, size + 1))
    confirmed = (Evidence(Confidence.CONFIRMED, "probe-owner", "owner set"),)
    probe = ProbeResult(
        rust_version="1.98.0",
        primitive_states=tuple(
            ProbePrimitiveState(
                primitive_id=f"mutex:0x{index:x}",
                owner_os_thread_ids=owners,
                raw_state="locked",
                evidence=confirmed,
            )
            for index in range(1, size + 1)
        ),
    )

    snapshot = analyze(raw, probe)

    assert len(snapshot.confirmed_deadlocks) == 256
    assert "Wait-cycle analysis truncated after 256 cycles." in snapshot.warnings


def test_dense_acyclic_owner_graph_has_no_cycles_or_truncation_warning():
    from tdb.rust_concurrency.models import WaitEdge

    confirmed = (Evidence(Confidence.CONFIRMED, "probe-owner", "owner"),)
    edges = tuple(
        WaitEdge(waiter, f"mutex:{waiter}:{owner}", owner, "mutex-lock", confirmed)
        for waiter in range(1, 19)
        for owner in range(waiter + 1, 19)
    )

    assert find_confirmed_cycles(edges) == ()
