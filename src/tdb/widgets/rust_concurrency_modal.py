"""Rust-specific concurrency inspection workspace.

The generic Threads modal remains the normal DAP thread browser.  Rust's
concurrency collector has richer, immutable wait-graph evidence, so this
screen renders that one snapshot coherently across its three tabs.

Structurally this is an `_InspectableListModal` (threads list + detail
pane) whose body is wrapped in a `TabbedContent` and extended with a
frames table in the detail pane plus two extra tabs (wait graph,
findings) rendered from the same snapshot.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Label, Static, TabbedContent, TabPane, Tree

from rich.text import Text

from tdb.rust_concurrency.models import (
    Confidence,
    ConcurrencySnapshot,
    Finding,
    ThreadAnalysis,
    WaitEdge,
)
from tdb.widgets._inspection_modal import _InspectableListModal
from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Scope, StackFrame, Variable


class RustConcurrencyModal(_InspectableListModal[ThreadAnalysis]):
    """Near-full-screen view of one immutable Rust concurrency snapshot."""

    KIND_LABEL = "Rust Concurrency"
    TABLE_COLUMNS = ("ID", "Name", "State", "Wait")
    FOOTER_HINT = "ESC close  |  r refresh  |  Enter select  |  arrows/tab navigate"

    # Additions to the base skeleton's CSS: tab sizing, the compact
    # evidence pane, and the two graph/findings tabs.
    DEFAULT_CSS = """
    RustConcurrencyModal TabbedContent { height: 1fr; }
    RustConcurrencyModal #info {
        height: 5;
        max-height: 5;
        padding: 0 2;
        overflow-y: auto;
    }
    RustConcurrencyModal #frames-table {
        height: 1fr;
        min-height: 3;
    }
    RustConcurrencyModal #vars {
        height: 1fr;
        min-height: 3;
    }
    RustConcurrencyModal #wait-graph-tree { height: 1fr; width: 1fr; }
    RustConcurrencyModal #wait-edge-list, RustConcurrencyModal #findings-list {
        padding: 1 2;
        overflow-y: auto;
        height: 1fr;
    }
    """

    def __init__(
        self,
        snapshot: ConcurrencySnapshot,
        current_thread_id: int | None,
    ) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._current_thread_id = current_thread_id
        self._items: list[ThreadAnalysis] = list(snapshot.threads)
        self._detail_thread_id: int | None = None
        self._detail_frames: list[StackFrame] = []

    # --- Compose / mount ---------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._header_text(), id="header")
            with TabbedContent(initial="threads-tab"):
                with TabPane("Threads", id="threads-tab"):
                    with Horizontal(id="body"):
                        yield from self._compose_body()
                with TabPane("Wait Graph", id="wait-graph-tab"):
                    yield Tree("Wait graph", id="wait-graph-tree")
                    yield Static(id="wait-edge-list")
                with TabPane("Findings", id="findings-tab"):
                    yield Static(id="findings-list")
            yield Label(self.FOOTER_HINT, id="footer")

    def _compose_body(self) -> ComposeResult:
        with Vertical(id="list-pane"):
            table = DataTable(id="table")
            table.cursor_type = "row"
            yield table
        with Vertical(id="detail-pane"):
            yield Static("", id="info")
            frames = DataTable(id="frames-table")
            frames.cursor_type = "row"
            yield frames
            yield VariableView(id="vars")

    def on_mount(self) -> None:
        frames = self.query_one("#frames-table", DataTable)
        frames.add_columns("#", "Function", "Location")
        tree = self.query_one("#wait-graph-tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3
        super().on_mount()
        self._render_wait_graph()
        self._render_findings()

    # --- Public workflow surface -------------------------------------

    class RefreshSnapshot(Message):
        """Request one fresh Rust concurrency snapshot."""

    class LoadThreadDetail(Message):
        """Request live DAP stack/scopes/variables for a highlighted thread."""

        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class SelectThread(Message):
        """Navigate the main stack/source/locals views to a thread."""

        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class SelectFrame(Message):
        """Navigate the main stack/source/locals views to a frame."""

        def __init__(self, thread_id: int, frame_id: int) -> None:
            self.thread_id = thread_id
            self.frame_id = frame_id
            super().__init__()

    def update_snapshot(self, snapshot: ConcurrencySnapshot) -> None:
        """Atomically replace the model rendered by all three workspace tabs."""
        self._snapshot = snapshot
        self._items = list(snapshot.threads)
        self._update_header()
        if self._items:
            self._populate_table()
            index = self._initial_cursor_index()
            self.query_one("#table", DataTable).move_cursor(row=index)
            self._show_detail(index)
        else:
            self._render_empty_state()
        self._render_wait_graph()
        self._render_findings()

    # --- Base-class contract -----------------------------------------

    def _header_text(self) -> str:
        warning_suffix = (
            f" — {len(self._snapshot.warnings)} warning(s)"
            if self._snapshot.warnings
            else ""
        )
        return (
            f"{self.KIND_LABEL} ({len(self._snapshot.threads)} threads){warning_suffix}"
        )

    def _empty_state_text(self) -> str:
        return "No Rust threads found"

    def _format_row(self, item: ThreadAnalysis) -> tuple:
        thread_id = Text(str(item.thread_id))
        name = Text(item.name)
        if item.thread_id == self._current_thread_id:
            thread_id.stylize("bold")
            name.stylize("bold")
        wait = item.wait.operation if item.wait is not None else "—"
        return (thread_id, name, item.state.value, wait)

    def _initial_cursor_index(self) -> int:
        return next(
            (
                i
                for i, thread in enumerate(self._items)
                if thread.thread_id == self._current_thread_id
            ),
            0,
        )

    def _render_loading_detail(self, item: ThreadAnalysis) -> Text:
        content = Text()
        content.append("Thread ID: ", style="bold")
        content.append(f"{item.thread_id}\n")
        content.append("Name:      ", style="bold")
        content.append(f"{item.name}\n")
        content.append("State:     ", style="bold")
        content.append(f"{item.state.value}\n")
        if item.wait is None:
            content.append("\nNo observed wait relationship.\n", style="dim")
        else:
            content.append("\nWaiting on: ", style="bold")
            content.append(f"{item.wait.primitive_id} ({item.wait.operation})\n")
            if item.wait.owner_thread_id is not None:
                content.append(f"Observed owner: thread {item.wait.owner_thread_id}\n")
            self._append_evidence(content, item.wait.evidence)
        return content

    def _on_after_show_detail(self, item: ThreadAnalysis) -> None:
        """Request the highlighted thread's live DAP stack + locals."""
        self._detail_thread_id = item.thread_id
        self._detail_frames = []
        self.query_one("#frames-table", DataTable).clear()
        variables = self.query_one("#vars", VariableView)
        variables.clear()
        variables.root.add_leaf("Loading live frame locals…")
        self.post_message(self.LoadThreadDetail(item.thread_id))

    def _select_id_for(self, item: ThreadAnalysis) -> int:
        return item.thread_id

    def _make_select_message(self, item_id: int) -> Message:
        return self.SelectThread(item_id)

    def _make_refresh_message(self) -> Message:
        return self.RefreshSnapshot()

    # --- Live thread detail ------------------------------------------

    def show_thread_detail(
        self,
        thread_id: int,
        frames: list[StackFrame],
        scopes: list[Scope],
        variables: dict[int, list[Variable]],
    ) -> None:
        """Render the selected thread's live stack frames and locals."""
        if thread_id != self._detail_thread_id:
            return
        self._detail_frames = frames
        frame_table = self.query_one("#frames-table", DataTable)
        frame_table.clear()
        for index, frame in enumerate(frames):
            location = ""
            if frame.source and frame.source.path:
                location = f"{Path(frame.source.path).name}:{frame.line}"
            elif frame.source and frame.source.name:
                location = f"{frame.source.name}:{frame.line}"
            frame_table.add_row(str(index), frame.name, location)
        if frames:
            frame_table.move_cursor(row=0)

        variable_view = self.query_one("#vars", VariableView)
        if scopes:
            variable_view.update_variables(scopes, variables)
        else:
            variable_view.clear()
            variable_view.root.add_leaf("(no variables available)")

    # --- Wait graph / findings tabs ----------------------------------

    def _render_wait_graph(self) -> None:
        tree = self.query_one("#wait-graph-tree", Tree)
        tree.clear()
        edge_text = Text()
        if not self._snapshot.edges:
            tree.root.add_leaf(Text("No observed waits", style="dim"))
            edge_text.append("No observed wait edges.", style="dim")
        for edge in self._snapshot.edges:
            label = self._edge_label(edge)
            node = tree.root.add(label, data=edge.waiter_thread_id)
            for evidence in edge.evidence:
                node.add_leaf(
                    Text(
                        f"{evidence.confidence.value}: {evidence.detail}",
                        style=self._confidence_style(evidence.confidence),
                    )
                )
            node.expand()
            edge_text.append_text(label)
            edge_text.append("\n")
            for evidence in edge.evidence:
                edge_text.append(
                    f"  {evidence.confidence.value}: {evidence.source} — {evidence.detail}\n",
                    style=self._confidence_style(evidence.confidence),
                )
        self.query_one("#wait-edge-list", Static).update(edge_text)

    def _render_findings(self) -> None:
        output = Text()
        if self._snapshot.warnings:
            output.append("Warnings\n", style="bold red")
            for warning in self._snapshot.warnings:
                output.append(f"  {warning}\n", style="yellow")
            output.append("\n")
        self._append_findings(
            output, "Confirmed deadlocks", self._snapshot.confirmed_deadlocks
        )
        cycles = tuple(
            finding
            for finding in self._snapshot.suspected_stalls
            if finding.kind.value == "suspected_cycle"
        )
        stalls = tuple(
            finding
            for finding in self._snapshot.suspected_stalls
            if finding.kind.value == "whole_program_stall"
        )
        self._append_findings(output, "Suspected cycles", cycles)
        self._append_findings(output, "Whole-program stalls", stalls)
        self.query_one("#findings-list", Static).update(output)

    def _append_findings(
        self,
        output: Text,
        heading: str,
        findings: tuple[Finding, ...],
    ) -> None:
        output.append(f"{heading}\n", style="bold")
        if not findings:
            output.append("  none\n", style="dim")
            return
        for finding in findings:
            output.append(f"  {finding.summary}\n")
            gaps = "; ".join(finding.evidence_gaps) or "none"
            output.append(f"    evidence gaps: {gaps}\n", style="dim")

    @staticmethod
    def _confidence_style(confidence: Confidence) -> str:
        return {
            Confidence.CONFIRMED: "bold green",
            Confidence.PROBABLE: "yellow",
            Confidence.UNKNOWN: "dim italic",
        }[confidence]

    def _edge_label(self, edge: WaitEdge) -> Text:
        owner = (
            f"thread {edge.owner_thread_id}"
            if edge.owner_thread_id is not None
            else "owner unknown"
        )
        confidence = max(
            (item.confidence for item in edge.evidence),
            key=lambda item: {
                Confidence.UNKNOWN: 0,
                Confidence.PROBABLE: 1,
                Confidence.CONFIRMED: 2,
            }[item],
            default=Confidence.UNKNOWN,
        )
        return Text(
            f"thread {edge.waiter_thread_id} --{edge.operation}--> "
            f"{edge.primitive_id} --owned by--> {owner} "
            f"[{confidence.value}]",
            style=self._confidence_style(confidence),
        )

    @staticmethod
    def _append_evidence(content: Text, evidence: tuple) -> None:
        if not evidence:
            content.append("Evidence: unknown (no adapter evidence)\n", style="dim")
            return
        content.append("Evidence:\n", style="bold")
        for item in evidence:
            content.append(
                f"  {item.confidence.value}: {item.source} — {item.detail}\n",
                style=RustConcurrencyModal._confidence_style(item.confidence),
            )

    # --- Frames-table events -----------------------------------------
    # Textual dispatches same-named handlers once per MRO class, so the
    # base class's handlers ALSO run for every event here. They ignore
    # anything that isn't the #table list; this handler ignores #table
    # and covers only the extra frames table. Do not call super() — that
    # would double-handle #table events.

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "frames-table":
            self._select_frame_at(event.cursor_row)

    def _select_frame_at(self, row: int | None) -> None:
        if (
            row is None
            or self._detail_thread_id is None
            or not (0 <= row < len(self._detail_frames))
        ):
            return
        self.post_message(
            self.SelectFrame(self._detail_thread_id, self._detail_frames[row].id)
        )
