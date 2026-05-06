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
from textual.widgets import DataTable, Label, Static, Tree
from textual.widgets._tree import TreeNode

from tdb.inspection import (
    AsyncTaskInfo,
    TASK_COLLECT_EXPR,
    TASK_LOCALS_EXPR,
    WaitTreeNode,
    build_wait_graph,
    build_wait_tree,
    find_cycles,
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
    AsyncTasksModal #task-graph-pane {
        width: 3fr;
        overflow-y: auto;
        padding: 0 1;
        display: none;
    }
    AsyncTasksModal #wait-graph-tree {
        height: 1fr;
    }
    AsyncTasksModal DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("r", "refresh_tasks", "Refresh", show=False),
        Binding("g", "toggle_graph", "Wait graph", show=False),
    ]

    def __init__(self, tasks: list[AsyncTaskInfo]) -> None:
        super().__init__()
        self._tasks = tasks
        self._cycles: list[list[str]] = []
        self._cycle_names: set[str] = set()

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
                with Vertical(id="task-graph-pane"):
                    tree: Tree[str] = Tree("Wait Graph", id="wait-graph-tree")
                    tree.show_root = False
                    tree.guide_depth = 3
                    yield tree
            yield Label("ESC close  |  r refresh  |  g graph", id="tasks-footer")

    def on_mount(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.add_columns("Name", "State", "Awaiting", "Coroutine")
        self._recompute_cycles()
        self._update_header()
        self._populate_table()
        self._populate_graph()
        if self._tasks:
            self._show_detail(0)

    def _recompute_cycles(self) -> None:
        """Recompute deadlock cycles from current task list.

        Called from update_tasks/on_mount so both the table decoration
        and the graph view share one detection pass."""
        self._cycles = find_cycles(build_wait_graph(self._tasks))
        self._cycle_names = {n for c in self._cycles for n in c}

    def _update_header(self) -> None:
        header = self.query_one("#tasks-header", Label)
        suffix = ""
        if self._cycles:
            suffix = f"  —  ⚠ {len(self._cycles)} deadlock cycle(s) (press g)"
        header.update(f"Async Tasks ({len(self._tasks)}){suffix}")

    def _populate_table(self) -> None:
        table = self.query_one("#task-table", DataTable)
        table.clear()
        for task in self._tasks:
            coro = task.coro if len(task.coro) <= 40 else task.coro[:37] + "..."
            in_cycle = task.name in self._cycle_names
            # Name in red when this task participates in a deadlock —
            # surfaces the cycle without forcing the user to open the
            # graph view first.
            name_text = (
                Text(task.name, style="bold red") if in_cycle else Text(task.name)
            )
            # Decorate state with a cancel marker so a task that's been
            # asked to cancel but hasn't observed the request yet stands
            # out — that's exactly the kind of state you open this modal
            # to find. Cycle marker takes precedence visually.
            state = task.state
            if in_cycle:
                state = f"{state}  ↻ deadlock"
            elif task.cancelling:
                state = f"{state} (×{task.cancelling})" if task.cancelling > 1 else f"{state} ⊘"
            state_text = Text(state, style="red") if in_cycle else Text(state)
            awaiting = task.awaiting or "—"
            table.add_row(name_text, state_text, Text(awaiting), Text(coro))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row is not None and 0 <= event.cursor_row < len(self._tasks):
            self._show_detail(event.cursor_row)

    def _show_detail(self, index: int) -> None:
        task = self._tasks[index]
        # Task info (name, state, coro, stack) in the Static widget
        content = Text()
        content.append("Name:     ", style="bold")
        content.append(task.name + "\n")
        content.append("State:    ", style="bold")
        content.append(task.state + "\n")
        if task.awaiting:
            content.append("Awaiting: ", style="bold")
            content.append(task.awaiting + "\n")
        if task.cancelling:
            content.append("Cancelling: ", style="bold")
            content.append(f"{task.cancelling} pending request(s)\n")
        if task.cancel_message:
            content.append("Cancel msg: ", style="bold")
            content.append(task.cancel_message + "\n")
        content.append("Coro:     ", style="bold")
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
        self._recompute_cycles()
        self._update_header()
        self._populate_table()
        self._populate_graph()
        if self._tasks:
            self._show_detail(0)
        else:
            info = self.query_one("#task-info", Static)
            info.update(Text("No asyncio tasks found", style="dim"))
            var_view = self.query_one("#task-vars", VariableView)
            var_view.clear()

    # --- Wait-graph view ----------------------------------------------

    _KIND_STYLES = {
        "section_cycles": "bold red",
        "section_blocked": "bold",
        "section_running": "dim",
        "cycle": "red",
        "task": "",
        "task_unblocked": "dim",
        "primitive": "cyan",
        "no_holder": "dim italic",
        "orphan": "dim italic",
        "cycle_ref": "red",
    }

    def _populate_graph(self) -> None:
        tree = self.query_one("#wait-graph-tree", Tree)
        tree.clear()
        if not self._tasks:
            tree.root.add_leaf(Text("(no tasks)", style="dim"))
            return
        sections = build_wait_tree(self._tasks)
        for section in sections:
            self._render_node(tree.root, section, expand=True)

    def _render_node(
        self,
        parent: TreeNode[str],
        node: WaitTreeNode,
        *,
        expand: bool = False,
    ) -> None:
        style = self._KIND_STYLES.get(node.kind, "")
        label = Text(node.label, style=style) if style else Text(node.label)
        # Tree.data is typed str; use empty string for non-task nodes so
        # the selection handler can distinguish them with a falsiness check.
        data = node.data or ""
        if node.children:
            tnode = parent.add(label, data=data)
            for child in node.children:
                # Auto-expand the spine of blocked tasks so the user
                # immediately sees the wait chain without clicking.
                self._render_node(tnode, child, expand=node.kind in (
                    "section_cycles", "section_blocked", "task", "primitive",
                ))
            if expand:
                tnode.expand()
        else:
            parent.add_leaf(label, data=data)

    def action_toggle_graph(self) -> None:
        detail = self.query_one("#task-detail-pane", Vertical)
        graph = self.query_one("#task-graph-pane", Vertical)
        if graph.display:
            graph.display = False
            detail.display = True
        else:
            detail.display = False
            graph.display = True
            self.query_one("#wait-graph-tree", Tree).focus()

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        """Selecting a task node in the wait-graph tree highlights the
        corresponding row in the task table."""
        # Only nodes whose data is a non-empty string correspond to tasks.
        name = event.node.data
        if not isinstance(name, str) or not name:
            return
        for i, t in enumerate(self._tasks):
            if t.name == name:
                table = self.query_one("#task-table", DataTable)
                table.move_cursor(row=i)
                break

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_refresh_tasks(self) -> None:
        """Post message to app to re-fetch tasks and update this modal."""
        self.app.post_message(self.app.RefreshAsyncTasks())
