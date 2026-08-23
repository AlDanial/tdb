"""Rust-specific concurrency inspection workspace.

The generic Threads modal remains the normal DAP thread browser.  Rust's
concurrency collector has richer, immutable wait-graph evidence, so this
screen renders that one snapshot coherently across its three tabs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static, TabbedContent, TabPane, Tree

from rich.text import Text

from tdb.rust_concurrency.models import (
    Confidence,
    ConcurrencySnapshot,
    Finding,
    ThreadAnalysis,
    WaitEdge,
)
from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Scope, StackFrame, Variable


class RustConcurrencyModal(ModalScreen[None]):
    """Near-full-screen view of one immutable Rust concurrency snapshot."""

    DEFAULT_CSS = """
    RustConcurrencyModal {
        align: center middle;
    }
    RustConcurrencyModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $primary;
        background: $surface;
    }
    RustConcurrencyModal #header, RustConcurrencyModal #footer {
        height: 1;
        padding: 0 1;
        background: $primary-background;
    }
    RustConcurrencyModal #header { text-style: bold; }
    RustConcurrencyModal #footer { color: $text-muted; }
    RustConcurrencyModal TabbedContent { height: 1fr; }
    RustConcurrencyModal #threads-body { height: 1fr; }
    RustConcurrencyModal #threads-table { width: 2fr; height: 1fr; }
    RustConcurrencyModal #thread-detail { width: 3fr; overflow-y: auto; }
    RustConcurrencyModal #thread-evidence {
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
        padding: 0 1;
    }
    RustConcurrencyModal #wait-graph-tree { height: 1fr; width: 1fr; }
    RustConcurrencyModal #wait-edge-list, RustConcurrencyModal #findings-list {
        padding: 1 2;
        overflow-y: auto;
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("r", "request_refresh", "Refresh", show=False),
        Binding("enter", "select_current", "Select", show=False),
    ]

    def __init__(
        self,
        snapshot: ConcurrencySnapshot,
        current_thread_id: int | None,
    ) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._current_thread_id = current_thread_id
        self._thread_ids: list[int] = []
        self._detail_thread_id: int | None = None
        self._detail_frames: list[StackFrame] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Rust Concurrency", id="header")
            with TabbedContent(initial="threads-tab"):
                with TabPane("Threads", id="threads-tab"):
                    with Horizontal(id="threads-body"):
                        yield DataTable(id="threads-table")
                        with Vertical(id="thread-detail"):
                            yield Static(id="thread-evidence")
                            yield DataTable(id="frames-table")
                            yield VariableView(id="vars")
                with TabPane("Wait Graph", id="wait-graph-tab"):
                    yield Tree("Wait graph", id="wait-graph-tree")
                    yield Static(id="wait-edge-list")
                with TabPane("Findings", id="findings-tab"):
                    yield Static(id="findings-list")
            yield Label(
                "ESC close  |  r refresh  |  Enter select  |  arrows/tab navigate",
                id="footer",
            )

    def on_mount(self) -> None:
        table = self.query_one("#threads-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Name", "State", "Wait")
        frames = self.query_one("#frames-table", DataTable)
        frames.cursor_type = "row"
        frames.add_columns("#", "Function", "Location")
        tree = self.query_one("#wait-graph-tree", Tree)
        tree.show_root = False
        tree.guide_depth = 3
        self._render_snapshot()

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
        self._render_snapshot()

    # --- Snapshot rendering ------------------------------------------

    def _render_snapshot(self) -> None:
        warning_suffix = (
            f" — {len(self._snapshot.warnings)} warning(s)"
            if self._snapshot.warnings
            else ""
        )
        self.query_one("#header", Label).update(
            f"Rust Concurrency ({len(self._snapshot.threads)} threads){warning_suffix}"
        )
        self._render_threads()
        self._render_wait_graph()
        self._render_findings()

    def _render_threads(self) -> None:
        table = self.query_one("#threads-table", DataTable)
        table.clear()
        self._thread_ids = []
        for thread in self._snapshot.threads:
            thread_id = Text(str(thread.thread_id))
            name = Text(thread.name)
            if thread.thread_id == self._current_thread_id:
                thread_id.stylize("bold")
                name.stylize("bold")
            wait = thread.wait.operation if thread.wait is not None else "—"
            table.add_row(thread_id, name, thread.state.value, wait)
            self._thread_ids.append(thread.thread_id)
        if self._snapshot.threads:
            index = next(
                (
                    i
                    for i, thread in enumerate(self._snapshot.threads)
                    if thread.thread_id == self._current_thread_id
                ),
                0,
            )
            table.move_cursor(row=index)
            self._show_thread_summary(self._snapshot.threads[index])
        else:
            self.query_one("#thread-evidence", Static).update("No Rust threads found")

    def _show_thread_summary(self, thread: ThreadAnalysis) -> None:
        """Show immutable analysis immediately and request its live DAP detail."""
        self._detail_thread_id = thread.thread_id
        self._detail_frames = []
        content = Text()
        content.append("Thread ID: ", style="bold")
        content.append(f"{thread.thread_id}\n")
        content.append("Name:      ", style="bold")
        content.append(f"{thread.name}\n")
        content.append("State:     ", style="bold")
        content.append(f"{thread.state.value}\n")
        if thread.wait is None:
            content.append("\nNo observed wait relationship.\n", style="dim")
        else:
            content.append("\nWaiting on: ", style="bold")
            content.append(f"{thread.wait.primitive_id} ({thread.wait.operation})\n")
            if thread.wait.owner_thread_id is not None:
                content.append(
                    f"Observed owner: thread {thread.wait.owner_thread_id}\n"
                )
            self._append_evidence(content, thread.wait.evidence)
        self.query_one("#thread-evidence", Static).update(content)
        self.query_one("#frames-table", DataTable).clear()
        variables = self.query_one("#vars", VariableView)
        variables.clear()
        variables.root.add_leaf("Loading live frame locals…")
        self.post_message(self.LoadThreadDetail(thread.thread_id))

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

    # --- User interaction --------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "threads-table":
            return
        row = event.cursor_row
        if row is not None and 0 <= row < len(self._snapshot.threads):
            self._show_thread_summary(self._snapshot.threads[row])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "frames-table":
            self._select_frame_at(event.cursor_row)
        elif event.data_table.id == "threads-table":
            self._select_thread_at(event.cursor_row)

    def on_tree_node_selected(self, event: Tree.NodeSelected[int]) -> None:
        if isinstance(event.node.data, int):
            self.post_message(self.SelectThread(event.node.data))

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_request_refresh(self) -> None:
        self.post_message(self.RefreshSnapshot())

    def action_select_current(self) -> None:
        frames = self.query_one("#frames-table", DataTable)
        if self.focused is frames:
            self._select_frame_at(frames.cursor_row)
            return
        table = self.query_one("#threads-table", DataTable)
        self._select_thread_at(table.cursor_row)

    def _select_thread_at(self, row: int | None) -> None:
        if row is None or not (0 <= row < len(self._thread_ids)):
            return
        self.post_message(self.SelectThread(self._thread_ids[row]))

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
