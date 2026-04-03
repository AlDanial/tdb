"""Console view: displays stdout/stderr from the debugged program."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import RichLog


class ConsoleView(RichLog):
    """Scrollable log for program output (stdout/stderr)."""

    DEFAULT_CSS = """
    ConsoleView {
        border: solid $primary;
        border-title-color: $text;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(
            highlight=False,
            markup=False,
            wrap=True,
            auto_scroll=True,
            **kwargs,
        )
        self.border_title = "Console"
        self.can_focus = False

    def write_output(self, text: str, category: str = "stdout") -> None:
        if category == "stderr":
            self.write(Text(text, style="red"))
        elif category == "console":
            self.write(Text(text, style="dim"))
        else:
            self.write(Text(text))
