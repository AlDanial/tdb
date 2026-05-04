"""Modal screen showing active asyncio tasks with detail view."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from rich.text import Text
from textual.widgets import DataTable, Label, Static

from tdb.inspection import (
    AsyncTaskInfo,
    TASK_COLLECT_EXPR,
    TASK_LOCALS_EXPR,
    parse_task_json,
)
from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Variable

# Re-export so existing `from tdb.widgets.async_tasks_modal import …` callers
# continue to work. Definitions live in tdb.inspection (UI-free).
__all__ = [
    "AsyncTasksModal",
    "AsyncTaskInfo",
    "TASK_COLLECT_EXPR",
    "TASK_LOCALS_EXPR",
    "parse_task_json",
]

log = logging.getLogger(__name__)


class AsyncTasksModal(ModalScreen[None]):
    """Near-full-screen modal showing all asyncio tasks."""

    DEFAULT_CSS = """
    AsyncTasksModal {
        align: center middle;
    }
    AsyncTasksModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 0;
    }
    AsyncTasksModal #tasks-header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text;
        text-style: bold;
    }
    AsyncTasksModal #tasks-footer {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text-muted;
    }
    AsyncTasksModal #tasks-body {
        height: 1fr;
    }
    AsyncTasksModal #task-list-pane {
        width: 2fr;
        border-right: solid $primary;
    }
    AsyncTasksModal #task-detail-pane {
        width: 3fr;
        overflow-y: auto;
    }
    AsyncTasksModal #task-info {
        padding: 1 2;
        height: auto;
        max-height: 50%;
    }
    AsyncTasksModal #task-vars {
        height: 1fr;
        padding: 0 1;
    }
    AsyncTasksModal DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("r", "refresh_tasks", "Refresh", show=False),
    ]

    def __init__(self, tasks: list[AsyncTaskInfo]) -> None:
        super().__init__()
        self._tasks = tasks

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Async Tasks ({len(self._tasks)})", id="tasks-header")
            with Horizontal(id="tasks-body"):
                with Vertical(id="task-list-pane"):
                    table = DataTable(id="task-table")
                    table.cursor_type = "row"
                    yield table
                with Vertical(id="task-detail-pane"):
                    yield Static("", id="task-info")
                    yield VariableView(id="task-vars")
            yield Label("ESC close  |  r refresh", id="tasks-footer")

    def on_mount(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.add_columns("Name", "State", "Coroutine")
        self._populate_table()
        if self._tasks:
            self._show_detail(0)

    def _populate_table(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.clear()
        for task in self._tasks:
            coro = task.coro if len(task.coro) <= 40 else task.coro[:37] + "..."
            table.add_row(Text(task.name), Text(task.state), Text(coro))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row is not None and 0 <= event.cursor_row < len(self._tasks):
            self._show_detail(event.cursor_row)

    def _show_detail(self, index: int) -> None:
        task = self._tasks[index]
        # Task info (name, state, coro, stack) in the Static widget
        content = Text()
        content.append("Name:  ", style="bold")
        content.append(task.name + "\n")
        content.append("State: ", style="bold")
        content.append(task.state + "\n")
        content.append("Coro:  ", style="bold")
        content.append(task.coro + "\n")
        content.append("\n")
        if task.stack:
            content.append("Stack:\n", style="bold")
            for i, frame in enumerate(task.stack):
                content.append(f"  #{i} {frame}\n")
        else:
            content.append("No stack frames (task may be awaiting)\n", style="dim")
        info = self.query_one("#task-info", Static)
        info.update(content)

        # Request variable loading from the app
        self.post_message(self.LoadTaskVariables(task.name))

    class LoadTaskVariables(Message):
        """Request to load variables for a task via DAP evaluate."""
        def __init__(self, task_name: str) -> None:
            self.task_name = task_name
            super().__init__()

    def show_task_variables(self, variables: list[Variable]) -> None:
        """Populate the variable tree with DAP variables."""
        var_view = self.query_one("#task-vars", VariableView)
        var_view.clear()
        if not variables:
            var_view.root.add_leaf("(no variables — frame not available)")
            return
        for var in variables:
            label = VariableView._format_variable(var)
            if var.variables_reference > 0:
                node = var_view.root.add(label, data=var.variables_reference)
                node.add_leaf("...")
            else:
                var_view.root.add_leaf(label, data=0)

    def update_tasks(self, tasks: list[AsyncTaskInfo]) -> None:
        """Replace task list and refresh the display."""
        self._tasks = tasks
        header = self.query_one("#tasks-header", Label)
        header.update(f"Async Tasks ({len(self._tasks)})")
        self._populate_table()
        if self._tasks:
            self._show_detail(0)
        else:
            info = self.query_one("#task-info", Static)
            info.update(Text("No asyncio tasks found", style="dim"))
            var_view = self.query_one("#task-vars", VariableView)
            var_view.clear()

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_refresh_tasks(self) -> None:
        """Post message to app to re-fetch tasks and update this modal."""
        self.app.post_message(self.app.RefreshAsyncTasks())
