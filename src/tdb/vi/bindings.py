"""tdb's prompt_toolkit key bindings layered over the built-in vi bindings.

Bindings returned here are passed as the engine Application's
``key_bindings`` and so take priority over prompt_toolkit's own vi
bindings for the same key sequence.

The Ctrl+R redo binding is adapted from pyvim (``pyvim/key_bindings.py``):

    Copyright (c) 2015, Jonathan Slenders
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are
    met:

    * Redistributions of source code must retain the above copyright
      notice, this list of conditions and the following disclaimer.

    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.

    * Neither the name of the {organization} nor the names of its
      contributors may be used to endorse or promote products derived from
      this software without specific prior written permission.

    THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
    IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
    TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A
    PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
    HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
    SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED
    TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
    PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
    LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
    NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
    SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import ClipboardData
from prompt_toolkit.document import Document
from prompt_toolkit.filters import (
    Condition,
    is_read_only,
    vi_navigation_mode,
    vi_selection_mode,
)
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.vi import (
    TextObject,
    TextObjectType,
    create_operator_decorator,
    create_text_object_decorator,
)
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.selection import PasteMode, SelectionType

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPressEvent

    from tdb.vi.engine import ViEngine


def load_tdb_vi_bindings(engine: ViEngine) -> KeyBindings:
    """Key bindings that fix or extend prompt_toolkit's vi mode for *engine*."""
    kb = KeyBindings()

    @kb.add("c-r", filter=vi_navigation_mode, save_before=lambda e: False)
    def _redo(event: KeyPressEvent) -> None:
        """Redo (pyvim)."""
        for _ in range(event.arg):
            event.app.current_buffer.redo()

    @kb.add("c-v", filter=vi_navigation_mode | vi_selection_mode)
    def _no_block_visual(event: KeyPressEvent) -> None:
        """Block visual mode is unsupported: Ctrl+V does nothing."""

    @kb.add("c-z")
    def _no_ctrl_z(event: KeyPressEvent) -> None:
        """Ctrl+Z does nothing (prompt_toolkit would insert a literal ``\\x1a``)."""

    _add_change_word(kb)
    _add_nvim_cursor_fixes(kb, engine)

    return kb


def _add_change_word(kb: KeyBindings) -> None:
    """vim's ``cw``/``cW`` special case, missing from prompt_toolkit.

    With the cursor on a non-blank, ``cw`` changes to the end of the word
    (like ``ce``) instead of also eating the following white space.  This
    needs a tdb-owned ``c`` operator so the ``w``/``W`` text objects can
    tell a change apart from ``d``/``y``.
    """
    operator = create_operator_decorator(kb)
    text_object = create_text_object_decorator(kb)

    @operator("c", filter=~is_read_only)
    def change(event: KeyPressEvent, text_obj: TextObject) -> None:
        """``c{motion}`` (same as prompt_toolkit's, minus registers)."""
        buff = event.current_buffer
        new_document, clipboard_data = text_obj.cut(buff)
        buff.document = new_document
        if clipboard_data.text:
            event.app.clipboard.set_data(clipboard_data)
        event.app.vi_state.input_mode = InputMode.INSERT

    changing = Condition(lambda: get_app().vi_state.operator_func is change)

    def change_word(event: KeyPressEvent, *, WORD: bool) -> TextObject:
        doc = event.current_buffer.document
        if doc.current_char and not doc.current_char.isspace():
            end = doc.find_next_word_ending(
                count=event.arg, include_current_position=True, WORD=WORD
            )
            if end:
                return TextObject(end - 1, type=TextObjectType.INCLUSIVE)
        return TextObject(
            doc.find_next_word_beginning(count=event.arg, WORD=WORD)
            or doc.get_end_of_document_position()
        )

    @text_object("w", filter=changing, no_move_handler=True, no_selection_handler=True)
    def _change_word(event: KeyPressEvent) -> TextObject:
        return change_word(event, WORD=False)

    @text_object("W", filter=changing, no_move_handler=True, no_selection_handler=True)
    def _change_WORD(event: KeyPressEvent) -> TextObject:
        return change_word(event, WORD=True)


def _set_cursor(buff: Buffer, row: int, col: int) -> None:
    """Put the cursor at (*row*, *col*), clamped to a normal-mode position."""
    lines = buff.document.lines
    row = max(0, min(row, len(lines) - 1))
    col = max(0, min(col, len(lines[row]) - 1))
    buff.cursor_position = buff.document.translate_row_col_to_index(row, col)


def _add_nvim_cursor_fixes(kb: KeyBindings, engine: ViEngine) -> None:
    """Match nvim's final cursor position where prompt_toolkit differs.

    The reference is nvim with ``nostartofline``: edits that vim does not
    consider "move to the first non-blank" keep the column.
    """
    operator = create_operator_decorator(kb)

    @kb.add("d", "d", filter=vi_navigation_mode & ~is_read_only)
    def _delete_line(event: KeyPressEvent) -> None:
        """``dd``: linewise delete, cursor keeps its column."""
        buff = event.current_buffer
        doc = buff.document
        row, col = doc.cursor_position_row, doc.cursor_position_col
        lines = doc.lines
        deleted = "\n".join(lines[row : row + event.arg])
        rest = lines[:row] + lines[row + event.arg :]
        buff.document = Document("\n".join(rest), 0)
        event.app.clipboard.set_data(ClipboardData(deleted, SelectionType.LINES))
        _set_cursor(buff, row, col)

    def paste(event: KeyPressEvent, mode: PasteMode) -> None:
        buff = event.current_buffer
        data = event.app.clipboard.get_data()
        row = buff.document.cursor_position_row
        buff.paste_clipboard_data(data, count=event.arg, paste_mode=mode)
        if data.type == SelectionType.LINES and data.text:
            first = row + 1 if mode == PasteMode.VI_AFTER else row
            line = buff.document.lines[first]
            _set_cursor(buff, first, len(line) - len(line.lstrip()))

    @kb.add("p", filter=vi_navigation_mode)
    def _paste(event: KeyPressEvent) -> None:
        paste(event, PasteMode.VI_AFTER)

    @kb.add("P", filter=vi_navigation_mode)
    def _paste_before(event: KeyPressEvent) -> None:
        paste(event, PasteMode.VI_BEFORE)

    @kb.add("~", filter=vi_navigation_mode & ~is_read_only)
    def _tilde(event: KeyPressEvent) -> None:
        """``{count}~``: swap case of *count* characters, cursor after them."""
        buff = event.current_buffer
        doc = buff.document
        line = doc.current_line
        col = doc.cursor_position_col
        n = min(event.arg, len(line) - col)
        if n <= 0:
            return
        start = buff.cursor_position
        new = doc.text[:start] + line[col : col + n].swapcase() + doc.text[start + n :]
        buff.document = Document(new, start)
        _set_cursor(buff, doc.cursor_position_row, col + n)

    def shift(event: KeyPressEvent, rows: tuple[int, int], dedent: bool) -> None:
        """Shift rows ``[rows[0], rows[1])`` one ``indent_unit``; keep the column."""
        buff = event.current_buffer
        col = buff.document.cursor_position_col
        unit = engine.indent_unit

        def change(line: str) -> str:
            if not dedent:
                return unit + line if line else line
            if line.startswith(unit):
                return line[len(unit) :]
            n = 0
            while n < len(unit) and n < len(line) and line[n] in " \t":
                n += 1
                if line[n - 1] == "\t":
                    break
            return line[n:]

        text = buff.transform_lines(range(*rows), change)
        buff.document = Document(text, 0)
        _set_cursor(buff, rows[0], col)

    @kb.add(">", ">", filter=vi_navigation_mode & ~is_read_only)
    def _indent(event: KeyPressEvent) -> None:
        row = event.current_buffer.document.cursor_position_row
        shift(event, (row, row + event.arg), False)

    @kb.add("<", "<", filter=vi_navigation_mode & ~is_read_only)
    def _unindent(event: KeyPressEvent) -> None:
        row = event.current_buffer.document.cursor_position_row
        shift(event, (row, row + event.arg), True)

    @operator(">", filter=~is_read_only)
    def _indent_op(event: KeyPressEvent, text_obj: TextObject) -> None:
        first, last = text_obj.get_line_numbers(event.current_buffer)
        shift(event, (first, last + 1), False)

    @operator("<", filter=~is_read_only)
    def _unindent_op(event: KeyPressEvent, text_obj: TextObject) -> None:
        first, last = text_obj.get_line_numbers(event.current_buffer)
        shift(event, (first, last + 1), True)

    @operator("y")
    def _yank(event: KeyPressEvent, text_obj: TextObject) -> None:
        """``y{motion}``: the cursor moves to the start of the yanked text."""
        buff = event.current_buffer
        _, data = text_obj.cut(buff)
        if data.text:
            event.app.clipboard.set_data(data)
        buff.cursor_position += min(text_obj.sorted()[0], 0)

    def transform(keys: tuple[str, ...], func) -> None:
        @operator(*keys, filter=~is_read_only)
        def _(event: KeyPressEvent, text_obj: TextObject) -> None:
            buff = event.current_buffer
            start, end = text_obj.operator_range(buff.document)
            if start < end:
                buff.transform_region(
                    buff.cursor_position + start, buff.cursor_position + end, func
                )
                buff.cursor_position += min(text_obj.sorted()[0], 0)

    transform(("g", "u"), str.lower)
    transform(("g", "U"), str.upper)
    transform(("g", "~"), str.swapcase)

    def transform_line(name: str, func) -> None:
        @kb.add("g", name, name, filter=vi_navigation_mode & ~is_read_only)
        def _(event: KeyPressEvent) -> None:
            """``gUU``/``guu``/``g~~``: like ``gU_``; the cursor never moves right."""
            buff = event.current_buffer
            doc = buff.document
            row, col = doc.cursor_position_row, doc.cursor_position_col
            line = doc.current_line
            text = buff.transform_lines(range(row, row + event.arg), func)
            buff.document = Document(text, 0)
            _set_cursor(buff, row, min(col, len(line) - len(line.lstrip())))

    transform_line("U", str.upper)
    transform_line("u", str.lower)
    transform_line("~", str.swapcase)

    text_object = create_text_object_decorator(kb)

    def has_count(event: KeyPressEvent) -> bool:
        """Whether a count was typed (``dG`` always carries ``_arg == "1"``)."""
        if event.app.vi_state.operator_func is not None:
            return event.arg != 1
        return bool(event._arg)

    def goto_row(event: KeyPressEvent, row: int) -> TextObject:
        """``G``/``gg`` target: *row*, same column (nvim ``nostartofline``)."""
        doc = event.current_buffer.document
        row = max(0, min(row, doc.line_count - 1))
        col = min(doc.cursor_position_col, max(len(doc.lines[row]) - 1, 0))
        target = doc.translate_row_col_to_index(row, col)
        return TextObject(target - doc.cursor_position, type=TextObjectType.LINEWISE)

    @text_object("G")
    def _goto_last(event: KeyPressEvent) -> TextObject:
        doc = event.current_buffer.document
        row = event.arg - 1 if has_count(event) else doc.line_count - 1
        return goto_row(event, row)

    @text_object("g", "g")
    def _goto_first(event: KeyPressEvent) -> TextObject:
        return goto_row(event, event.arg - 1 if has_count(event) else 0)

    @operator("d", filter=~is_read_only)
    def _delete(event: KeyPressEvent, text_obj: TextObject) -> None:
        """``d{motion}``; linewise deletes remove whole lines, even the last."""
        buff = event.current_buffer
        if text_obj.type != TextObjectType.LINEWISE:
            new_doc, data = text_obj.cut(buff)
            buff.document = new_doc
            if data.text:
                event.app.clipboard.set_data(data)
            return
        col = buff.document.cursor_position_col
        first, last = text_obj.get_line_numbers(buff)
        lines = buff.document.lines
        deleted = "\n".join(lines[first : last + 1])
        buff.document = Document("\n".join(lines[:first] + lines[last + 1 :]), 0)
        event.app.clipboard.set_data(ClipboardData(deleted, SelectionType.LINES))
        _set_cursor(buff, first, col)

    @text_object("j", no_move_handler=True, no_selection_handler=True)
    def _down(event: KeyPressEvent) -> TextObject | None:
        """``dj``: fails (no-op) on the last line; the count is clamped."""
        doc = event.current_buffer.document
        if doc.cursor_position_row >= doc.line_count - 1:
            return None
        offset = doc.get_cursor_down_position(count=event.arg)
        return TextObject(offset, type=TextObjectType.LINEWISE)

    @text_object("k", no_move_handler=True, no_selection_handler=True)
    def _up(event: KeyPressEvent) -> TextObject | None:
        """``dk``: fails (no-op) on the first line; the count is clamped."""
        doc = event.current_buffer.document
        if doc.cursor_position_row == 0:
            return None
        offset = doc.get_cursor_up_position(count=event.arg)
        return TextObject(offset, type=TextObjectType.LINEWISE)
