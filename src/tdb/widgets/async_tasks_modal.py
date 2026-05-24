"""Modal screen showing active asyncio tasks with detail view."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Static, Tree
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
from tdb.widgets._inspection_modal import _InspectableListModal
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


class AsyncTasksModal(_InspectableListModal[AsyncTaskInfo]):
    """Near-full-screen modal showing all asyncio tasks.

    Adds two things on top of the shared inspection-modal base:

    - A **wait-graph view** in a third pane that toggles in/out via `g`.
      The graph surfaces deadlocks by showing each blocked task and the
      primitive it's parked on; cycle participants are decorated in red
      in both the table and the graph.

    - **Deadlock-cycle decoration** on the task table: every time the
      task list changes we recompute `find_cycles(build_wait_graph(...))`
      and use the result both for the table colorization and the header
      "⚠ N deadlock cycle(s)" suffix.
    """

    KIND_LABEL = "Async Tasks"
    TABLE_COLUMNS = ("Name", "State", "Awaiting", "Coroutine")
    FOOTER_HINT = (
        "ESC close  |  r refresh  |  g graph  |  "
        "Enter/double-click jump to task"
    )

    DEFAULT_CSS = """
    AsyncTasksModal #graph-pane {
        width: 3fr;
        overflow-y: auto;
        padding: 0 1;
        display: none;
    }
    AsyncTasksModal #wait-graph-tree {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("g", "toggle_graph", "Wait graph", show=False),
    ]

    def __init__(self, tasks: list[AsyncTaskInfo]) -> None:
        super().__init__()
        self._items: list[AsyncTaskInfo] = tasks
        # Cycle detection runs early (here in __init__ and again on every
        # `update_tasks`) so `_format_row` can read `_cycle_names` while
        # the base is still painting the table.
        self._cycles: list[list[str]] = []
        self._cycle_names: set[str] = set()
        self._recompute_cycles()

    # --- Base hooks ---------------------------------------------------

    def _compose_body(self) -> ComposeResult:
        yield from super()._compose_body()
        # Third pane (hidden by default, toggled by `g`) showing the
        # wait-graph as a tree. Sibling to #detail-pane; only one is
        # visible at a time. Both occupy the same column-fraction so
        # layout doesn't shift when toggled.
        with Vertical(id="graph-pane"):
            tree: Tree[str] = Tree("Wait Graph", id="wait-graph-tree")
            tree.show_root = False
            tree.guide_depth = 3
            yield tree

    def _format_row(self, task: AsyncTaskInfo) -> tuple:
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
            state = (
                f"{state} (×{task.cancelling})"
                if task.cancelling > 1
                else f"{state} ⊘"
            )
        state_text = Text(state, style="red") if in_cycle else Text(state)
        awaiting = task.awaiting or "—"
        return (name_text, state_text, Text(awaiting), Text(coro))

    def _render_loading_detail(self, task: AsyncTaskInfo) -> Text:
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
            content.append(
                "No stack frames (task may be awaiting)\n", style="dim",
            )
        return content

    def _select_id_for(self, task: AsyncTaskInfo) -> str:
        return task.name

    def _make_select_message(self, task_name: str) -> Message:
        return self.SelectTask(task_name)

    def _make_refresh_message(self) -> Message:
        # Tasks are refreshed via the App's existing RefreshAsyncTasks
        # message rather than a modal-local one. Importing TdbApp at
        # module load would be circular; the App's nested message class
        # is reachable via `self.app` at runtime.
        return self.app.RefreshAsyncTasks()

    def _on_after_show_detail(self, task: AsyncTaskInfo) -> None:
        self.post_message(self.LoadTaskVariables(task.name))

    def _after_populate_table(self) -> None:
        # The graph mirrors the same items list; populate it whenever
        # the table is repainted so the two views never diverge.
        self._populate_graph()

    def _header_text(self) -> str:
        base = super()._header_text()
        if self._cycles:
            return (
                f"{base}  —  ⚠ {len(self._cycles)} deadlock cycle(s) "
                "(press g)"
            )
        return base

    def _empty_state_text(self) -> str:
        return "No asyncio tasks found"

    # --- Cycle detection ----------------------------------------------

    def _recompute_cycles(self) -> None:
        """Recompute deadlock cycles from the current task list.

        Called from __init__ and update_tasks so both the table
        decoration and the graph view share one detection pass.
        """
        self._cycles = find_cycles(build_wait_graph(self._items))
        self._cycle_names = {n for c in self._cycles for n in c}

    # --- Workflow-facing surface --------------------------------------

    class LoadTaskVariables(Message):
        """Request to load variables for a task via DAP evaluate."""
        def __init__(self, task_name: str) -> None:
            self.task_name = task_name
            super().__init__()

    class SelectTask(Message):
        """User double-clicked / Enter'd a row: close the modal and
        populate the main Code/Stack views with this task's coroutine
        stack. Stepping commands still target the live paused thread."""
        def __init__(self, task_name: str) -> None:
            self.task_name = task_name
            super().__init__()

    def show_task_variables(self, variables: list[Variable]) -> None:
        """Populate the variable tree with DAP variables."""
        var_view = self.query_one("#vars", VariableView)
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
        self._items = tasks
        self._recompute_cycles()
        self._reload_after_items_change()

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
        if not self._items:
            tree.root.add_leaf(Text("(no tasks)", style="dim"))
            return
        sections = build_wait_tree(self._items)
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
        detail = self.query_one("#detail-pane", Vertical)
        graph = self.query_one("#graph-pane", Vertical)
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
        for i, t in enumerate(self._items):
            if t.name == name:
                table = self.query_one("#table", DataTable)
                table.move_cursor(row=i)
                break
