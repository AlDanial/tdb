"""Breakpoint view: displays and manages breakpoints."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.binding import Binding
from textual.message import Message
from textual.widgets import DataTable

if TYPE_CHECKING:
    from tdb.dap.types import SourceBreakpoint


class BreakpointView(DataTable):
    """Table showing all set breakpoints."""

    DEFAULT_CSS = """
    BreakpointView {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("d", "toggle_enabled_under_cursor", "Enable/Disable"),
        Binding("c", "clear_under_cursor", "Clear"),
        Binding("D", "toggle_disable_all", "Disable All"),
        Binding("C", "clear_all", "Clear All"),
    ]

    class BreakpointSelected(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    class BreakpointConditionRequested(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    class BreakpointDeleteRequested(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    class BreakpointToggleEnabledRequested(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    class DisableAllRequested(Message):
        pass

    class ClearAllRequested(Message):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "[bold orange]B[/]reakpoints"
        self.cursor_type = "row"
        self._entries: list[tuple[str, int]] = []

    def on_mount(self) -> None:
        self.add_columns("", "File", "Line", "Condition", "Hits")

    def update_breakpoints(
        self, breakpoints: dict[str, list[SourceBreakpoint]]
    ) -> None:
        self.clear()
        self._entries.clear()

        for source_path, bps in sorted(breakpoints.items()):
            for bp in sorted(bps, key=lambda b: b.line):
                filename = Path(source_path).name
                condition = bp.condition or ""
                hit_condition = bp.hit_condition or ""
                marker = "✓" if bp.enabled else "·"
                self.add_row(marker, filename, str(bp.line), condition, hit_condition)
                self._entries.append((source_path, bp.line))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_idx = event.cursor_row
        if 0 <= row_idx < len(self._entries):
            source_path, line = self._entries[row_idx]
            self.post_message(self.BreakpointConditionRequested(source_path, line))

    def set_disabled_state(self, disabled: bool) -> None:
        """Update the binding label to reflect current disabled state."""
        self._bindings.bind(
            "D",
            "toggle_disable_all",
            "Enable All" if disabled else "Disable All",
        )

    def action_toggle_disable_all(self) -> None:
        self.post_message(self.DisableAllRequested())

    def action_clear_all(self) -> None:
        self.post_message(self.ClearAllRequested())

    def action_clear_under_cursor(self) -> None:
        row_idx = self.cursor_row
        if row_idx is not None and 0 <= row_idx < len(self._entries):
            source_path, line = self._entries[row_idx]
            self.post_message(self.BreakpointDeleteRequested(source_path, line))

    def action_toggle_enabled_under_cursor(self) -> None:
        row_idx = self.cursor_row
        if row_idx is not None and 0 <= row_idx < len(self._entries):
            source_path, line = self._entries[row_idx]
            self.post_message(self.BreakpointToggleEnabledRequested(source_path, line))
