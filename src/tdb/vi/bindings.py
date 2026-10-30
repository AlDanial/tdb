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
    vi_insert_mode,
    vi_navigation_mode,
    vi_selection_mode,
    vi_waiting_for_text_object_mode,
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

from tdb.vi.textobjects import (
    PAIRS,
    Cursor,
    NoObject,
    Pos,
    block_span,
    current_par,
    current_word,
    index,
    join_lines,
    quote_span,
)

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
    _add_text_objects(kb)
    _add_join(kb)
    _add_viewport_commands(kb, engine)
    _add_insert_tab(kb, engine)

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
        if text_obj.type == TextObjectType.LINEWISE:
            _change_rows(event, *text_obj.get_line_numbers(buff))
        else:
            new_document, clipboard_data = text_obj.cut(buff)
            buff.document = new_document
            if clipboard_data.text:
                event.app.clipboard.set_data(clipboard_data)
        event.app.vi_state.input_mode = InputMode.INSERT

    @kb.add("c", "c", filter=vi_navigation_mode & ~is_read_only)
    @kb.add("S", filter=vi_navigation_mode & ~is_read_only)
    def _change_line(event: KeyPressEvent) -> None:
        """``{count}cc`` / ``{count}S`` (prompt_toolkit ignores the count)."""
        doc = event.current_buffer.document
        row = doc.cursor_position_row
        _change_rows(event, row, min(row + event.arg, doc.line_count) - 1)
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


def _delete_rows(
    event: KeyPressEvent,
    first: int,
    last: int,
    col: int,
    replacement: str | None = None,
) -> None:
    """Linewise delete of rows ``first..last``: register + cursor at (*first*, *col*).

    With *replacement* the rows become that single line instead (``cc``).
    """
    buff = event.current_buffer
    if not buff.text and not getattr(buff, "eol", True):
        return  # an empty buffer has no line to delete (vim fails)
    lines = buff.document.lines
    deleted = "\n".join(lines[first : last + 1])
    keep = [] if replacement is None else [replacement]
    buff.document = Document("\n".join(lines[:first] + keep + lines[last + 1 :]), 0)
    event.app.clipboard.set_data(ClipboardData(deleted, SelectionType.LINES))
    _set_cursor(buff, first, col)


def _change_rows(event: KeyPressEvent, first: int, last: int) -> None:
    """Linewise change: rows ``first..last`` become one line keeping the indent."""
    buff = event.current_buffer
    lines = buff.document.lines
    indent = lines[first][: len(lines[first]) - len(lines[first].lstrip(" \t"))]
    _delete_rows(event, first, last, 0, indent)
    buff.cursor_position = index(buff.document.lines, (first, len(indent)))


def _add_nvim_cursor_fixes(kb: KeyBindings, engine: ViEngine) -> None:
    """Match nvim's final cursor position where prompt_toolkit differs.

    The reference is nvim with ``nostartofline``: edits that vim does not
    consider "move to the first non-blank" keep the column.
    """
    operator = create_operator_decorator(kb)

    @kb.add("d", "d", filter=vi_navigation_mode & ~is_read_only)
    def _delete_line(event: KeyPressEvent) -> None:
        """``dd``: linewise delete, cursor keeps its column."""
        doc = event.current_buffer.document
        row = doc.cursor_position_row
        _delete_rows(event, row, row + event.arg - 1, doc.cursor_position_col)

    def paste(event: KeyPressEvent, mode: PasteMode) -> None:
        buff = event.current_buffer
        data = event.app.clipboard.get_data()
        if not data.text and data.type != SelectionType.LINES:
            return  # empty register: nothing to put
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

    @kb.add("y", "y", filter=vi_navigation_mode)
    @kb.add("Y", filter=vi_navigation_mode)
    def _yank_line(event: KeyPressEvent) -> None:
        """``{count}yy`` / ``{count}Y``: a yank, so it also sets ``"0``."""
        doc = event.current_buffer.document
        text = "\n".join(doc.lines_from_current[: event.arg])
        data = ClipboardData(text, SelectionType.LINES)
        event.app.clipboard.yank(data)  # type: ignore[attr-defined]

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
        if data.text or data.type == SelectionType.LINES:
            event.app.clipboard.yank(data)  # type: ignore[attr-defined]
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
        first, last = text_obj.get_line_numbers(buff)
        _delete_rows(event, first, last, buff.document.cursor_position_col)

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


class _EmptyTextObject(TextObject):
    """A found-but-empty object (``di(`` on ``()``): operators change nothing."""

    def operator_range(self, document: Document) -> tuple[int, int]:
        return self.start, self.start

    def cut(self, buffer: Buffer) -> tuple[Document, ClipboardData]:
        pos = buffer.cursor_position + self.start
        return Document(buffer.text, pos), ClipboardData()


def _linewise(lines: list[str], cur: int, first: int, last: int) -> TextObject:
    return TextObject(
        index(lines, (first, 0)) - cur,
        index(lines, (last, 0)) - cur,
        type=TextObjectType.LINEWISE,
    )


def _charwise(
    lines: list[str], cur: int, start: Pos, end: Pos, inclusive: bool
) -> TextObject:
    """Operator object for *start*..*end*, with vim's exclusive-at-column-0 rule."""
    if not inclusive and end[1] == 0 and end[0] > start[0]:
        # "o_v" rule: the end moves to the end of the previous line, and the
        # motion becomes linewise when it starts in the indent.
        row = end[0] - 1
        first = lines[start[0]]
        if len(first) - len(first.lstrip(" \t")) >= start[1]:
            return _linewise(lines, cur, start[0], row)
        end = (row, len(lines[row]))
        if end[1]:
            end = (row, end[1] - 1)
            inclusive = True
    if inclusive and end[1] >= len(lines[end[0]]):
        inclusive = False  # inclusive of the line end: the break is not taken
    s, e = index(lines, start), index(lines, end) + (1 if inclusive else 0)
    if e <= s:
        return _EmptyTextObject(s - cur, s - cur)
    return TextObject(s - cur, e - 1 - cur, type=TextObjectType.INCLUSIVE)


def _add_text_objects(kb: KeyBindings) -> None:
    """vim-accurate ``iw aw iW aW ip ap`` and bracket / quote objects.

    Each object is registered twice: through ``create_text_object_decorator``
    for operator-pending mode (overriding prompt_toolkit's for the same keys,
    and a ``None`` result cancels the operator), and as a Visual-mode
    selection.  The decorator's own selection handler is disabled because it
    selects one character too many.
    """
    text_object = create_text_object_decorator(kb)
    visual = ~vi_waiting_for_text_object_mode & vi_selection_mode

    def register(keys: tuple[str, ...], pending, selecting) -> None:
        text_object(*keys, no_move_handler=True, no_selection_handler=True)(pending)
        kb.add(*keys, filter=visual)(selecting)

    def where(event: KeyPressEvent):
        """(lines, cursor index, cursor pos, Visual start pos or None)."""
        buff = event.current_buffer
        doc = buff.document
        pos = (doc.cursor_position_row, doc.cursor_position_col)
        sel = buff.selection_state
        vstart = None
        if sel is not None:
            vstart = doc.translate_index_to_position(sel.original_cursor_position)
        return doc.lines, doc.cursor_position, pos, vstart

    def select(
        event: KeyPressEvent, lines: list[str], start: Pos, end: Pos, linewise: bool
    ) -> None:
        """Select *start*..*end* (inclusive) in Visual mode."""
        buff = event.current_buffer
        sel = buff.selection_state
        if sel is None:
            return
        sel.original_cursor_position = index(lines, start)
        sel.type = SelectionType.LINES if linewise else SelectionType.CHARACTERS
        buff.cursor_position = index(lines, end)

    def word_object(include: bool, bigword: bool):
        def pending(event: KeyPressEvent) -> TextObject | None:
            lines, cur, pos, _ = where(event)
            try:
                found = current_word(lines, pos, event.arg, include, bigword, None)
            except NoObject as fail:
                _set_cursor(event.current_buffer, *fail.pos)
                return None
            return _charwise(lines, cur, *found)

        def selecting(event: KeyPressEvent) -> None:
            lines, _cur, pos, vstart = where(event)
            try:
                found = current_word(lines, pos, event.arg, include, bigword, vstart)
            except NoObject:
                return
            start, end, inclusive = found
            if not inclusive:
                c = Cursor(lines, end)
                c.dec()
                end = c.pos
            select(event, lines, start, end, False)

        return pending, selecting

    for keys, include, bigword in [
        (("i", "w"), False, False),
        (("a", "w"), True, False),
        (("i", "W"), False, True),
        (("a", "W"), True, True),
    ]:
        register(keys, *word_object(include, bigword))

    def para_object(include: bool):
        def pending(event: KeyPressEvent) -> TextObject | None:
            lines, cur, pos, _ = where(event)
            found = current_par(lines, pos[0], event.arg, include, None)
            return None if found is None else _linewise(lines, cur, *found)

        def selecting(event: KeyPressEvent) -> None:
            lines, _cur, pos, vstart = where(event)
            vrow = vstart[0] if vstart else None
            found = current_par(lines, pos[0], event.arg, include, vrow)
            if found is not None:
                select(event, lines, (found[0], 0), (found[1], 0), True)

        return pending, selecting

    register(("i", "p"), *para_object(False))
    register(("a", "p"), *para_object(True))

    def block_object(key: str, inner: bool):
        def pending(event: KeyPressEvent) -> TextObject | None:
            lines, cur, pos, _ = where(event)
            span = block_span(lines, pos, key, event.arg, inner, None)
            if span is None:
                return None
            start, end, sol = span
            if not inner:
                return _charwise(lines, cur, start, end, True)
            if sol:
                # vim steps over the line break when the closer starts its line.
                c = Cursor(lines, end)
                c.incl()
                return _charwise(lines, cur, start, c.pos, False)
            if start <= end:
                return _charwise(lines, cur, start, end, True)
            return _EmptyTextObject(
                index(lines, start) - cur, index(lines, start) - cur
            )

        def selecting(event: KeyPressEvent) -> None:
            lines, _cur, pos, vstart = where(event)
            selection = None if vstart is None else (min(vstart, pos), max(vstart, pos))
            span = block_span(lines, pos, key, event.arg, inner, selection)
            if span is None:
                return
            start, end, sol = span
            if sol and end[1] < len(lines[end[0]]):
                end = (end[0], end[1] + 1)  # include the line break
            elif start > end:
                return
            select(event, lines, start, end, False)

        return pending, selecting

    for inner in (False, True):
        for key in PAIRS:
            register(("ai"[inner], key), *block_object(key, inner))

    def quote_object(quote: str, inner: bool):
        def pending(event: KeyPressEvent) -> TextObject | None:
            lines, cur, pos, _ = where(event)
            row, col = pos
            span = quote_span(lines[row], col, quote, not inner)
            if span is None:
                return None
            return _charwise(lines, cur, (row, span[0]), (row, span[1]), False)

        def selecting(event: KeyPressEvent) -> None:
            lines, _cur, pos, vstart = where(event)
            row, col = pos
            span = quote_span(lines[row], col, quote, not inner)
            if span is None:
                return
            start, end = span
            if inner and vstart is not None and vstart != pos:
                # Repeating i" on the selected inner text takes the quotes too.
                lo, hi = sorted((vstart[1], col))
                if (lo, hi + 1) == (start, end) and start > 0:
                    start, end = start - 1, end + 1
            if end > start:
                select(event, lines, (row, start), (row, end - 1), False)

        return pending, selecting

    for inner in (False, True):
        for quote in ('"', "'", "`"):
            register(("ai"[inner], quote), *quote_object(quote, inner))


def _add_join(kb: KeyBindings) -> None:
    """``J`` / ``gJ``: ``{count}J`` joins *count* lines (minimum two)."""

    def join(buff: Buffer, row: int, count: int, space: bool) -> None:
        doc = buff.document
        if row >= doc.line_count - 1:
            return
        lines, col = join_lines(doc.lines, row, max(count, 2), space)
        buff.document = Document("\n".join(lines), 0)
        _set_cursor(buff, row, col)

    def join_selected(event: KeyPressEvent, space: bool) -> None:
        buff = event.current_buffer
        doc = buff.document
        sel = buff.selection_state
        if sel is None:
            return
        other = doc.translate_index_to_position(sel.original_cursor_position)[0]
        first, last = sorted((doc.cursor_position_row, other))
        buff.exit_selection()
        join(buff, first, last - first + 1, space)

    @kb.add("J", filter=vi_navigation_mode & ~is_read_only)
    def _join(event: KeyPressEvent) -> None:
        join(
            event.current_buffer,
            event.current_buffer.document.cursor_position_row,
            event.arg,
            True,
        )

    @kb.add("g", "J", filter=vi_navigation_mode & ~is_read_only)
    def _join_nospace(event: KeyPressEvent) -> None:
        join(
            event.current_buffer,
            event.current_buffer.document.cursor_position_row,
            event.arg,
            False,
        )

    @kb.add("J", filter=vi_selection_mode & ~is_read_only)
    def _join_selection(event: KeyPressEvent) -> None:
        join_selected(event, True)

    @kb.add("g", "J", filter=vi_selection_mode & ~is_read_only)
    def _join_selection_nospace(event: KeyPressEvent) -> None:
        join_selected(event, False)


def _add_insert_tab(kb: KeyBindings, engine: ViEngine) -> None:
    """Insert-mode Tab inserts one ``indent_unit`` (prompt_toolkit: a bare tab)."""

    @kb.add("c-i", filter=vi_insert_mode & ~is_read_only)
    def _tab(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text(engine.indent_unit * event.arg)


def _add_viewport_commands(kb: KeyBindings, engine: ViEngine) -> None:
    """``H M L``, ``^D ^U ^F ^B ^Y``, ``PageUp/PageDown`` and the ``z`` family.

    prompt_toolkit's own versions need a rendered window; the engine is
    headless, so these work from ``engine.viewport`` instead.  Without a
    viewport they do nothing.  Numbers follow vim: ``^D``/``^U`` scroll
    half the window (a count sets and keeps that amount, vim's 'scroll'),
    ``^F``/``^B`` a page minus two lines, ``zz`` puts the cursor row at
    ``(height - 1) // 2``.  ``^D`` ``^U`` ``^F`` ``^B`` ``^Y`` and the
    ``zt zz zb`` family keep the cursor column (nvim ``nostartofline``);
    ``H M L`` and ``z<CR> z. z-`` go to the first non-blank (documented vim).
    """
    text_object = create_text_object_decorator(kb)
    scroll_mode = vi_navigation_mode | vi_selection_mode
    # vim's 'scroll' option: None means "half the window".
    scroll_option: list[int | None] = [None]

    def geometry(buff: Buffer) -> tuple[int, int, int] | None:
        """``(top, height, last_row)`` of the window, or None without one."""
        vp = engine.viewport
        if vp is None:
            return None
        top, bottom = vp.visible_rows()
        return top, max(bottom - top + 1, 1), buff.document.line_count - 1

    def first_nonblank(doc: Document, row: int) -> int:
        line = doc.lines[row]
        return len(line) - len(line.lstrip())

    def put_cursor(buff: Buffer, row: int, col: int) -> None:
        doc = buff.document
        row = max(0, min(row, doc.line_count - 1))
        limit = len(doc.lines[row])
        if get_app().vi_state.input_mode != InputMode.INSERT:
            limit -= 1
        buff.cursor_position = doc.translate_row_col_to_index(
            row, max(0, min(col, limit))
        )

    def screen_row(event: KeyPressEvent, which: str) -> TextObject | None:
        buff = event.current_buffer
        geo = geometry(buff)
        if geo is None:
            return None
        top, height, last = geo
        shown = min(top + height - 1, last) - top + 1  # rows with text
        if which == "H":
            offset = event.arg - 1
        elif which == "L":
            offset = shown - event.arg
        else:
            offset = (shown + 1) // 2 - 1
        doc = buff.document
        row = max(0, min(top + max(0, min(offset, shown - 1)), last))
        col = min(doc.cursor_position_col, len(doc.lines[row]))
        target = doc.translate_row_col_to_index(row, col)
        return TextObject(target - doc.cursor_position, type=TextObjectType.LINEWISE)

    for name in "HML":

        @text_object(name)
        def _(event: KeyPressEvent, name: str = name) -> TextObject | None:
            return screen_row(event, name)

    def scroll_half(event: KeyPressEvent, down: bool) -> None:
        buff = event.current_buffer
        geo = geometry(buff)
        if geo is None:
            return
        top, height, last = geo
        if event._arg:
            scroll_option[0] = event.arg
        amount = scroll_option[0] or max(height // 2, 1)
        doc = buff.document
        row, col = doc.cursor_position_row, doc.cursor_position_col
        if down:
            new_top = min(top + amount, max(top, last - height + 1))
            new_row = min(row + amount, last)
        else:
            new_top = max(top - amount, 0)
            new_row = max(row - amount, 0)
        if new_top != top:
            engine.viewport.scroll_to_top_row(new_top)  # type: ignore[union-attr]
        put_cursor(buff, new_row, col)

    def scroll_page(event: KeyPressEvent, down: bool) -> None:
        buff = event.current_buffer
        geo = geometry(buff)
        if geo is None:
            return
        top, height, last = geo
        step = max(height - 2, 1) * event.arg
        new_top = min(top + step, last) if down else max(top - step, 0)
        if new_top != top:
            engine.viewport.scroll_to_top_row(new_top)  # type: ignore[union-attr]
        doc = buff.document
        row, col = doc.cursor_position_row, doc.cursor_position_col
        if down:
            row = max(row, new_top)
        else:
            row = min(row, new_top + height - 1)
        put_cursor(buff, row, col)

    @kb.add("c-d", filter=scroll_mode)
    def _ctrl_d(event: KeyPressEvent) -> None:
        scroll_half(event, True)

    @kb.add("c-u", filter=scroll_mode)
    def _ctrl_u(event: KeyPressEvent) -> None:
        scroll_half(event, False)

    page_mode = scroll_mode | vi_insert_mode

    @kb.add("c-f", filter=scroll_mode)
    @kb.add("pagedown", filter=page_mode)
    def _page_down(event: KeyPressEvent) -> None:
        scroll_page(event, True)

    @kb.add("c-b", filter=scroll_mode)
    @kb.add("pageup", filter=page_mode)
    def _page_up(event: KeyPressEvent) -> None:
        scroll_page(event, False)

    @kb.add("c-y", filter=scroll_mode)
    def _ctrl_y(event: KeyPressEvent) -> None:
        buff = event.current_buffer
        geo = geometry(buff)
        if geo is None:
            return
        top, height, _ = geo
        new_top = max(top - event.arg, 0)
        if new_top != top:
            engine.viewport.scroll_to_top_row(new_top)  # type: ignore[union-attr]
        doc = buff.document
        row, col = doc.cursor_position_row, doc.cursor_position_col
        put_cursor(buff, min(row, new_top + height - 1), col)

    def place(event: KeyPressEvent, where: str, bol: bool) -> None:
        """``zt``/``zz``/``zb`` (and ``z<CR>`` ``z.`` ``z-``) for the cursor row."""
        buff = event.current_buffer
        geo = geometry(buff)
        if geo is None:
            return
        _, height, last = geo
        doc = buff.document
        row, col = doc.cursor_position_row, doc.cursor_position_col
        if event._arg:  # {count}zt: that line becomes the target
            row = max(0, min(event.arg - 1, last))
        if where == "t":
            new_top = row
        elif where == "z":
            new_top = row - (height - 1) // 2
        else:
            new_top = row - (height - 1)
        engine.viewport.scroll_to_top_row(max(new_top, 0))  # type: ignore[union-attr]
        put_cursor(buff, row, first_nonblank(doc, row) if bol else col)

    for keys, where, bol in (
        (("z", "t"), "t", False),
        (("z", "enter"), "t", True),
        (("z", "z"), "z", False),
        (("z", "."), "z", True),
        (("z", "b"), "b", False),
        (("z", "-"), "b", True),
    ):

        @kb.add(*keys, filter=scroll_mode)
        def _(event: KeyPressEvent, where=where, bol=bol) -> None:
            place(event, where, bol)
