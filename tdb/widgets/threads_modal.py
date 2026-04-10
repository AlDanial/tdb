"""Modal screen showing active threads with stack and variable detail."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from rich.text import Text
from textual.widgets import DataTable, Label, Static

from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Scope, StackFrame, Thread, Variable

log = logging.getLogger(__name__)


class ThreadsModal(ModalScreen[None]):
    """Near-full-screen modal showing all threads with stack and variables."""

    DEFAULT_CSS = """
    ThreadsModal {
        align: center middle;
    }
    ThreadsModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 0;
    }
    ThreadsModal #threads-header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text;
        text-style: bold;
    }
    ThreadsModal #threads-footer {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text-muted;
    }
    ThreadsModal #threads-body {
        height: 1fr;
    }
    ThreadsModal #thread-list-pane {
        width: 2fr;
        border-right: solid $primary;
    }
    ThreadsModal #thread-detail-pane {
        width: 3fr;
        overflow-y: auto;
    }
    ThreadsModal #thread-info {
        padding: 1 2;
        height: auto;
        max-height: 50%;
    }
    ThreadsModal #thread-vars {
        height: 1fr;
        padding: 0 1;
    }
    ThreadsModal DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("r", "refresh_threads", "Refresh", show=False),
    ]

    def __init__(
        self,
        threads: list[Thread],
        current_thread_id: int | None = None,
    ) -> None:
        super().__init__()
        self._threads = threads
        self._current_thread_id = current_thread_id

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"Threads ({len(self._threads)})", id="threads-header",
            )
            with Horizontal(id="threads-body"):
                with Vertical(id="thread-list-pane"):
                    table = DataTable(id="thread-table")
                    table.cursor_type = "row"
                    yield table
                with Vertical(id="thread-detail-pane"):
                    yield Static("", id="thread-info")
                    yield VariableView(id="thread-vars")
            yield Label("ESC close  |  r refresh", id="threads-footer")

    def on_mount(self) -> None:
        table = self.query_one("#thread-table", DataTable)
        table.add_columns("ID", "Name")
        self._populate_table()
        if self._threads:
            # Select the current thread if present, otherwise first
            idx = 0
            if self._current_thread_id is not None:
                for i, t in enumerate(self._threads):
                    if t.id == self._current_thread_id:
                        idx = i
                        break
            table.move_cursor(row=idx)
            self._show_detail(idx)

    def _populate_table(self) -> None:
        table = self.query_one("#thread-table", DataTable)
        table.clear()
        for thread in self._threads:
            tid = Text(str(thread.id))
            name = Text(thread.name)
            if thread.id == self._current_thread_id:
                tid.stylize("bold")
                name.stylize("bold")
            table.add_row(tid, name)

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
    ) -> None:
        if (
            event.cursor_row is not None
            and 0 <= event.cursor_row < len(self._threads)
        ):
            self._show_detail(event.cursor_row)

    def _show_detail(self, index: int) -> None:
        thread = self._threads[index]
        # Show basic info immediately; stack + vars arrive via message
        content = Text()
        content.append("Thread ID: ", style="bold")
        content.append(str(thread.id) + "\n")
        content.append("Name:      ", style="bold")
        content.append(thread.name + "\n")
        if thread.id == self._current_thread_id:
            content.append("(current thread)\n", style="dim italic")
        content.append("\n")
        content.append("Loading stack...\n", style="dim")
        info = self.query_one("#thread-info", Static)
        info.update(content)

        # Clear vars while loading
        var_view = self.query_one("#thread-vars", VariableView)
        var_view.clear()

        # Request stack + variables from the app
        self.post_message(self.LoadThreadDetail(thread.id))

    class LoadThreadDetail(Message):
        """Request to fetch stack trace and variables for a thread."""
        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class RefreshThreads(Message):
        """Request to refresh the thread list."""

    def show_thread_detail(
        self,
        thread_id: int,
        frames: list[StackFrame],
        scopes: list[Scope],
        variables: dict[int, list[Variable]],
    ) -> None:
        """Populate the detail pane with stack trace and variables."""
        # Find the thread
        thread = next((t for t in self._threads if t.id == thread_id), None)
        if thread is None:
            return

        content = Text()
        content.append("Thread ID: ", style="bold")
        content.append(str(thread.id) + "\n")
        content.append("Name:      ", style="bold")
        content.append(thread.name + "\n")
        if thread.id == self._current_thread_id:
            content.append("(current thread)\n", style="dim italic")
        content.append("\n")

        if frames:
            content.append("Stack:\n", style="bold")
            for i, frame in enumerate(frames):
                loc = ""
                if frame.source and frame.source.path:
                    loc = f" at {Path(frame.source.path).name}:{frame.line}"
                elif frame.source and frame.source.name:
                    loc = f" at {frame.source.name}:{frame.line}"
                content.append(f"  #{i} {frame.name}{loc}\n")
        else:
            content.append("No stack frames available\n", style="dim")

        info = self.query_one("#thread-info", Static)
        info.update(content)

        # Populate variable tree
        var_view = self.query_one("#thread-vars", VariableView)
        if scopes:
            var_view.update_variables(scopes, variables)
        else:
            var_view.clear()
            var_view.root.add_leaf("(no variables available)")

    def update_threads(
        self,
        threads: list[Thread],
        current_thread_id: int | None = None,
    ) -> None:
        """Replace thread list and refresh the display."""
        self._threads = threads
        self._current_thread_id = current_thread_id
        header = self.query_one("#threads-header", Label)
        header.update(f"Threads ({len(self._threads)})")
        self._populate_table()
        if self._threads:
            self._show_detail(0)
        else:
            info = self.query_one("#thread-info", Static)
            info.update(Text("No threads found", style="dim"))
            var_view = self.query_one("#thread-vars", VariableView)
            var_view.clear()

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_refresh_threads(self) -> None:
        """Post message to app to re-fetch threads."""
        self.post_message(self.RefreshThreads())
