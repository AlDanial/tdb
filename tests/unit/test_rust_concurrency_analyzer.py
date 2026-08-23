"""Graph analysis contracts for Rust concurrency snapshots."""

from __future__ import annotations

from tdb.rust_concurrency.analyzer import analyze, find_confirmed_cycles, find_suspected_stalls
from tdb.rust_concurrency.models import (
    Confidence,
    Evidence,
    FindingKind,
    ProbePrimitiveState,
    ProbeResult,
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
