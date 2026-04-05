"""Code view widget: displays source code with breakpoint gutter and current line highlight."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.events import Click, Key  # Click used by _CodeContent
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from tdbg.keybindings import KeybindingConfig, Mode

if TYPE_CHECKING:
    from tdbg.dap.types import SourceBreakpoint


# ---------------------------------------------------------------------------
# Modal dialogs
# ---------------------------------------------------------------------------

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

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

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


class _SearchModal(ModalScreen[str | None]):
    """Modal dialog for searching code."""

    DEFAULT_CSS = """
    _SearchModal {
        align: center middle;
    }
    _SearchModal #dialog {
        width: 50;
        height: auto;
        max-height: 12;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    _SearchModal Input {
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def compose(self):
        with Vertical(id="dialog"):
            yield Label("Search:")
            yield Input(placeholder="Search text...", id="search-input")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value if value else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Code content widget
# ---------------------------------------------------------------------------

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

    class LineClicked(Message):
        def __init__(self, y: int) -> None:
            self.y = y
            super().__init__()

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(self.LineClicked(event.y))


# ---------------------------------------------------------------------------
# Main CodeView
# ---------------------------------------------------------------------------

class CodeView(ScrollableContainer, can_focus=True):
    """Source code viewer with breakpoint gutter and current-line highlighting."""

    # Only keep non-printable key bindings; everything else goes through on_key
    BINDINGS = [
        Binding("pageup", "page_up", "Page Up", show=False),
        Binding("pagedown", "page_down", "Page Down", show=False),
    ]

    DEFAULT_CSS = """
    CodeView {
        background: $surface;
    }
    """

    source_path: reactive[str | None] = reactive(None)
    current_line: reactive[int | None] = reactive(None)
    cursor_line: reactive[int] = reactive(1)

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

    class ModeChanged(Message):
        def __init__(self, mode: Mode) -> None:
            self.mode = mode
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines: list[str] = []
        self._breakpoint_lines: set[int] = set()
        self._content: _CodeContent | None = None
        self._highlighted: list[Text] | None = None

        # Mode & keybindings
        self.mode = Mode.DEBUG
        self.keybindings = KeybindingConfig()
        self._count_buf = ""  # accumulates digit prefix

        # Search state
        self._search_term: str | None = None
        self._search_backward: bool = False

    def compose(self):
        self._content = _CodeContent("")
        yield self._content

    # ---- Key handling with mode + count prefix ----

    async def _on_key(self, event: Key) -> None:
        """Custom key handler implementing vim-style count + action keys."""
        key = event.key

        # ESC toggles mode
        if key == "escape":
            self._count_buf = ""
            if self.mode == Mode.DEBUG:
                self.mode = Mode.NAVIGATION
            else:
                self.mode = Mode.DEBUG
            self.post_message(self.ModeChanged(self.mode))
            event.stop()
            event.prevent_default()
            return

        # Accumulate digits for count prefix (but not '0' as first char in nav
        # mode — '0' could be a future "go to column 0" if desired)
        if key.isdigit() and (self._count_buf or key != "0"):
            self._count_buf += key
            event.stop()
            event.prevent_default()
            return

        # Look up action
        action = self.keybindings.lookup(self.mode, key)
        if action is None:
            self._count_buf = ""
            return  # let event propagate normally

        count = int(self._count_buf) if self._count_buf else 1
        self._count_buf = ""

        self._dispatch_action(action, count)
        event.stop()
        event.prevent_default()

    def _dispatch_action(self, action: str, count: int) -> None:
        """Execute an action with the given repeat count."""
        if action == "cursor_up":
            self.cursor_line = max(1, self.cursor_line - count)
        elif action == "cursor_down":
            self.cursor_line = min(len(self._lines) or 1, self.cursor_line + count)
        elif action == "goto_line":
            if self._lines:
                self.cursor_line = max(1, min(count, len(self._lines)))
        elif action == "goto_end":
            if self._lines:
                self.cursor_line = len(self._lines)
        elif action == "goto_home":
            self.cursor_line = 1
        elif action == "page_up":
            page = max(1, self.size.height - 2)
            self.cursor_line = max(1, self.cursor_line - page * count)
        elif action == "page_down":
            page = max(1, self.size.height - 2)
            self.cursor_line = min(len(self._lines) or 1, self.cursor_line + page * count)
        elif action == "paragraph_down":
            self._move_paragraph(down=True, count=count)
        elif action == "paragraph_up":
            self._move_paragraph(down=False, count=count)
        elif action == "search":
            self._open_search(backward=False)
        elif action == "search_back":
            self._open_search(backward=True)
        elif action == "search_next":
            for _ in range(count):
                self._search_step(forward=True)
        elif action == "search_prev":
            for _ in range(count):
                self._search_step(forward=False)
        # Debug actions
        elif action == "step_over":
            self.post_message(self.DebugAction("step_over"))
        elif action == "step_in":
            self.post_message(self.DebugAction("step_in"))
        elif action == "step_out":
            self.post_message(self.DebugAction("step_out"))
        elif action == "continue_":
            self.post_message(self.DebugAction("continue_"))
        elif action == "pause":
            self.post_message(self.DebugAction("pause"))
        elif action == "toggle_breakpoint":
            if self.source_path:
                self.post_message(self.BreakpointToggled(self.source_path, self.cursor_line))
        elif action == "run_to_cursor":
            if self.source_path:
                self.post_message(self.RunToCursor(self.source_path, self.cursor_line))

    # ---- Paragraph movement ----

    def _move_paragraph(self, *, down: bool, count: int) -> None:
        """Move cursor to next/previous blank-line boundary."""
        if not self._lines:
            return
        line = self.cursor_line  # 1-based
        for _ in range(count):
            line = self._find_paragraph_boundary(line, down)
        self.cursor_line = line

    def _find_paragraph_boundary(self, start: int, down: bool) -> int:
        """Find the next paragraph boundary from start (1-based)."""
        total = len(self._lines)
        if down:
            i = start  # 1-based; lines[i-1] is the text
            # Skip current non-blank lines
            while i <= total and self._lines[i - 1].strip():
                i += 1
            # Skip blank lines
            while i <= total and not self._lines[i - 1].strip():
                i += 1
            return min(i, total)
        else:
            i = start
            # Skip current non-blank lines upward
            while i > 1 and self._lines[i - 1].strip():
                i -= 1
            # Skip blank lines upward
            while i > 1 and not self._lines[i - 1].strip():
                i -= 1
            # Now find the start of this paragraph
            while i > 1 and self._lines[i - 2].strip():
                i -= 1
            return max(i, 1)

    # ---- Search ----

    def _open_search(self, backward: bool = False) -> None:
        self._search_backward = backward
        def on_dismiss(term: str | None) -> None:
            if term is not None:
                self._search_term = term
                self._do_search(from_line=self.cursor_line, backward=backward)
        self.app.push_screen(_SearchModal(), callback=on_dismiss)

    def _do_search(self, from_line: int, backward: bool) -> None:
        if not self._search_term or not self._lines:
            return
        term = self._search_term.lower()
        total = len(self._lines)
        step = -1 if backward else 1
        for i in range(1, total + 1):
            idx = (from_line - 1 + step * i) % total
            if term in self._lines[idx].lower():
                self.cursor_line = idx + 1
                return
        self.app.notify(f"Not found: {self._search_term}", title="Search")

    def _search_step(self, forward: bool) -> None:
        """Advance search in the given direction (respects initial search direction)."""
        if not self._search_term:
            return
        # n repeats original direction, N reverses it
        backward = self._search_backward if forward else not self._search_backward
        self._do_search(from_line=self.cursor_line, backward=backward)

    # ---- File loading & rendering ----

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
        target_y = line - 1
        half_height = self.size.height // 2
        self.scroll_to(y=max(0, target_y - half_height), animate=False)

    @staticmethod
    def _highlight_source(source: str) -> list[Text]:
        syntax = Syntax(source, "python", theme="monokai", line_numbers=False)
        text = syntax.highlight(source)
        lines = text.split("\n")
        for line in lines:
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

    def on__code_content_line_clicked(self, event: _CodeContent.LineClicked) -> None:
        self._toggle_breakpoint_at_content_y(event.y)

    def on_click(self, event: Click) -> None:
        """Handle clicks on the empty area to the right of _CodeContent."""
        if self.source_path is None or self.mode != Mode.DEBUG:
            return
        # event.y on CodeView is NOT scroll-adjusted and includes border row
        line = int(self.scroll_offset.y) + event.y
        if 1 <= line <= len(self._lines):
            self.post_message(self.BreakpointToggled(self.source_path, line))

    def _toggle_breakpoint_at_content_y(self, y: int) -> None:
        """Toggle breakpoint from _CodeContent click (y is scroll-adjusted)."""
        if self.source_path is None or self.mode != Mode.DEBUG:
            return
        line = y + 1
        if 1 <= line <= len(self._lines):
            self.post_message(self.BreakpointToggled(self.source_path, line))

    def watch_current_line(self, value: int | None) -> None:
        if value is not None:
            self.cursor_line = value
        self._render_code()
        if value is not None:
            # Defer scroll to allow layout to recompute virtual size after file load.
            # call_later is not enough when switching files in a complex layout.
            self.set_timer(0.05, lambda: self.goto_line(value))

    def watch_cursor_line(self, value: int) -> None:
        self._render_code()
        self.call_later(self.goto_line, value)

    # ---- Binding-based actions for pageup/pagedown (non-printable keys) ----

    def action_page_up(self) -> None:
        page = max(1, self.size.height - 2)
        self.cursor_line = max(1, self.cursor_line - page)

    def action_page_down(self) -> None:
        page = max(1, self.size.height - 2)
        self.cursor_line = min(len(self._lines) or 1, self.cursor_line + page)
