"""Console view: the debugged program's stdout/stderr, plus its stdin.

The output log fills the pane; an input line docked under it feeds the
program's stdin when the session has one (see
DebugController.supports_stdin). A pipe has no terminal echo, so each
submitted line is echoed into the log where the program's prompt
already sits.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, RichLog


class _StdinInput(Input):
    """Input line for the program's stdin; Ctrl+D is EOF like a terminal."""

    BINDINGS = [
        Binding("ctrl+d", "send_eof", "EOF", show=False, priority=True),
    ]

    class EofPressed(Message):
        pass

    def action_send_eof(self) -> None:
        self.post_message(self.EofPressed())


class ConsoleView(Vertical):
    """Program output log with a stdin line docked at the bottom."""

    DEFAULT_CSS = """
    ConsoleView {
        height: 1fr;
    }

    ConsoleView RichLog {
        height: 1fr;
    }

    ConsoleView Input {
        dock: bottom;
    }
    """

    class StdinSubmitted(Message):
        """A line typed in the Console; `text` has no trailing newline."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class StdinEof(Message):
        """Ctrl+D in the Console input line."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "C[bold orange]o[/]nsole"
        self._stdin_enabled = False

    def compose(self):
        log = RichLog(
            id="console-log",
            highlight=False,
            markup=False,
            wrap=True,
            auto_scroll=True,
        )
        log.can_focus = True
        yield log
        field = _StdinInput(
            id="console-stdin",
            placeholder="stdin: type a line, Enter sends, Ctrl+D EOF",
        )
        field.display = False
        yield field

    # --- output -----------------------------------------------------------

    def write_output(self, text: str, category: str = "stdout") -> None:
        log = self.query_one("#console-log", RichLog)
        if category == "stderr":
            log.write(Text(text, style="red"))
        elif category == "console":
            log.write(Text(text, style="dim"))
        else:
            log.write(Text(text))

    def echo_input(self, text: str) -> None:
        """Show a submitted stdin line in the log; pipes don't echo."""
        self.query_one("#console-log", RichLog).write(Text(text, style="bold cyan"))

    # --- stdin ------------------------------------------------------------

    def set_stdin_enabled(self, enabled: bool) -> None:
        """Show the input line only while the program's stdin is writable."""
        self._stdin_enabled = enabled
        field = self.query_one("#console-stdin", _StdinInput)
        field.display = enabled
        if not enabled and field.has_focus:
            self.query_one("#console-log", RichLog).focus()

    def focus(self, scroll_visible: bool = True):  # type: ignore[override]
        """Ctrl+O lands on the stdin line when there is one, else the log."""
        if self._stdin_enabled:
            self.query_one("#console-stdin", _StdinInput).focus(scroll_visible)
        else:
            self.query_one("#console-log", RichLog).focus(scroll_visible)
        return self

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # No strip(): whitespace may be the answer, and a bare Enter is a
        # real (empty) answer to input(), not a no-op.
        event.stop()
        field = self.query_one("#console-stdin", _StdinInput)
        field.value = ""
        self.post_message(self.StdinSubmitted(event.value))

    def on__stdin_input_eof_pressed(self, event: _StdinInput.EofPressed) -> None:
        event.stop()
        self.post_message(self.StdinEof())
