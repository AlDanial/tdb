"""Modal screen showing active child processes with detail view."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.message import Message
from textual.widgets import DataTable, Static

from tdb.inspection import PROCESS_COLLECT_EXPR, ProcessInfo, parse_process_json
from tdb.widgets._inspection_modal import _InspectableListModal
from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Scope, StackFrame, Variable

# Re-export so existing `from tdb.widgets.processes_modal import …` callers
# continue to work. Definitions live in tdb.inspection (UI-free).
__all__ = [
    "ProcessesModal",
    "ProcessInfo",
    "PROCESS_COLLECT_EXPR",
    "parse_process_json",
]

log = logging.getLogger(__name__)


class ProcessesModal(_InspectableListModal[ProcessInfo]):
    """Near-full-screen modal showing child processes with stack and variables."""

    KIND_LABEL = "Processes"
    TABLE_COLUMNS = ("PID", "Name", "Status")
    FOOTER_HINT = "ESC close  |  r refresh  |  Enter/double-click jump to process"

    def __init__(
        self,
        processes: list[ProcessInfo],
        detail_cache: dict[int, dict] | None = None,
        current_pid: int | None = None,
    ) -> None:
        super().__init__()
        self._items: list[ProcessInfo] = processes
        self._mounted = False
        # PID of the process whose detail pane is currently displayed.
        # Used by the app to route variable lazy-loads to the correct
        # child DAPClient (variablesReference is scoped per-session).
        # When restored from a cached snapshot the previously-viewed pid
        # is passed in so on_mount lands the cursor there.
        self._current_pid: int | None = current_pid
        # pid → {"frames": [...], "scopes": [...], "variables": {ref: [...]}}.
        # Populated by show_process_detail; consulted by _show_detail so
        # cursor-moves over already-loaded rows don't re-hit DAP. Also
        # serialized on dismiss for the next open within this stop.
        self._detail_cache: dict[int, dict] = dict(detail_cache or {})

    # --- Mount: handle the "fresh open, list still empty" case --------

    def on_mount(self) -> None:
        self._mounted = True
        if not self._items:
            # The Processes modal is unique among the inspection modals
            # in that it can be pushed before the worker has fetched the
            # process list. Show a "Loading..." placeholder until
            # `update_processes` is called with the fetched data, instead
            # of the default "No processes found" empty state.
            table = self.query_one("#table", DataTable)
            table.add_columns(*self.TABLE_COLUMNS)
            info = self.query_one("#info", Static)
            info.update(Text("Loading...", style="dim italic"))
            return
        super().on_mount()

    def _empty_state_text(self) -> str:
        # Used by the base only for the post-update empty case.
        return "No child processes found"

    # --- Row + detail rendering ---------------------------------------

    def _format_row(self, proc: ProcessInfo) -> tuple:
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
        return (pid, name, status)

    def _render_loading_detail(self, proc: ProcessInfo) -> Text:
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
        return content

    def _select_id_for(self, proc: ProcessInfo) -> int | None:
        # Skip dead and not-yet-started processes: there's no live DAP
        # session to attach to, so SelectProcess would be a no-op.
        if proc.pid is None or not proc.alive:
            return None
        return proc.pid

    def _make_select_message(self, pid: int) -> Message:
        return self.SelectProcess(pid)

    def _make_refresh_message(self) -> Message:
        return self.RefreshProcesses()

    def _on_after_show_detail(self, proc: ProcessInfo) -> None:
        # Hit the per-pid cache if we have it — reopening a process the
        # user already viewed (within this stop episode) skips the DAP
        # round-trip. Cache misses post LoadProcessDetail and let the
        # workflow side fetch.
        if proc.pid is None or not proc.alive:
            return
        cached = self._detail_cache.get(proc.pid)
        if cached is not None:
            self.show_process_detail(
                proc.pid,
                cached["frames"],
                cached["scopes"],
                cached["variables"],
            )
        else:
            self.post_message(self.LoadProcessDetail(proc.pid))

    def _initial_cursor_index(self) -> int:
        if self._current_pid is None:
            return 0
        for i, p in enumerate(self._items):
            if p.pid == self._current_pid:
                return i
        return 0

    # --- Messages -----------------------------------------------------

    class LoadProcessDetail(Message):
        """Request to fetch stack trace and variables for a child process."""
        def __init__(self, pid: int) -> None:
            self.pid = pid
            super().__init__()

    class SelectProcess(Message):
        """User double-clicked / Enter'd a row: close the modal and
        switch the main Code/Stack/Variable views to this process.
        Subsequent step / continue commands target this process too."""
        def __init__(self, pid: int) -> None:
            self.pid = pid
            super().__init__()

    class RefreshProcesses(Message):
        """Request to refresh the process list."""

    # --- Workflow-callable methods -----------------------------------

    def show_process_detail(
        self,
        pid: int,
        frames: list[StackFrame],
        scopes: list[Scope],
        variables: dict[int, list[Variable]],
    ) -> None:
        """Populate the detail pane with stack trace and variables."""
        proc = next((p for p in self._items if p.pid == pid), None)
        if proc is None:
            return
        self._current_pid = pid
        # Remember what we got so close+reopen (or a cursor revisit)
        # can short-circuit the DAP round-trip.
        self._detail_cache[pid] = {
            "frames": frames,
            "scopes": scopes,
            "variables": variables,
        }

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

        info = self.query_one("#info", Static)
        info.update(content)

        var_view = self.query_one("#vars", VariableView)
        if scopes:
            var_view.update_variables(scopes, variables)
        else:
            var_view.clear()
            var_view.root.add_leaf("(no variables available)")

    def update_processes(self, processes: list[ProcessInfo]) -> None:
        """Replace process list and refresh the display."""
        self._items = processes
        # Any previously-cached per-pid detail is stale relative to the
        # new process list (this is called from refresh and from the
        # initial worker fill-in). Drop the cache so the next row-
        # highlight fetches fresh.
        self._detail_cache.clear()
        if not self._mounted:
            return
        self._reload_after_items_change()

    def cache_snapshot(
        self,
    ) -> tuple[list[ProcessInfo], dict[int, dict], int | None]:
        """Return (processes, detail_cache, current_pid) for serialization.

        Called by the inspection workflow on modal dismiss so the next
        open within the same stopped episode can skip all DAP fetches.
        """
        return (self._items, self._detail_cache, self._current_pid)

    @property
    def selected_pid(self) -> int | None:
        """The pid whose detail pane is currently displayed.

        Used by callers that need to route per-process DAP requests
        (e.g. lazy variable expansion) to the right child DAPClient
        without reaching into modal internals.
        """
        return self._current_pid
