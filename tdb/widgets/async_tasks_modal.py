"""Modal screen showing active asyncio tasks with detail view."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from rich.markup import escape
from textual.widgets import DataTable, Label, Static

log = logging.getLogger(__name__)


@dataclass
class AsyncTaskInfo:
    """Parsed info about a single asyncio task."""
    name: str
    state: str
    coro: str
    stack: list[str]
    variables: dict[str, str] = field(default_factory=dict)


# Python snippet evaluated in the debuggee to collect asyncio task info.
# Uses eval(compile(...)) with a multi-statement body via a namespace dict.
# Handles None coroutines, missing cr_frame, and repr() failures.
TASK_COLLECT_EXPR = """\
(lambda _ns: (exec('''
import asyncio, json
_result = []
for _t in sorted(asyncio.all_tasks(), key=lambda t: t.get_name()):
    _coro = _t.get_coro()
    _frame = getattr(_coro, "cr_frame", None) if _coro is not None else None
    _vars = {}
    if _frame is not None:
        for _k, _v in _frame.f_locals.items():
            if not _k.startswith("__"):
                try:
                    _vars[_k] = repr(_v)[:200]
                except Exception:
                    _vars[_k] = "<repr failed>"
    _result.append({
        "name": _t.get_name(),
        "state": "cancelled" if _t.cancelled() else ("done" if _t.done() else "pending"),
        "coro": repr(_coro) if _coro is not None else "(finished)",
        "stack": [
            f"{_f.f_code.co_name} at {_f.f_code.co_filename}:{_f.f_lineno}"
            for _f in (_t.get_stack() or [])
        ],
        "variables": _vars,
    })
''', _ns), _ns.get('json', __import__('json')).dumps(_ns['_result']))[-1])({})
"""


def parse_task_json(raw: str) -> list[AsyncTaskInfo]:
    """Parse the JSON output from TASK_COLLECT_EXPR into AsyncTaskInfo list."""
    import ast
    import json
    try:
        text = raw.strip()
        # DAP evaluate returns Python repr of the string. Try ast.literal_eval
        # first (handles all quoting styles), fall back to raw JSON parse.
        try:
            parsed = ast.literal_eval(text)
            # literal_eval may return a str (unwrapped repr) or a list (if JSON
            # happened to be valid Python syntax too)
            if isinstance(parsed, str):
                data = json.loads(parsed)
            elif isinstance(parsed, list):
                data = parsed
            else:
                data = json.loads(text)
        except (ValueError, SyntaxError):
            data = json.loads(text)
        return [
            AsyncTaskInfo(
                name=d["name"],
                state=d["state"],
                coro=d["coro"],
                stack=d.get("stack", []),
                variables=d.get("variables", {}),
            )
            for d in data
        ]
    except Exception:
        log.exception("Failed to parse async task info: %s", raw[:200])
        return []


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
        padding: 1 2;
        overflow-y: auto;
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
                    yield Static("", id="task-detail")
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
            table.add_row(escape(task.name), escape(task.state), escape(coro))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row is not None and 0 <= event.cursor_row < len(self._tasks):
            self._show_detail(event.cursor_row)

    def _show_detail(self, index: int) -> None:
        task = self._tasks[index]
        lines = []
        lines.append(f"[bold]Name:[/bold]  {escape(task.name)}")
        lines.append(f"[bold]State:[/bold] {escape(task.state)}")
        lines.append(f"[bold]Coro:[/bold]  {escape(task.coro)}")
        lines.append("")
        if task.stack:
            lines.append("[bold]Stack:[/bold]")
            for i, frame in enumerate(task.stack):
                lines.append(f"  #{i} {escape(frame)}")
        else:
            lines.append("[dim]No stack frames (task may be awaiting)[/dim]")
        lines.append("")
        if task.variables:
            lines.append("[bold]Variables:[/bold]")
            for name, value in sorted(task.variables.items()):
                lines.append(f"  [cyan]{escape(name)}[/cyan] = {escape(value)}")
        else:
            lines.append("[dim]No variables (frame not available)[/dim]")
        detail = self.query_one("#task-detail", Static)
        detail.update("\n".join(lines))

    def update_tasks(self, tasks: list[AsyncTaskInfo]) -> None:
        """Replace task list and refresh the display."""
        self._tasks = tasks
        header = self.query_one("#tasks-header", Label)
        header.update(f"Async Tasks ({len(self._tasks)})")
        self._populate_table()
        if self._tasks:
            self._show_detail(0)
        else:
            detail = self.query_one("#task-detail", Static)
            detail.update("[dim]No asyncio tasks found[/dim]")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_refresh_tasks(self) -> None:
        """Post message to app to re-fetch tasks and update this modal."""
        self.app.post_message(self.app.RefreshAsyncTasks())
