"""Code view widget: displays source code with breakpoint gutter and current line highlight."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

if TYPE_CHECKING:
    from tdbg.dap.types import SourceBreakpoint


class _CodeContent(Static):
    """Inner static widget that renders the actual source code."""
    pass


class CodeView(VerticalScroll, can_focus=True):
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
    ]

    DEFAULT_CSS = """
    CodeView {
        background: $surface;
    }
    """

    source_path: reactive[str | None] = reactive(None)
    current_line: reactive[int | None] = reactive(None)

    class BreakpointToggled(Message):
        def __init__(self, source_path: str, line: int) -> None:
            self.source_path = source_path
            self.line = line
            super().__init__()

    class DebugAction(Message):
        def __init__(self, action: str) -> None:
            self.action = action
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines: list[str] = []
        self._breakpoint_lines: set[int] = set()
        self._content: _CodeContent | None = None

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

    def _render_code(self) -> None:
        """Rebuild the full code display as Rich Text."""
        if self._content is None:
            return

        output = Text()
        for i, line_text in enumerate(self._lines):
            line_num = i + 1

            # Breakpoint marker
            if line_num in self._breakpoint_lines:
                output.append("● ", style="bold red")
            else:
                output.append("  ")

            # Line number
            output.append(f"{line_num:>4} ", style="bright_black")

            # Source line
            is_current = self.current_line is not None and line_num == self.current_line
            if is_current:
                output.append(f"{line_text}\n", style="bold on dark_goldenrod")
            else:
                output.append(f"{line_text}\n")

        self._content.update(output)

    def on_click(self, event: Click) -> None:
        if self.source_path is None:
            return
        line = int(self.scroll_offset.y) + event.y + 1
        if 1 <= line <= len(self._lines):
            self.post_message(self.BreakpointToggled(self.source_path, line))

    def watch_current_line(self, value: int | None) -> None:
        self._render_code()
        if value is not None:
            self.goto_line(value)

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

    def action_toggle_breakpoint(self) -> None:
        if self.source_path and self.current_line:
            self.post_message(self.BreakpointToggled(self.source_path, self.current_line))
