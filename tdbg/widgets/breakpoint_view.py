"""Breakpoint view: displays and manages breakpoints."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.message import Message
from textual.widgets import DataTable

if TYPE_CHECKING:
    from tdbg.dap.types import SourceBreakpoint


class BreakpointView(DataTable):
    """Table showing all set breakpoints."""

    DEFAULT_CSS = """
    BreakpointView {
        border: solid $primary;
        border-title-color: $text;
        height: 1fr;
    }
    """

    class BreakpointSelected(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    class BreakpointDeleteRequested(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Breakpoints"
        self.cursor_type = "row"
        self._entries: list[tuple[str, int]] = []

    def on_mount(self) -> None:
        self.add_columns("File", "Line", "Condition")

    def update_breakpoints(self, breakpoints: dict[str, list[SourceBreakpoint]]) -> None:
        self.clear()
        self._entries.clear()

        for source_path, bps in sorted(breakpoints.items()):
            for bp in sorted(bps, key=lambda b: b.line):
                filename = Path(source_path).name
                condition = bp.condition or ""
                self.add_row(filename, str(bp.line), condition)
                self._entries.append((source_path, bp.line))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_idx = event.cursor_row
        if 0 <= row_idx < len(self._entries):
            source_path, line = self._entries[row_idx]
            self.post_message(self.BreakpointSelected(source_path, line))
