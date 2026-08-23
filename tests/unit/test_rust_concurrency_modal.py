"""Widget coverage for the Rust concurrency inspection workspace."""

from __future__ import annotations

from textual.app import App

from tdb.rust_concurrency.models import (
    ConcurrencySnapshot,
    Confidence,
    Evidence,
    Finding,
    FindingKind,
    Primitive,
    PrimitiveKind,
    ThreadAnalysis,
    ThreadState,
    WaitEdge,
)
from tdb.widgets.rust_concurrency_modal import RustConcurrencyModal


def sample_snapshot() -> ConcurrencySnapshot:
    """A hand-built stopped Rust program with one evidenced mutex wait."""
    evidence = Evidence(
        confidence=Confidence.CONFIRMED,
        source="lldb-dap",
        detail="mutex owner was observed by the adapter",
    )
    edge = WaitEdge(
        waiter_thread_id=1,
        primitive_id="mutex:0x1",
        owner_thread_id=2,
        operation="lock",
        evidence=(evidence,),
    )
    return ConcurrencySnapshot(
        rust_version="1.98.0",
        adapter="lldb-dap",
        platform="linux",
        threads=(
            ThreadAnalysis(1, "main", ThreadState.BLOCKED, edge),
            ThreadAnalysis(2, "worker", ThreadState.RUNNING, None),
        ),
        primitives=(
            Primitive(
                primitive_id="mutex:0x1",
                kind=PrimitiveKind.MUTEX,
                address="0x1",
                label="work lock",
                evidence=(evidence,),
            ),
        ),
        edges=(edge,),
        confirmed_deadlocks=(
            Finding(
                FindingKind.CONFIRMED_DEADLOCK,
                (1, 2),
                "main and worker are deadlocked",
                (),
            ),
        ),
        suspected_stalls=(
            Finding(
                FindingKind.WHOLE_PROGRAM_STALL,
                (1,),
                "all observed workers are blocked",
                ("the runtime may have unobserved workers",),
            ),
        ),
        warnings=(),
    )


class _ModalApp(App[None]):
    pass


async def test_modal_has_three_tabs():
    """Removing any workspace tab would hide part of the Rust snapshot."""
    app = _ModalApp()
    async with app.run_test() as pilot:
        modal = RustConcurrencyModal(sample_snapshot(), current_thread_id=1)
        app.push_screen(modal)
        await pilot.pause()
        assert modal.query_one("#threads-tab")
        assert modal.query_one("#wait-graph-tab")
        assert modal.query_one("#findings-tab")
