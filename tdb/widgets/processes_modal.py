"""Modal screen showing active child processes with detail view."""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    from tdb.dap.types import Scope, StackFrame, Variable

log = logging.getLogger(__name__)


@dataclass
class ProcessInfo:
    """Parsed info about a single child process."""
    name: str
    pid: int | None
    alive: bool
    exitcode: int | None
    daemon: bool
    target: str
    args: str
    kwargs: str
    start_method: str


# Python snippet evaluated in the debuggee to collect child process info.
PROCESS_COLLECT_EXPR = """\
(lambda _ns: (exec('''
import json, multiprocessing
_result = []
for _p in multiprocessing.active_children():
    try:
        _tgt = repr(getattr(_p, "_target", None))[:200]
    except Exception:
        _tgt = "<unknown>"
    try:
        _args = repr(getattr(_p, "_args", ()))[:200]
    except Exception:
        _args = "()"
    try:
        _kwargs = repr(getattr(_p, "_kwargs", {}))[:200]
    except Exception:
        _kwargs = "{}"
    _result.append({
        "name": _p.name,
        "pid": _p.pid,
        "alive": _p.is_alive(),
        "exitcode": _p.exitcode,
        "daemon": _p.daemon,
        "target": _tgt,
        "args": _args,
        "kwargs": _kwargs,
        "start_method": getattr(_p, "_start_method", "") or "",
    })
_result.sort(key=lambda x: x["name"])
''', _ns), _ns.get('json', __import__('json')).dumps(_ns['_result']))[-1])({})
"""


def parse_process_json(raw: str) -> list[ProcessInfo]:
    """Parse the JSON output from PROCESS_COLLECT_EXPR into ProcessInfo list."""
    import ast
    import json
    try:
        text = raw.strip()
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, str):
                data = json.loads(parsed)
            elif isinstance(parsed, list):
                data = parsed
            else:
                data = json.loads(text)
        except (ValueError, SyntaxError):
            data = json.loads(text)
        return [
            ProcessInfo(
                name=d["name"],
                pid=d.get("pid"),
                alive=d.get("alive", False),
                exitcode=d.get("exitcode"),
                daemon=d.get("daemon", False),
                target=d.get("target", "None"),
                args=d.get("args", "()"),
                kwargs=d.get("kwargs", "{}"),
                start_method=d.get("start_method", ""),
            )
            for d in data
        ]
    except Exception:
        log.exception("Failed to parse process info: %s", raw[:200])
        return []


class ProcessesModal(ModalScreen[None]):
    """Near-full-screen modal showing child processes with stack and variables."""

    DEFAULT_CSS = """
    ProcessesModal {
        align: center middle;
    }
    ProcessesModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 0;
    }
    ProcessesModal #procs-header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text;
        text-style: bold;
    }
    ProcessesModal #procs-footer {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text-muted;
    }
    ProcessesModal #procs-body {
        height: 1fr;
    }
    ProcessesModal #proc-list-pane {
        width: 2fr;
        border-right: solid $primary;
    }
    ProcessesModal #proc-detail-pane {
        width: 3fr;
        overflow-y: auto;
    }
    ProcessesModal #proc-info {
        padding: 1 2;
        height: auto;
        max-height: 50%;
    }
    ProcessesModal #proc-vars {
        height: 1fr;
        padding: 0 1;
    }
    ProcessesModal DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("r", "refresh_processes", "Refresh", show=False),
    ]

    def __init__(self, processes: list[ProcessInfo]) -> None:
        super().__init__()
        self._processes = processes

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                f"Processes ({len(self._processes)})", id="procs-header",
            )
            with Horizontal(id="procs-body"):
                with Vertical(id="proc-list-pane"):
                    table = DataTable(id="proc-table")
                    table.cursor_type = "row"
                    yield table
                with Vertical(id="proc-detail-pane"):
                    yield Static("", id="proc-info")
                    yield VariableView(id="proc-vars")
            yield Label("ESC close  |  r refresh", id="procs-footer")

    def on_mount(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        table.add_columns("PID", "Name", "Status")
        if self._processes:
            self._populate_table()
            self._show_detail(0)
        else:
            info = self.query_one("#proc-info", Static)
            info.update(Text("Loading...", style="dim italic"))

    def _populate_table(self) -> None:
        table = self.query_one("#proc-table", DataTable)
        table.clear()
        for proc in self._processes:
            pid = Text(str(proc.pid) if proc.pid is not None else "—")
            name = Text(proc.name)
            if proc.alive:
                status = Text("alive", style="green")
            elif proc.exitcode is not None and proc.exitcode != 0:
                status = Text(f"exited ({proc.exitcode})", style="red")
            elif proc.exitcode == 0:
                status = Text("exited (0)")
            else:
                status = Text("unknown", style="dim")
            table.add_row(pid, name, status)

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
    ) -> None:
        if (
            event.cursor_row is not None
            and 0 <= event.cursor_row < len(self._processes)
        ):
            self._show_detail(event.cursor_row)

    def _show_detail(self, index: int) -> None:
        proc = self._processes[index]
        # Show basic info immediately; stack + vars arrive via message
        content = Text()
        content.append("Name:    ", style="bold")
        content.append(proc.name + "\n")
        content.append("PID:     ", style="bold")
        content.append(
            (str(proc.pid) if proc.pid is not None else "not started") + "\n",
        )
        content.append("Status:  ", style="bold")
        if proc.alive:
            content.append("alive\n", style="green")
        elif proc.exitcode is not None:
            style = "" if proc.exitcode == 0 else "red"
            content.append(f"exited (code {proc.exitcode})\n", style=style)
        else:
            content.append("unknown\n", style="dim")
        content.append("Daemon:  ", style="bold")
        content.append(str(proc.daemon) + "\n")
        if proc.start_method:
            content.append("Method:  ", style="bold")
            content.append(proc.start_method + "\n")
        content.append("\n")
        if proc.alive:
            content.append("Loading stack...\n", style="dim")
        info = self.query_one("#proc-info", Static)
        info.update(content)

        # Clear vars while loading
        var_view = self.query_one("#proc-vars", VariableView)
        var_view.clear()

        # Request stack + variables from the app
        if proc.pid is not None and proc.alive:
            self.post_message(self.LoadProcessDetail(proc.pid))

    class LoadProcessDetail(Message):
        """Request to fetch stack trace and variables for a child process."""
        def __init__(self, pid: int) -> None:
            self.pid = pid
            super().__init__()

    class RefreshProcesses(Message):
        """Request to refresh the process list."""

    def show_process_detail(
        self,
        pid: int,
        frames: list[StackFrame],
        scopes: list[Scope],
        variables: dict[int, list[Variable]],
    ) -> None:
        """Populate the detail pane with stack trace and variables."""
        proc = next((p for p in self._processes if p.pid == pid), None)
        if proc is None:
            return

        content = Text()
        content.append("Name:    ", style="bold")
        content.append(proc.name + "\n")
        content.append("PID:     ", style="bold")
        content.append(str(proc.pid) + "\n")
        content.append("Status:  ", style="bold")
        if proc.alive:
            content.append("alive\n", style="green")
        elif proc.exitcode is not None:
            style = "" if proc.exitcode == 0 else "red"
            content.append(f"exited (code {proc.exitcode})\n", style=style)
        else:
            content.append("unknown\n", style="dim")
        content.append("Daemon:  ", style="bold")
        content.append(str(proc.daemon) + "\n")
        if proc.start_method:
            content.append("Method:  ", style="bold")
            content.append(proc.start_method + "\n")
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

        info = self.query_one("#proc-info", Static)
        info.update(content)

        # Populate variable tree
        var_view = self.query_one("#proc-vars", VariableView)
        if scopes:
            var_view.update_variables(scopes, variables)
        else:
            var_view.clear()
            var_view.root.add_leaf("(no variables available)")

    def update_processes(self, processes: list[ProcessInfo]) -> None:
        """Replace process list and refresh the display."""
        self._processes = processes
        header = self.query_one("#procs-header", Label)
        header.update(f"Processes ({len(self._processes)})")
        self._populate_table()
        if self._processes:
            self._show_detail(0)
        else:
            info = self.query_one("#proc-info", Static)
            info.update(Text("No child processes found", style="dim"))
            var_view = self.query_one("#proc-vars", VariableView)
            var_view.clear()

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_refresh_processes(self) -> None:
        """Post message to app to re-fetch processes."""
        self.post_message(self.RefreshProcesses())
