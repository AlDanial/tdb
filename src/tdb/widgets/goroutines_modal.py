"""Go goroutine inspection workspace.

Same shape as RustConcurrencyModal: an _InspectableListModal wrapped in
a TabbedContent — goroutine list + live detail, wait-graph tree,
findings. The snapshot is immutable (tdb.go_concurrency.models);
live stack/locals for the highlighted goroutine arrive via
LoadGoroutineDetail exactly like the Rust workspace's thread detail
(goroutines ARE DAP threads under Delve).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Label, Static, TabbedContent, TabPane, Tree

from rich.text import Text

from tdb.go_concurrency.models import (
    Confidence,
    GoroutineInfo,
    GoroutineSnapshot,
)
from tdb.widgets._inspection_modal import _InspectableListModal
from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Scope, StackFrame, Variable


class GoroutinesModal(_InspectableListModal[GoroutineInfo]):
    """Near-full-screen view of one immutable goroutine snapshot."""

    KIND_LABEL = "Goroutines"
    TABLE_COLUMNS = ("ID", "State", "Function", "Waiting on")
    FOOTER_HINT = (
        "ESC close  |  r refresh  |  a show runtime  |  Enter select  |  tab tabs"
    )

    BINDINGS = _InspectableListModal.BINDINGS + [
        Binding("a", "toggle_runtime", "Show all", show=False),
    ]

    DEFAULT_CSS = """
    GoroutinesModal TabbedContent { height: 1fr; }
    GoroutinesModal #info { height: 5; max-height: 5; padding: 0 2; overflow-y: auto; }
    GoroutinesModal #frames-table { height: 1fr; min-height: 3; }
    GoroutinesModal #vars { height: 1fr; min-height: 3; }
    GoroutinesModal #wait-graph-tree { height: 1fr; width: 1fr; }
    GoroutinesModal #findings-list { padding: 1 2; overflow-y: auto; height: 1fr; }
    """

    def __init__(
        self, snapshot: GoroutineSnapshot, current_thread_id: int | None
    ) -> None:
        super().__init__()
        self._snapshot = snapshot
        self._current_thread_id = current_thread_id
        self._show_runtime = False
        self._items: list[GoroutineInfo] = self.visible_items()
        self._detail_thread_id: int | None = None
        self._detail_frames: list[StackFrame] = []

    # --- Snapshot-derived helpers ------------------------------------

    def visible_items(self) -> list[GoroutineInfo]:
        return [
            g
            for g in self._snapshot.goroutines
            if self._show_runtime or not g.is_runtime
        ]

    def _in_finding(self, thread_id: int) -> bool:
        return any(thread_id in f.thread_ids for f in self._snapshot.findings)

    # --- Messages ------------------------------------------------------

    class RefreshSnapshot(Message):
        """Request one fresh goroutine snapshot."""

    class LoadGoroutineDetail(Message):
        """Request live DAP stack/scopes/variables for a highlighted goroutine."""

        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class SelectGoroutine(Message):
        """Navigate the main stack/source/locals views to a goroutine."""

        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class SelectFrame(Message):
        """Navigate the main stack/source/locals views to a frame."""

        def __init__(self, thread_id: int, frame_id: int) -> None:
            self.thread_id = thread_id
            self.frame_id = frame_id
            super().__init__()

    # --- Compose / mount ---------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._header_text(), id="header")
            with TabbedContent(initial="goroutines-tab"):
                with TabPane("Goroutines", id="goroutines-tab"):
                    with Horizontal(id="body"):
                        yield from self._compose_body()
                with TabPane("Wait Graph", id="wait-graph-tab"):
                    yield Tree("Wait graph", id="wait-graph-tree")
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

    def update_snapshot(self, snapshot: GoroutineSnapshot) -> None:
        """Atomically replace the model rendered by all three workspace tabs."""
        self._snapshot = snapshot
        self._repopulate()

    def action_toggle_runtime(self) -> None:
        self._show_runtime = not self._show_runtime
        self._repopulate()

    def _repopulate(self) -> None:
        self._items = self.visible_items()
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
        extra = ""
        if self._snapshot.uncollected:
            extra = f" — {self._snapshot.uncollected} more not collected"
        if self._snapshot.warnings:
            extra += f" — {len(self._snapshot.warnings)} warning(s)"
        return f"{self.KIND_LABEL} ({len(self.visible_items())}){extra}"

    def _empty_state_text(self) -> str:
        return "No goroutines found"

    def _format_row(self, item: GoroutineInfo) -> tuple:
        gid = Text(f"Go {item.goid}" if item.goid is not None else str(item.thread_id))
        state = Text(item.state.value)
        func = Text(item.function)
        wait = Text(item.resource_id or "—")
        if item.thread_id == self._current_thread_id:
            gid.stylize("bold")
            func.stylize("bold")
        if self._in_finding(item.thread_id):
            for cell in (gid, state, func, wait):
                cell.stylize("red")
        return (gid, state, func, wait)

    def _initial_cursor_index(self) -> int:
        return next(
            (
                i
                for i, item in enumerate(self._items)
                if item.thread_id == self._current_thread_id
            ),
            0,
        )

    def _render_loading_detail(self, item: GoroutineInfo) -> Text:
        content = Text()
        content.append("Thread ID: ", style="bold")
        content.append(f"{item.thread_id}\n")
        if item.goid is not None:
            content.append("Goroutine: ", style="bold")
            content.append(f"Go {item.goid}\n")
        content.append("Function:  ", style="bold")
        content.append(f"{item.function}\n")
        content.append("State:     ", style="bold")
        content.append(f"{item.state.value}\n")
        if item.resource_id is not None:
            content.append("\nWaiting on: ", style="bold")
            content.append(
                f"{item.resource_id}"
                + (f" ({item.operation})" if item.operation else "")
                + "\n"
            )
        else:
            content.append("\nNo observed wait relationship.\n", style="dim")
        findings = [
            f for f in self._snapshot.findings if item.thread_id in f.thread_ids
        ]
        if findings:
            content.append("\nFindings:\n", style="bold")
            for finding in findings:
                content.append(f"  {finding.summary}\n", style="red")
        return content

    def _on_after_show_detail(self, item: GoroutineInfo) -> None:
        """Request the highlighted goroutine's live DAP stack + locals."""
        self._detail_thread_id = item.thread_id
        self._detail_frames = []
        self.query_one("#frames-table", DataTable).clear()
        variables = self.query_one("#vars", VariableView)
        variables.clear()
        variables.root.add_leaf("Loading live frame locals…")
        self.post_message(self.LoadGoroutineDetail(item.thread_id))

    def _select_id_for(self, item: GoroutineInfo) -> int:
        return item.thread_id

    def _make_select_message(self, item_id: int) -> Message:
        return self.SelectGoroutine(item_id)

    def _make_refresh_message(self) -> Message:
        return self.RefreshSnapshot()

    # --- Live goroutine detail -----------------------------------------

    def show_thread_detail(
        self,
        thread_id: int,
        frames: list[StackFrame],
        scopes: list[Scope],
        variables: dict[int, list[Variable]],
    ) -> None:
        """Render the selected goroutine's live stack frames and locals."""
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
        by_resource: dict[str, list] = {}
        for edge in self._snapshot.edges:
            by_resource.setdefault(edge.resource_id, []).append(edge)
        if not by_resource:
            tree.root.add_leaf(Text("No observed waits", style="dim"))
            return
        labels = {r.resource_id: r.label for r in self._snapshot.resources}
        funcs = {g.thread_id: g.function for g in self._snapshot.goroutines}
        goids = {g.thread_id: g.goid for g in self._snapshot.goroutines}
        for rid, edges in sorted(by_resource.items()):
            node = tree.root.add(Text(labels.get(rid, rid), style="bold"))
            for e in edges:
                goid = goids.get(e.thread_id)
                who = f"Go {goid}" if goid is not None else f"thread {e.thread_id}"
                node.add_leaf(
                    Text(f"{who} — {e.operation} — {funcs.get(e.thread_id, '?')}")
                )
            node.expand()

    def _render_findings(self) -> None:
        output = Text()
        if self._snapshot.warnings:
            output.append("Warnings\n", style="bold red")
            for warning in self._snapshot.warnings:
                output.append(f"  {warning}\n", style="yellow")
            output.append("\n")
        output.append("Findings\n", style="bold")
        if not self._snapshot.findings:
            output.append("  none\n", style="dim")
        else:
            for finding in self._snapshot.findings:
                style = self._confidence_style(finding.confidence)
                output.append(f"  {finding.summary}\n", style=style)
                who = ", ".join(f"Go {tid}" for tid in finding.thread_ids)
                output.append(f"    goroutines: {who}\n", style="dim")
        self.query_one("#findings-list", Static).update(output)

    @staticmethod
    def _confidence_style(confidence: Confidence) -> str:
        return {
            Confidence.CONFIRMED: "bold red",
            Confidence.PROBABLE: "yellow",
        }[confidence]

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
