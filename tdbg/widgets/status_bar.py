"""Status bar widget: shows running/paused state and current location."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class StatusBar(Static):
    """Single-line status bar showing debug state."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary-background;
        color: $text;
        content-align: left middle;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.can_focus = False

    def set_running(self) -> None:
        self.update(Text(" \u25b6 Running", style="bold green"))

    def set_paused(self, source_path: str | None, line: int | None) -> None:
        parts = Text(" \u275a\u275a Paused", style="bold yellow")
        if source_path and line is not None:
            parts.append(f"  \u2502  {source_path}:{line}", style="")
        self.update(parts)

    def set_terminated(self) -> None:
        self.update(Text(" \u25a0 Terminated", style="bold red"))

    def set_idle(self) -> None:
        self.update(Text(" Starting...", style="dim"))
