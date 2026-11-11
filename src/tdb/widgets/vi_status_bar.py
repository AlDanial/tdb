"""The two-line footer of `tdb --vi FILE`, laid out like vim's.

Line 1 (the status line): the file name, ``[+]`` when the buffer has
changes, and the cursor position (vim's ruler, ``23,19``) on the right.

Line 2 (the command line): the ``:`` ``/`` ``?`` line being typed, the
last one entered (until a mode message replaces it), or the mode message
(``-- INSERT --``); pending keys (vim's 'showcmd') on the right.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from rich.cells import cell_len
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Label

if TYPE_CHECKING:
    from tdb.widgets.code_view import CodeView

_MODE_MESSAGES = {
    "insert": "-- INSERT --",
    "replace": "-- REPLACE --",
    "visual": "-- VISUAL --",
    "visual-line": "-- VISUAL LINE --",
}


def ruler(line: str, col: int, tabstop: int, *, inserting: bool = False) -> str:
    """vim's ruler column for the cursor at codepoint *col* of *line*:
    the 1-based byte column, then ``-`` and the screen column when they
    differ (tabs, wide or multi-byte characters); ``0-1`` on an empty
    line.  Out of insert mode the cursor is on a tab's last cell, as in
    vim (*inserting*: before the tab)."""
    if not line:
        return "0-1"
    col = min(col, len(line))
    prefix = line[:col]
    byte_col = len(prefix.encode("utf-8", "surrogatepass")) + 1
    screen = 0
    for ch in prefix:
        screen += tabstop - screen % tabstop if ch == "\t" else cell_len(ch)
    # On a tab (in normal mode) vim's cursor sits on the tab's last cell.
    if not inserting and col < len(line) and line[col] == "\t":
        screen += tabstop - screen % tabstop - 1
    screen_col = screen + 1
    return str(byte_col) if byte_col == screen_col else f"{byte_col}-{screen_col}"


def display_name(path: str | None) -> str:
    """*path* as vim shows it: relative to the current directory when
    it is inside it, else as it is."""
    if not path:
        return "[No Name]"
    if os.path.isabs(path):
        try:
            rel = os.path.relpath(path)
        except ValueError:  # Windows: another drive
            return path
        if not rel.startswith(os.pardir):
            return rel
    return path


class ViStatusBar(Widget):
    DEFAULT_CSS = """
    ViStatusBar {
        height: 2;
        width: 1fr;
    }
    ViStatusBar > Horizontal {
        height: 1;
        width: 1fr;
    }
    ViStatusBar #vi-status-line {
        background: $panel;
        color: $text;
    }
    ViStatusBar Label {
        height: 1;
    }
    ViStatusBar #vi-file, ViStatusBar #vi-message {
        width: 1fr;
    }
    ViStatusBar #vi-ruler {
        width: auto;
        min-width: 14;
        padding: 0 1;
    }
    ViStatusBar #vi-showcmd {
        width: auto;
        min-width: 14;
        padding: 0 1;
    }
    ViStatusBar #vi-message.mode {
        text-style: bold;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # `ViEngine.lines_closed` when a mode message last took line 2:
        # the line closed before it is no longer shown.
        self._cleared_at = -1

    def compose(self) -> ComposeResult:
        with Horizontal(id="vi-status-line"):
            yield Label("", id="vi-file", markup=False)
            yield Label("", id="vi-ruler", markup=False)
        with Horizontal(id="vi-command-line"):
            yield Label("", id="vi-message", markup=False)
            yield Label("", id="vi-showcmd", markup=False)

    def show(self, view: CodeView) -> None:
        """Redraw both lines from *view*'s editor."""
        file_line, ruler_text, message, showcmd, is_mode = self.texts(view)
        self.query_one("#vi-file", Label).update(file_line)
        self.query_one("#vi-ruler", Label).update(ruler_text)
        msg = self.query_one("#vi-message", Label)
        msg.update(message)
        msg.set_class(is_mode, "mode")
        self.query_one("#vi-showcmd", Label).update(showcmd)

    def texts(self, view: CodeView) -> tuple[str, str, str, str, bool]:
        """``(file, ruler, message, showcmd, message is a mode message)``."""
        name = display_name(view.source_path)
        if view.is_dirty:
            name += " [+]"
        editor = view._editor
        if editor is None:
            return name, "", "", "", False
        row, col = editor.cursor_location
        line = editor.document.get_line(row) if row < editor.document.line_count else ""
        engine = editor.vi_engine
        inserting = engine is not None and engine.mode in ("insert", "replace")
        col_text = ruler(line, col, editor.indent_width, inserting=inserting)
        ruler_text = f"{row + 1},{col_text}"
        if engine is None:
            return name, ruler_text, "", "", False
        mode, status = engine.mode, engine.status
        recording = status if status.startswith("recording @") else ""
        if mode in ("command", "search", "confirm"):
            return name, ruler_text, status, "", False
        if mode in _MODE_MESSAGES:
            self._cleared_at = engine.lines_closed
            return name, ruler_text, _MODE_MESSAGES[mode] + recording, "", True
        # Normal mode: the last line entered stays until a mode message.
        showcmd = "" if recording else status
        if recording:
            return name, ruler_text, recording, showcmd, False
        last = engine.last_line if engine.lines_closed != self._cleared_at else ""
        return name, ruler_text, last, showcmd, False
