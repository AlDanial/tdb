"""Code view widget: displays source code with breakpoint gutter and current line highlight."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

if TYPE_CHECKING:
    from tdbg.dap.types import SourceBreakpoint


class _GoToLineModal(ModalScreen[int | None]):
    """Modal dialog prompting for a line number."""

    DEFAULT_CSS = """
    _GoToLineModal {
        align: center middle;
    }

    _GoToLineModal #dialog {
        width: 40;
        height: auto;
        max-height: 12;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }

    _GoToLineModal Input {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self):
        with Vertical(id="dialog"):
            yield Label("Go to line:")
            yield Input(placeholder="Line number...", id="line-input")

    def on_mount(self) -> None:
        self.query_one("#line-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value.isdigit():
            self.dismiss(int(value))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _CodeContent(Static):
    """Inner static widget that renders the actual source code.

    Uses width:auto so long lines expand the widget rather than wrapping.
    This keeps 1 source line = 1 display row for reliable scroll positioning.
    """

    DEFAULT_CSS = """
    _CodeContent {
        width: auto;
    }
    """


class CodeView(ScrollableContainer, can_focus=True):
    """Source code viewer with breakpoint gutter and current-line highlighting."""

    BINDINGS = [
        # Navigation
        Binding("up", "scroll_up", "Up", show=False),
        Binding("down", "scroll_down", "Down", show=False),
        Binding("pageup", "page_up", "Page Up", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False),
        Binding("home", "scroll_home", "Home", show=False),
        Binding("end", "scroll_end", "End", show=False),
        # Debug actions (single-letter, GDB/PDB style)
        Binding("n", "step_over", "Step Over"),
        Binding("s", "step_in", "Step In"),
        Binding("o", "step_out", "Step Out"),
        Binding("c", "continue_", "Continue"),
        Binding("b", "toggle_breakpoint", "Toggle BP"),
        Binding("p", "pause", "Pause"),
        Binding("t", "run_to_cursor", "Run To Cursor"),
        Binding("L", "goto_line_prompt", "Go To Line"),
    ]

    DEFAULT_CSS = """
    CodeView {
        background: $surface;
    }
    """

    source_path: reactive[str | None] = reactive(None)
    current_line: reactive[int | None] = reactive(None)
    cursor_line: reactive[int] = reactive(1)  # user-movable cursor (1-based)

    class BreakpointToggled(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    class DebugAction(Message):
        def __init__(self, action: str) -> None:
            self.action = action
            super().__init__()

    class RunToCursor(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines: list[str] = []
        self._breakpoint_lines: set[int] = set()
        self._content: _CodeContent | None = None
        self._highlighted: list[Text] | None = None  # cached per file

    def compose(self):
        self._content = _CodeContent("")
        yield self._content

    def load_file(self, path: str) -> None:
        self.source_path = path
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = f"<Could not read {path}>"
        self._lines = text.splitlines()
        self._highlighted = self._highlight_source(text)
        self._render_code()

    def set_breakpoints(self, breakpoints: list[SourceBreakpoint]) -> None:
        self._breakpoint_lines = {bp.line for bp in breakpoints}
        self._render_code()

    def goto_line(self, line: int) -> None:
        if line < 1 or not self._lines:
            return
        # Each line is one row in the content; scroll to center it
        target_y = line - 1
        half_height = self.size.height // 2
        self.scroll_to(y=max(0, target_y - half_height), animate=False)

    @staticmethod
    def _highlight_source(source: str) -> list[Text]:
        """Use Rich Syntax to produce a list of highlighted Text lines.

        Strips the theme background from each span so that the widget's
        own background (or the current-line highlight) shows through.
        """
        syntax = Syntax(source, "python", theme="monokai", line_numbers=False)
        text = syntax.highlight(source)
        lines = text.split("\n")
        for line in lines:
            # Remove bgcolor from every span so we control the background
            new_spans = []
            for span in line._spans:
                style = span.style
                if isinstance(style, Style) and style.bgcolor:
                    style = Style(
                        color=style.color,
                        bold=style.bold,
                        italic=style.italic,
                        underline=style.underline,
                    )
                new_spans.append(span._replace(style=style))
            line._spans = new_spans
        return lines

    @staticmethod
    def _apply_line_bg(line: Text, bgcolor: str) -> Text:
        """Override the background of every span in a line."""
        result = line.copy()
        new_spans = []
        for span in result._spans:
            style = span.style
            if isinstance(style, Style):
                style = style + Style(bgcolor=bgcolor)
            new_spans.append(span._replace(style=style))
        result._spans = new_spans
        result.stylize(Style(bgcolor=bgcolor))
        return result

    def _render_code(self) -> None:
        """Rebuild the full code display as Rich Text."""
        if self._content is None:
            return

        highlighted = self._highlighted or [Text(line) for line in self._lines]

        output = Text()
        for i, line_text in enumerate(self._lines):
            line_num = i + 1
            is_current = self.current_line is not None and line_num == self.current_line
            is_cursor = line_num == self.cursor_line

            # Breakpoint marker
            if line_num in self._breakpoint_lines:
                output.append("● ", style="bold red")
            else:
                output.append("  ")

            # Line number
            if is_current:
                output.append(f"{line_num:>4} ", style="bright_white on rgb(120,100,30)")
            elif is_cursor:
                output.append(f"{line_num:>4} ", style="bright_white on rgb(60,60,80)")
            else:
                output.append(f"{line_num:>4} ", style="bright_black")

            # Syntax-highlighted source line
            if i < len(highlighted):
                hl_line = highlighted[i]
            else:
                hl_line = Text(line_text)

            if is_current:
                hl_line = self._apply_line_bg(hl_line, "rgb(120,100,30)")
            elif is_cursor:
                hl_line = self._apply_line_bg(hl_line, "rgb(60,60,80)")
            else:
                hl_line = hl_line.copy()

            output.append_text(hl_line)
            output.append("\n")

        self._content.update(output)

    def on_click(self, event: Click) -> None:
        if self.source_path is None:
            return
        line = int(self.scroll_offset.y) + event.y + 1
        if 1 <= line <= len(self._lines):
            self.post_message(self.BreakpointToggled(self.source_path, line))

    def watch_current_line(self, value: int | None) -> None:
        if value is not None:
            self.cursor_line = value  # snap cursor to the stopped line
        self._render_code()
        if value is not None:
            self.goto_line(value)

    def watch_cursor_line(self, value: int) -> None:
        self._render_code()
        # Defer scroll so layout updates first (needed after modal dismiss)
        self.call_later(self.goto_line, value)

    # --- Debug action handlers (forward to app) ---

    def action_step_over(self) -> None:
        self.post_message(self.DebugAction("step_over"))

    def action_step_in(self) -> None:
        self.post_message(self.DebugAction("step_in"))

    def action_step_out(self) -> None:
        self.post_message(self.DebugAction("step_out"))

    def action_continue_(self) -> None:
        self.post_message(self.DebugAction("continue_"))

    def action_pause(self) -> None:
        self.post_message(self.DebugAction("pause"))

    def action_run_to_cursor(self) -> None:
        if self.source_path:
            self.post_message(self.RunToCursor(self.source_path, self.cursor_line))

    def action_toggle_breakpoint(self) -> None:
        if self.source_path:
            self.post_message(self.BreakpointToggled(self.source_path, self.cursor_line))

    def action_scroll_up(self) -> None:
        if self.cursor_line > 1:
            self.cursor_line -= 1

    def action_scroll_down(self) -> None:
        if self.cursor_line < len(self._lines):
            self.cursor_line += 1

    def action_page_up(self) -> None:
        page = max(1, self.size.height - 2)
        self.cursor_line = max(1, self.cursor_line - page)

    def action_page_down(self) -> None:
        page = max(1, self.size.height - 2)
        self.cursor_line = min(len(self._lines), self.cursor_line + page)

    def action_scroll_home(self) -> None:
        self.cursor_line = 1

    def action_scroll_end(self) -> None:
        if self._lines:
            self.cursor_line = len(self._lines)

    def action_goto_line_prompt(self) -> None:
        def on_dismiss(line: int | None) -> None:
            if line is not None and self._lines:
                self.cursor_line = max(1, min(line, len(self._lines)))
        self.app.push_screen(_GoToLineModal(), callback=on_dismiss)
