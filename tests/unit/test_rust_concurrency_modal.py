"""Widget coverage for the Rust concurrency inspection workspace."""

from __future__ import annotations

from textual.app import App

from tdb.dap.types import Scope, Source, StackFrame, Variable

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


async def test_thread_highlight_requests_live_detail():
    """Removing live-detail loading would leave the locals pane permanently empty."""
    app = _ModalApp()
    async with app.run_test() as pilot:
        modal = RustConcurrencyModal(sample_snapshot(), current_thread_id=1)
        messages = []
        original_post_message = modal.post_message

        def record(message):
            messages.append(message)
            return original_post_message(message)

        modal.post_message = record  # type: ignore[method-assign]
        app.push_screen(modal)
        await pilot.pause()

        assert any(
            isinstance(message, RustConcurrencyModal.LoadThreadDetail)
            and message.thread_id == 1
            for message in messages
        )


async def test_live_detail_populates_locals_and_frame_selection_posts_message():
    """A Rust workspace frame must be selectable, not just painted as text."""
    app = _ModalApp()
    async with app.run_test() as pilot:
        modal = RustConcurrencyModal(sample_snapshot(), current_thread_id=1)
        messages = []
        original_post_message = modal.post_message

        def record(message):
            messages.append(message)
            return original_post_message(message)

        modal.post_message = record  # type: ignore[method-assign]
        app.push_screen(modal)
        await pilot.pause()

        modal.show_thread_detail(
            1,
            [
                StackFrame(
                    id=41,
                    name="wait_for_work",
                    source=Source(path="/src/main.rs"),
                    line=12,
                )
            ],
            [Scope(name="Locals", variables_reference=9)],
            {9: [Variable(name="guard", value="MutexGuard", variables_reference=0)]},
        )
        frames = modal.query_one("#frames-table")
        frames.focus()
        await pilot.press("enter")
        await pilot.pause()

        variables = modal.query_one("#vars")
        scope = next(
            node for node in variables.root.children if "Locals" in str(node.label)
        )
        assert any("guard" in str(node.label) for node in scope.children)
        assert any(
            isinstance(message, RustConcurrencyModal.SelectFrame)
            and message.thread_id == 1
            and message.frame_id == 41
            for message in messages
        )


async def test_small_viewport_keeps_live_frames_and_locals_visible():
    """At a terminal-sized viewport, detail controls need usable rows."""
    app = _ModalApp()
    async with app.run_test(size=(100, 24)) as pilot:
        modal = RustConcurrencyModal(sample_snapshot(), current_thread_id=1)
        app.push_screen(modal)
        await pilot.pause()
        modal.show_thread_detail(
            1,
            [
                StackFrame(
                    id=41,
                    name="wait_for_work",
                    source=Source(path="/src/main.rs"),
                    line=12,
                )
            ],
            [Scope(name="Locals", variables_reference=9)],
            {9: [Variable(name="guard", value="MutexGuard", variables_reference=0)]},
        )
        await pilot.pause()

        assert modal.query_one("#frames-table").size.height >= 3
        assert modal.query_one("#vars").size.height >= 3


async def test_warnings_are_prominent_and_refresh_with_snapshot():
    app = _ModalApp()
    async with app.run_test() as pilot:
        initial = sample_snapshot()
        modal = RustConcurrencyModal(initial, current_thread_id=1)
        app.push_screen(modal)
        await pilot.pause()
        degraded = ConcurrencySnapshot(
            rust_version=None,
            adapter=initial.adapter,
            platform=initial.platform,
            threads=initial.threads,
            primitives=initial.primitives,
            edges=initial.edges,
            confirmed_deadlocks=(),
            suspected_stalls=initial.suspected_stalls,
            warnings=(
                "unsupported Rust unknown; layout evidence disabled",
                "probe failed: command unavailable",
            ),
        )

        modal.update_snapshot(degraded)
        await pilot.pause()

        assert "2 warning(s)" in str(modal.query_one("#header").render())
        findings = str(modal.query_one("#findings-list").render())
        assert "unsupported Rust unknown" in findings
        assert "probe failed" in findings


def test_edge_label_uses_strongest_evidence():
    edge = sample_snapshot().edges[0]
    mixed = WaitEdge(
        waiter_thread_id=edge.waiter_thread_id,
        primitive_id=edge.primitive_id,
        owner_thread_id=edge.owner_thread_id,
        operation=edge.operation,
        evidence=(
            Evidence(Confidence.UNKNOWN, "stack", "unknown"),
            Evidence(Confidence.CONFIRMED, "probe", "guard observed"),
        ),
    )

    modal = object.__new__(RustConcurrencyModal)
    assert "[confirmed]" in str(modal._edge_label(mixed))
