"""Modal screen showing active threads with stack and variable detail."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from tdb.widgets._inspection_modal import _InspectableListModal
from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from tdb.dap.types import Scope, StackFrame, Thread, Variable

log = logging.getLogger(__name__)


class ThreadsModal(_InspectableListModal["Thread"]):
    """Near-full-screen modal showing all threads with stack and variables."""

    KIND_LABEL = "Threads"
    TABLE_COLUMNS = ("ID", "Name")
    FOOTER_HINT = "ESC close  |  r refresh  |  Enter/double-click jump to thread"

    def __init__(
        self,
        threads: list[Thread],
        current_thread_id: int | None = None,
        frame_name: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__()
        self._items: list[Thread] = threads
        self._current_thread_id = current_thread_id
        self._frame_name = frame_name

    # --- Row + detail rendering ---------------------------------------

    def _format_row(self, thread: Thread) -> tuple:
        tid = Text(str(thread.id))
        name = Text(thread.name)
        # Bold the current thread so it stands out without the user
        # having to remember which one debugpy reported as the stop.
        if thread.id == self._current_thread_id:
            tid.stylize("bold")
            name.stylize("bold")
        return (tid, name)

    def _render_loading_detail(self, thread: Thread) -> Text:
        content = Text()
        content.append("Thread ID: ", style="bold")
        content.append(str(thread.id) + "\n")
        content.append("Name:      ", style="bold")
        content.append(thread.name + "\n")
        if thread.id == self._current_thread_id:
            content.append("(current thread)\n", style="dim italic")
        content.append("\n")
        content.append("Loading stack...\n", style="dim")
        return content

    def _select_id_for(self, thread: Thread) -> int:
        return thread.id

    def _make_select_message(self, thread_id: int) -> Message:
        return self.SelectThread(thread_id)

    def _make_refresh_message(self) -> Message:
        return self.RefreshThreads()

    def _on_after_show_detail(self, thread: Thread) -> None:
        self.post_message(self.LoadThreadDetail(thread.id))

    def _initial_cursor_index(self) -> int:
        if self._current_thread_id is None:
            return 0
        for i, t in enumerate(self._items):
            if t.id == self._current_thread_id:
                return i
        return 0

    # --- Workflow-callable methods -----------------------------------

    class LoadThreadDetail(Message):
        """Request to fetch stack trace and variables for a thread."""

        def __init__(self, thread_id: int) -> None:
            self.thread_id = thread_id
            super().__init__()

    class SelectThread(Message):
        """User double-clicked / Enter'd a row: close modal and switch
        the main Code/Stack/Variable views to this thread. Subsequent
        step / continue commands will also target this thread."""

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
        thread = next((t for t in self._items if t.id == thread_id), None)
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
                name = self._frame_name(frame.name) if self._frame_name else frame.name
                content.append(f"  #{i} {name}{loc}\n")
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

    def update_threads(
        self,
        threads: list[Thread],
        current_thread_id: int | None = None,
    ) -> None:
        """Replace thread list and refresh the display."""
        self._items = threads
        self._current_thread_id = current_thread_id
        self._reload_after_items_change()
