"""Evaluate console: REPL-style widget using DAP evaluate requests."""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, RichLog
from rich.text import Text


class EvaluateConsole(Vertical):
    """A REPL console that evaluates expressions via DAP."""

    DEFAULT_CSS = """
    EvaluateConsole {
        border: solid $primary;
        border-title-color: $text;
        height: 1fr;
    }

    EvaluateConsole RichLog {
        height: 1fr;
    }

    EvaluateConsole Input {
        dock: bottom;
        height: 1;
    }
    """

    BINDINGS = [
        Binding("up", "history_back", "Previous", show=False),
        Binding("down", "history_forward", "Next", show=False),
    ]

    class EvaluateRequested(Message):
        def __init__(self, expression: str) -> None:
            self.expression = expression
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Evaluate"
        self._history: list[str] = []
        self._history_idx = 0

    def compose(self):
        yield RichLog(id="eval-output", highlight=True, markup=False, wrap=True, auto_scroll=True)
        yield Input(id="eval-input", placeholder=">>> Enter expression...")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        expression = event.value.strip()
        if not expression:
            return

        # Add to history
        self._history.append(expression)
        self._history_idx = len(self._history)

        # Show the expression in the output
        output = self.query_one("#eval-output", RichLog)
        output.write(Text(f">>> {expression}", style="bold cyan"))

        # Clear input
        input_widget = self.query_one("#eval-input", Input)
        input_widget.value = ""

        # Request evaluation
        self.post_message(self.EvaluateRequested(expression))

    def show_result(self, result: str) -> None:
        output = self.query_one("#eval-output", RichLog)
        output.write(Text(result))

    def show_error(self, error: str) -> None:
        output = self.query_one("#eval-output", RichLog)
        output.write(Text(error, style="red"))

    def action_history_back(self) -> None:
        if self._history and self._history_idx > 0:
            self._history_idx -= 1
            input_widget = self.query_one("#eval-input", Input)
            input_widget.value = self._history[self._history_idx]

    def action_history_forward(self) -> None:
        input_widget = self.query_one("#eval-input", Input)
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            input_widget.value = self._history[self._history_idx]
        else:
            self._history_idx = len(self._history)
            input_widget.value = ""
