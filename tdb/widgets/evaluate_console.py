"""Evaluate console: REPL-style widget using DAP evaluate requests."""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.message import Message
from textual.widgets import Input, RichLog
from rich.text import Text


class _EvalInput(Input):
    """Input subclass that intercepts Tab for DAP completions."""

    BINDINGS = [
        Binding("tab", "complete", "Complete", show=False, priority=True),
    ]

    class TabPressed(Message):
        """Posted when the user presses Tab for completion."""

        def __init__(self, text: str, column: int) -> None:
            self.text = text
            self.column = column
            super().__init__()

    def action_complete(self) -> None:
        self.post_message(self.TabPressed(self.value, self.cursor_position + 1))


class EvaluateConsole(Vertical):
    """A REPL console that evaluates expressions via DAP."""

    DEFAULT_CSS = """
    EvaluateConsole {
        height: 1fr;
    }

    EvaluateConsole RichLog {
        height: 1fr;
    }

    EvaluateConsole Input {
        dock: bottom;
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

    class HelpRequested(Message):
        def __init__(self, expression: str) -> None:
            self.expression = expression
            super().__init__()

    class CompletionRequested(Message):
        def __init__(self, text: str, column: int) -> None:
            self.text = text
            self.column = column
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Evaluate"
        self._history: list[str] = []
        self._history_idx = 0

    def compose(self):
        log = RichLog(id="eval-output", highlight=True, markup=False, wrap=True, auto_scroll=True)
        log.can_focus = False
        yield log
        yield _EvalInput(id="eval-input", placeholder=">>> Enter expression...")

    def focus_input(self) -> None:
        self.query_one("#eval-input", _EvalInput).focus()

    def on__eval_input_tab_pressed(self, event: _EvalInput.TabPressed) -> None:
        self.post_message(self.CompletionRequested(event.text, event.column))

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
        input_widget = self.query_one("#eval-input", _EvalInput)
        input_widget.value = ""

        # Help query: "obj.method?" → show docstring
        if expression.endswith("?"):
            self.post_message(self.HelpRequested(expression[:-1]))
        else:
            self.post_message(self.EvaluateRequested(expression))

    def apply_completion(self, text: str, completions: list[tuple[str, str | None]]) -> None:
        """Apply completions returned by DAP.

        Args:
            text: The original input text that was sent for completion.
            completions: List of (label, insert_text) pairs.
        """
        input_widget = self.query_one("#eval-input", _EvalInput)
        output = self.query_one("#eval-output", RichLog)

        if not completions:
            return

        if len(completions) == 1:
            # Single match — auto-complete
            label, insert_text = completions[0]
            replacement = insert_text or label
            # Find the partial token to replace (text after last '.')
            current = input_widget.value
            dot_pos = current.rfind(".")
            if dot_pos >= 0:
                input_widget.value = current[: dot_pos + 1] + replacement
            else:
                input_widget.value = replacement
            input_widget.cursor_position = len(input_widget.value)
        else:
            # Multiple matches — find common prefix and auto-complete that
            labels = [t or l for l, t in completions]
            prefix = _common_prefix(labels)

            current = input_widget.value
            dot_pos = current.rfind(".")
            partial = current[dot_pos + 1 :] if dot_pos >= 0 else current

            if len(prefix) > len(partial):
                # Extend to common prefix
                if dot_pos >= 0:
                    input_widget.value = current[: dot_pos + 1] + prefix
                else:
                    input_widget.value = prefix
                input_widget.cursor_position = len(input_widget.value)

            # Show all matches in output
            items = [t or l for l, t in completions]
            output.write(Text("  ".join(items), style="dim"))

    def show_result(self, result: str) -> None:
        output = self.query_one("#eval-output", RichLog)
        output.write(Text(result))

    def show_error(self, error: str) -> None:
        output = self.query_one("#eval-output", RichLog)
        output.write(Text(error, style="red"))

    def action_history_back(self) -> None:
        if self._history and self._history_idx > 0:
            self._history_idx -= 1
            input_widget = self.query_one("#eval-input", _EvalInput)
            input_widget.value = self._history[self._history_idx]

    def action_history_forward(self) -> None:
        input_widget = self.query_one("#eval-input", _EvalInput)
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            input_widget.value = self._history[self._history_idx]
        else:
            self._history_idx = len(self._history)
            input_widget.value = ""


def _common_prefix(strings: list[str]) -> str:
    """Return the longest common prefix of a list of strings."""
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix
