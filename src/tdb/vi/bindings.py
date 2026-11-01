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

import re
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
from prompt_toolkit.filters.app import vi_replace_single_mode
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.vi import (
    TextObject,
    TextObjectType,
    create_operator_decorator,
    create_text_object_decorator,
)
from prompt_toolkit.key_binding.vi_state import CharacterFind, InputMode
from prompt_toolkit.keys import Keys
from prompt_toolkit.selection import PasteMode, SelectionState, SelectionType
from prompt_toolkit.utils import get_cwidth

from tdb.vi.marks import NAMES as MARK_NAMES
from tdb.vi.search import ViPatternError, compile_pattern, find_nth, place, word_pattern
from tdb.vi.textobjects import (
    PAIRS,
    Cursor,
    NoObject,
    Pos,
    back_word,
    back_word_end,
    block_span,
    current_par,
    current_word,
    increment_number,
    index,
    join_lines,
    quote_span,
)

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPressEvent

    from tdb.vi.engine import ViEngine

# Internal key the engine feeds when a '/' or '?' line is accepted: the
# search as a motion (a KeyPress no ViKey can produce; F-keys never reach
# the engine).
SEARCH_KEY = Keys.F24


def load_tdb_vi_bindings(engine: ViEngine) -> KeyBindings:
    """Key bindings that fix or extend prompt_toolkit's vi mode for *engine*."""
    kb = KeyBindings()

    @kb.add("c-r", filter=vi_navigation_mode, save_before=lambda e: False)
    def _redo(event: KeyPressEvent) -> None:
        """Redo (pyvim); fails when there is nothing (more) to redo."""
        buff = event.app.current_buffer
        for _ in range(event.arg):
            if not buff._redo_stack:
                engine.fail()
                break
            buff.redo()

    @kb.add("c-v", filter=vi_navigation_mode | vi_selection_mode)
    def _no_block_visual(event: KeyPressEvent) -> None:
        """Block visual mode is unsupported: Ctrl+V does nothing."""

    @kb.add("c-z")
    def _no_ctrl_z(event: KeyPressEvent) -> None:
        """Ctrl+Z does nothing (prompt_toolkit would insert a literal ``\\x1a``)."""

    _add_change_word(kb, engine)
    _add_nvim_cursor_fixes(kb, engine)
    _add_backward_words(kb, engine)
    _add_text_objects(kb, engine)
    _add_join(kb, engine)
    _add_viewport_commands(kb, engine)
    _add_insert_tab(kb, engine)
    _add_visual_commands(kb, engine)
    _add_increment(kb, engine)
    _add_counted_commands(kb, engine)
    _add_marks(kb, engine)
    _add_search(kb, engine)
    _add_substitute_repeat(kb, engine)
    _add_unknown_keys(kb, engine)

    return kb


def _text_objects(kb: KeyBindings, engine: ViEngine):
    """``create_text_object_decorator`` whose objects report ``None`` (no
    object or no match, which cancels the operator) as a failed command.

    The decorated function is returned wrapped, so a tdb move handler that
    calls it reports the failure too.
    """
    register = create_text_object_decorator(kb)

    def text_object(*keys, **kwargs):
        def decorator(func):
            def target(event: KeyPressEvent) -> TextObject | None:
                obj = func(event)
                if obj is None:
                    engine.fail()
                return obj

            register(*keys, **kwargs)(target)
            return target

        return decorator

    return text_object


def _operators(kb: KeyBindings):
    """``create_operator_decorator`` whose operators get vim's region for
    a charwise motion (every tdb operator is registered through it).

    prompt_toolkit's ``TextObject.operator_range`` moves an exclusive end
    at column 0 back a character whatever the start, and ``cut`` then
    takes a character before the cursor for an empty region: ``d0`` at
    column 0 joined the line to the one above, at the start of the buffer
    it duplicated text.  See ``_vim_region``.  The decorated function is
    returned wrapped (``vi_state.operator_func`` is the wrapper).
    """
    register = create_operator_decorator(kb)

    def operator(*keys, **kwargs):
        def decorator(func):
            def run(event: KeyPressEvent, text_obj: TextObject) -> None:
                func(event, _vim_region(event.current_buffer, text_obj))

            register(*keys, **kwargs)(run)
            return run

        return decorator

    return operator


def _vim_region(buff: Buffer, obj: TextObject) -> TextObject:
    """*obj* as vim operates on it, where prompt_toolkit would not.

    A charwise object whose region is empty in prompt_toolkit (an
    exclusive one, or an inclusive one of just a line end, as on an empty
    line) goes through ``_charwise``: empty in vim too (``d0`` at
    column 0, ``dh``, ``d^`` on the first non-blank, ``dTx`` next to the
    ``x``, ``d1|`` onto the cursor) it becomes an ``_EmptyTextObject``,
    which no operator changes text for (``c`` still inserts, ``<`` and
    ``>`` still shift the line, as in vim); ending at column 0 of a later
    line (``dw`` on an empty line) it gets vim's exclusive rule.  Visual
    selections and linewise objects are left alone.
    """
    if (
        buff.selection_state is not None
        or obj.type not in (TextObjectType.EXCLUSIVE, TextObjectType.INCLUSIVE)
        or isinstance(obj, _EmptyTextObject)
    ):
        return obj
    doc = buff.document
    cur = doc.cursor_position
    start, end = obj.sorted()
    inclusive = obj.type == TextObjectType.INCLUSIVE
    last = doc.translate_index_to_position(cur + end)
    if inclusive:
        # (tdb's own inclusive objects can end on a line end: the break)
        if start != end or last[1] < len(doc.lines[last[0]]):
            return obj
    else:
        first, upto = obj.operator_range(doc)
        if first < upto:
            return obj
    first_pos = doc.translate_index_to_position(cur + start)
    return _charwise(doc.lines, cur, first_pos, last, inclusive)


def _add_unknown_keys(kb: KeyBindings, engine: ViEngine) -> None:
    """Unknown or unsupported keys: cancel the command, notify, fail.

    Without these prompt_toolkit drops the first key of an unmatched
    sequence and runs the rest as a new command (``g8x`` deleted 8
    characters, ``=ip`` entered insert mode).  The catch-alls hold
    ``Keys.Any``, so every specific binding for the same keys wins (see
    ``KeyBindings.get_bindings_for_keys``).  Unsupported operators take
    their motion and change nothing; ``!{motion}`` opens the ``:`` line
    with the range, as vim does, where ``!`` is refused.
    """
    operator = _operators(kb)
    pending = vi_waiting_for_text_object_mode
    visual = vi_selection_mode & ~pending
    command = vi_navigation_mode | visual

    def unknown(event: KeyPressEvent) -> None:
        vs = event.app.vi_state
        vs.operator_func = None
        vs.operator_arg = None
        engine.unsupported(engine.command_notation())

    for keys, active in (
        ((Keys.Any,), command | pending),
        (("g", Keys.Any), command | pending),
        (("i", Keys.Any), visual | pending),
        (("a", Keys.Any), visual | pending),
        (("z", Keys.Any), command),
        (("Z", Keys.Any), command),
        (("[", Keys.Any), command),
        (("]", Keys.Any), command),
        (("c-w", Keys.Any), command),
    ):
        kb.add(*keys, filter=active)(unknown)

    for keys in (("=",), ("g", "?"), ("g", "q"), ("g", "w"), ("g", "@"), ("z", "f")):

        @operator(*keys, filter=~is_read_only)
        def _(event: KeyPressEvent, text_obj: TextObject, keys=keys) -> None:
            engine.unsupported("".join(keys))

    @operator("!", filter=~is_read_only)
    def _filter(event: KeyPressEvent, text_obj: TextObject) -> None:
        first, last = text_obj.get_line_numbers(event.current_buffer)
        engine.open_filter_line(first, last)

    # Supported only as their plain forms (documented).
    @kb.add("g", "i", filter=vi_navigation_mode & ~is_read_only)
    def _gi(event: KeyPressEvent) -> None:
        event.app.vi_state.input_mode = InputMode.INSERT

    @kb.add("g", "I", filter=vi_navigation_mode & ~is_read_only)
    def _gI(event: KeyPressEvent) -> None:
        buff = event.current_buffer
        buff.cursor_position += buff.document.get_start_of_line_position()
        event.app.vi_state.input_mode = InputMode.INSERT


def _add_substitute_repeat(kb: KeyBindings, engine: ViEngine) -> None:
    """``&``: the last ``:s`` on the current line (a count: that many
    lines), without its flags; ``g&``: it on every line, with its flags
    and the last search pattern (vim's ``:%s//~/&``)."""

    @kb.add("&", filter=vi_navigation_mode)
    def _repeat(event: KeyPressEvent) -> None:
        engine.substitute_again(
            event.arg if event.arg_present else None, every_line=False
        )

    @kb.add("g", "&", filter=vi_navigation_mode)
    def _repeat_all(event: KeyPressEvent) -> None:
        engine.substitute_again(None, every_line=True)


def _add_change_word(kb: KeyBindings, engine: ViEngine) -> None:
    """vim's ``cw``/``cW`` special case, missing from prompt_toolkit.

    With the cursor on a non-blank, ``cw`` changes to the end of the word
    (like ``ce``) instead of also eating the following white space.  This
    needs a tdb-owned ``c`` operator so the ``w``/``W`` text objects can
    tell a change apart from ``d``/``y``.
    """
    operator = _operators(kb)
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
        if _count_past_end(event, engine):
            return
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


def _count_past_end(event: KeyPressEvent, engine: ViEngine) -> bool:
    """vim's rule for ``{count}dd`` and the other doubled operators: a
    count on the last line fails (elsewhere it is clamped to the end)."""
    doc = event.current_buffer.document
    if event.arg > 1 and doc.cursor_position_row >= doc.line_count - 1:
        engine.fail()
        return True
    return False


def _record_rows(buff: Buffer, first: int, deleted: int, inserted: int) -> None:
    """Tell the engine which lines a linewise change removed or added
    (``:g`` follows its marked lines by it)."""
    record = getattr(buff, "record_rows", None)
    if record is not None:
        record(first, deleted, inserted)


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
    rest = lines[:first] + keep + lines[last + 1 :]
    gone = len(lines) - len(rest)
    if keep:
        _record_rows(buff, first + 1, gone, 0)  # the first line is changed
    elif not rest:
        _record_rows(buff, 0, gone, 1)  # an empty line is left
    else:
        _record_rows(buff, first, gone, 0)
    buff.document = Document("\n".join(rest), 0)
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
    operator = _operators(kb)

    @kb.add("d", "d", filter=vi_navigation_mode & ~is_read_only)
    def _delete_line(event: KeyPressEvent) -> None:
        """``dd``: linewise delete, cursor keeps its column."""
        if _count_past_end(event, engine):
            return
        doc = event.current_buffer.document
        row = doc.cursor_position_row
        _delete_rows(event, row, row + event.arg - 1, doc.cursor_position_col)

    def paste(event: KeyPressEvent, mode: PasteMode) -> None:
        buff = event.current_buffer
        data = event.app.clipboard.get_data()
        if not data.text and data.type != SelectionType.LINES:
            engine.fail()  # empty register: nothing to put (vim's E353)
            return
        row = buff.document.cursor_position_row
        at = buff.cursor_position
        if mode == PasteMode.VI_AFTER and buff.document.current_line:
            at += 1
        count = buff.document.line_count
        buff.paste_clipboard_data(data, count=event.arg, paste_mode=mode)
        if data.type == SelectionType.LINES:
            first = row + 1 if mode == PasteMode.VI_AFTER else row
            _record_rows(buff, first, 0, buff.document.line_count - count)
        if data.type == SelectionType.CHARACTERS and "\n" in data.text:
            buff.cursor_position = at  # a multi-line put leaves the cursor at its start
        if data.type == SelectionType.LINES and data.text:
            first = row + 1 if mode == PasteMode.VI_AFTER else row
            line = buff.document.lines[first]
            _set_cursor(buff, first, len(line) - len(line.lstrip()))

    @kb.add("p", filter=vi_navigation_mode)
    @kb.add("g", "p", filter=vi_navigation_mode)  # documented: as p
    def _paste(event: KeyPressEvent) -> None:
        paste(event, PasteMode.VI_AFTER)

    @kb.add("P", filter=vi_navigation_mode)
    @kb.add("g", "P", filter=vi_navigation_mode)  # documented: as P
    def _paste_before(event: KeyPressEvent) -> None:
        paste(event, PasteMode.VI_BEFORE)

    @kb.add("y", "y", filter=vi_navigation_mode)
    @kb.add("Y", filter=vi_navigation_mode)
    def _yank_line(event: KeyPressEvent) -> None:
        """``{count}yy`` / ``{count}Y``: a yank, so it also sets ``"0``."""
        if _count_past_end(event, engine):
            return
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
            engine.fail()  # an empty line
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
        if _count_past_end(event, engine):
            return
        row = event.current_buffer.document.cursor_position_row
        shift(event, (row, row + event.arg), False)

    @kb.add("<", "<", filter=vi_navigation_mode & ~is_read_only)
    def _unindent(event: KeyPressEvent) -> None:
        if _count_past_end(event, engine):
            return
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
        """``y{motion}``: the cursor moves to the start of the yanked text.

        An empty region (``y0`` at column 0, ``yi(`` on ``()``) yanks
        empty text, as vim does."""
        buff = event.current_buffer
        _, data = text_obj.cut(buff)
        if isinstance(text_obj, _EmptyTextObject):
            event.app.clipboard.yank(data)  # type: ignore[attr-defined]
            buff.cursor_position += text_obj.start
            return
        if data.text or data.type == SelectionType.LINES:
            event.app.clipboard.yank(data)  # type: ignore[attr-defined]
        sel = buff.selection_state
        down = sel is not None and sel.original_cursor_position <= buff.cursor_position
        buff.cursor_position += min(text_obj.sorted()[0], 0)
        if down and sel.type == SelectionType.LINES:  # type: ignore[union-attr]
            _set_cursor(buff, buff.document.cursor_position_row, 0)  # `Vjy`

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
            if _count_past_end(event, engine):
                return
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

    text_object = _text_objects(kb, engine)

    def has_count(event: KeyPressEvent) -> bool:
        """Whether a count was typed.  Under an operator prompt_toolkit always
        sets ``_arg`` (``dG`` carries ``"1"``, as does ``d1G``), so ask the
        engine, which saw the keys."""
        if event.app.vi_state.operator_func is not None:
            return engine.count_typed()
        return bool(event._arg)

    def goto_row(event: KeyPressEvent, row: int) -> TextObject:
        """``G``/``gg`` target: *row*, same column (nvim ``nostartofline``)."""
        doc = event.current_buffer.document
        row = max(0, min(row, doc.line_count - 1))
        col = min(doc.cursor_position_col, max(len(doc.lines[row]) - 1, 0))
        target = doc.translate_row_col_to_index(row, col)
        return TextObject(target - doc.cursor_position, type=TextObjectType.LINEWISE)

    @text_object("|")
    def _to_screen_column(event: KeyPressEvent) -> TextObject:
        """``N|``: screen column N, as vim (a tab reaches to the next tab
        stop, a wide character takes two columns); past the end of the line
        the last character.  Exclusive, also for an operator (``d99|``
        keeps the last character); onto the cursor's own character (any N
        inside a tab's span) the region is empty (``_vim_region``)."""
        doc = event.current_buffer.document
        return TextObject(
            _screen_column_index(doc.current_line, event.arg - 1, engine.tabstop)
            - doc.cursor_position_col
        )

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

    @kb.add("j", filter=vi_navigation_mode | vi_selection_mode)
    @kb.add("g", "j", filter=vi_navigation_mode | vi_selection_mode)  # no wrap
    def _move_down(event: KeyPressEvent) -> None:
        """``j``: fails on the last line (prompt_toolkit goes to column 0)."""
        buff = event.current_buffer
        if buff.document.cursor_position_row < buff.document.line_count - 1:
            buff.cursor_down(count=event.arg)
        else:
            engine.fail()

    @kb.add("k", filter=vi_navigation_mode | vi_selection_mode)
    @kb.add("g", "k", filter=vi_navigation_mode | vi_selection_mode)
    def _move_up(event: KeyPressEvent) -> None:
        """``k``: fails on the first line."""
        buff = event.current_buffer
        if buff.document.cursor_position_row > 0:
            buff.cursor_up(count=event.arg)
        else:
            engine.fail()

    @kb.add("u", filter=vi_navigation_mode, save_before=lambda e: False)
    def _undo(event: KeyPressEvent) -> None:
        """``{count}u``; fails when there is nothing (more) to undo."""
        for _ in range(event.arg):
            if not event.current_buffer.undo():
                engine.fail()
                break

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


def _add_backward_words(kb: KeyBindings, engine: ViEngine) -> None:
    """vim's ``b``/``B``/``ge``/``gE`` and ``g_``.

    prompt_toolkit's ``b`` stays put when fewer than *count* words are
    left (vim stops at the start of the buffer), and at the start of the
    buffer neither ``b`` nor ``ge`` fails; its ``ge`` then takes the
    cursor's character.  Its ``g_`` on an empty line ends on the line
    break above.  Under an operator a failed motion cancels it.
    """
    text_object = _text_objects(kb, engine)
    movable = (
        vi_navigation_mode | vi_selection_mode
    ) & ~vi_waiting_for_text_object_mode

    def backward(keys: tuple[str, ...], find, bigword: bool, inclusive: bool) -> None:
        def go(event: KeyPressEvent) -> tuple[Pos, Pos, bool]:
            """(cursor, target, ok); the cursor is moved to the target."""
            buff = event.current_buffer
            doc = buff.document
            pos = (doc.cursor_position_row, doc.cursor_position_col)
            to, ok = find(doc.lines, pos, event.arg, bigword)
            buff.cursor_position = doc.translate_row_col_to_index(*to)
            return pos, to, ok

        @text_object(*keys, no_move_handler=True, no_selection_handler=True)
        def target(event: KeyPressEvent) -> TextObject | None:
            buff = event.current_buffer
            doc = buff.document
            pos, to, ok = go(event)
            if not ok:
                return None  # the operator is cancelled; the cursor moved
            buff.cursor_position = doc.cursor_position
            return _charwise(doc.lines, doc.cursor_position, to, pos, inclusive)

        @kb.add(*keys, filter=movable)
        def _move(event: KeyPressEvent) -> None:
            if not go(event)[2]:
                engine.fail()  # (``5ge`` can fail after moving)

    backward(("b",), back_word, False, False)
    backward(("B",), back_word, True, False)
    backward(("g", "e"), back_word_end, False, True)
    backward(("g", "E"), back_word_end, True, True)

    def last_nonblank(event: KeyPressEvent) -> Pos | None:
        """``[count]g_``: the last non-blank *count* - 1 lines down (column 0
        on a blank line); a count on the last line fails."""
        doc = event.current_buffer.document
        lines = doc.lines
        row = doc.cursor_position_row
        if event.arg > 1 and row >= len(lines) - 1:
            return None
        to = min(row + event.arg - 1, len(lines) - 1)
        return to, max(len(lines[to].rstrip(" \t")) - 1, 0)

    @text_object("g", "_", no_move_handler=True, no_selection_handler=True)
    def _last_nonblank(event: KeyPressEvent) -> TextObject | None:
        to = last_nonblank(event)
        if to is None:
            return None
        doc = event.current_buffer.document
        pos = (doc.cursor_position_row, doc.cursor_position_col)
        start, end = sorted((pos, to))  # (back, from trailing white space)
        return _charwise(doc.lines, doc.cursor_position, start, end, True)

    @kb.add("g", "_", filter=movable)
    def _move_last_nonblank(event: KeyPressEvent) -> None:
        to = last_nonblank(event)
        if to is None:
            engine.fail()
            return
        doc = event.current_buffer.document
        event.current_buffer.cursor_position = doc.translate_row_col_to_index(*to)


def _screen_column_index(line: str, column: int, tabstop: int) -> int:
    """The index of the character of *line* drawn at screen *column*
    (0-based), or of the last one when the line is shorter."""
    x = 0
    for i, ch in enumerate(line):
        x += tabstop - x % tabstop if ch == "\t" else max(get_cwidth(ch), 0)
        if x > column:
            return i
    return max(len(line) - 1, 0)


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


def _add_text_objects(kb: KeyBindings, engine: ViEngine) -> None:
    """vim-accurate ``iw aw iW aW ip ap`` and bracket / quote objects.

    Each object is registered twice: through ``create_text_object_decorator``
    for operator-pending mode (overriding prompt_toolkit's for the same keys,
    and a ``None`` result cancels the operator), and as a Visual-mode
    selection.  The decorator's own selection handler is disabled because it
    selects one character too many.  No object is a failed command, in
    both modes.
    """
    text_object = _text_objects(kb, engine)
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
                engine.fail()
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
            if found is None:
                engine.fail()
            else:
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
                engine.fail()
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
                engine.fail()
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


def _add_join(kb: KeyBindings, engine: ViEngine) -> None:
    """``J`` / ``gJ``: ``{count}J`` joins *count* lines (minimum two)."""

    def join(buff: Buffer, row: int, count: int, space: bool) -> None:
        doc = buff.document
        if row >= doc.line_count - 1:
            engine.fail()  # no line below
            return
        lines, col = join_lines(doc.lines, row, max(count, 2), space)
        _record_rows(buff, row + 1, doc.line_count - len(lines), 0)
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
    text_object = _text_objects(kb, engine)
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


def _add_visual_commands(kb: KeyBindings, engine: ViEngine) -> None:
    """Visual mode: ``o O``, ``gv``, ``u U ~`` and ``r{char}``."""
    visual = vi_selection_mode & ~vi_waiting_for_text_object_mode

    @kb.add("o", filter=visual)
    @kb.add("O", filter=visual)
    def _other_end(event: KeyPressEvent) -> None:
        """Move the cursor to the other end of the selection."""
        buff = event.current_buffer
        sel = buff.selection_state
        if sel is not None:
            sel.original_cursor_position, buff.cursor_position = (
                buff.cursor_position,
                sel.original_cursor_position,
            )

    @kb.add("g", "v", filter=vi_navigation_mode | visual)
    def _reselect(event: KeyPressEvent) -> None:
        """``gv``: select the previous Visual area (in Visual: swap with it)."""
        raw = engine.last_visual_raw
        if raw is None:
            engine.fail()
            return
        buff = event.current_buffer
        current = engine._visual_state()
        lines = buff.document.lines

        def clamp(pos: tuple[int, int]) -> int:
            row = max(0, min(pos[0], len(lines) - 1))
            return index(lines, (row, max(0, min(pos[1], len(lines[row])))))

        anchor, cursor, linewise = raw
        kind = SelectionType.LINES if linewise else SelectionType.CHARACTERS
        buff.selection_state = SelectionState(clamp(anchor), kind)
        buff.cursor_position = clamp(cursor)
        if current is not None:
            engine.last_visual_raw = current

    def selected_span(buff: Buffer) -> tuple[int, int, int] | None:
        """``(start, end, cursor index)`` of the selected text, end exclusive.

        The cursor index is where vim leaves the cursor afterwards: the first
        selected character, or for a linewise area started at its first line
        the start of that line.
        """
        sel = buff.selection_state
        if sel is None:
            return None
        doc = buff.document
        first, last = sorted((buff.cursor_position, sel.original_cursor_position))
        if sel.type == SelectionType.LINES:
            row1 = doc.translate_index_to_position(first)[0]
            row2 = doc.translate_index_to_position(last)[0]
            start = doc.translate_row_col_to_index(row1, 0)
            end = doc.translate_row_col_to_index(row2, len(doc.lines[row2]))
        else:
            start, end = first, min(last + 1, len(doc.text))
        if sel.type == SelectionType.LINES and sel.original_cursor_position <= (
            buff.cursor_position
        ):
            first = start
        return start, end, first

    def finish(buff: Buffer, first: int) -> None:
        """Leave Visual mode with the cursor at *first*."""
        buff.exit_selection()
        _set_cursor(buff, *buff.document.translate_index_to_position(first))

    def change_case(keys: tuple[str, ...], func) -> None:
        @kb.add(*keys, filter=visual & ~is_read_only)
        def _(event: KeyPressEvent) -> None:
            buff = event.current_buffer
            span = selected_span(buff)
            if span is None:
                return
            start, end, first = span
            if start < end:
                buff.transform_region(start, end, func)
            finish(buff, first)

    change_case(("U",), str.upper)
    change_case(("u",), str.lower)
    change_case(("~",), str.swapcase)

    @kb.add("r", Keys.Any, filter=visual & ~is_read_only)
    def _replace_selected(event: KeyPressEvent) -> None:
        """``r{char}``: every selected character becomes *char*."""
        char = event.data
        if len(char) != 1 or not char.isprintable():
            return  # Esc and other keys cancel the r
        buff = event.current_buffer
        span = selected_span(buff)
        if span is None:
            return
        start, end, first = span
        if start < end:
            buff.transform_region(
                start,
                end,
                lambda text: "".join(c if c == "\n" else char for c in text),
            )
        finish(buff, first)


def _add_increment(kb: KeyBindings, engine: ViEngine) -> None:
    """``{count}Ctrl+A`` / ``{count}Ctrl+X``: add to the number at or after the cursor."""

    def bump(event: KeyPressEvent, sign: int) -> None:
        buff = event.current_buffer
        doc = buff.document
        found = increment_number(
            doc.current_line, doc.cursor_position_col, sign * event.arg
        )
        if found is None:
            engine.fail()  # no number at or after the cursor
            return
        line, col = found
        lines = list(doc.lines)
        lines[doc.cursor_position_row] = line
        buff.document = Document(
            "\n".join(lines), index(lines, (doc.cursor_position_row, col))
        )

    @kb.add("c-a", filter=vi_navigation_mode & ~is_read_only)
    def _increment(event: KeyPressEvent) -> None:
        bump(event, 1)

    @kb.add("c-x", filter=vi_navigation_mode & ~is_read_only)
    def _decrement(event: KeyPressEvent) -> None:
        bump(event, -1)


def _add_counted_commands(kb: KeyBindings, engine: ViEngine) -> None:
    """``{count}r`` and ``{count}f/t/F/T`` (``;`` ``,``), which fail like vim's.

    prompt_toolkit ignores the count of ``r`` and, when a ``f``/``t`` has too
    few matches, still lets an operator run.  Here a command that cannot be
    done leaves the text and the cursor alone, cancels a pending operator
    and fails.
    """
    text_object = _text_objects(kb, engine)
    count_of_r = [1]

    @kb.add("r", filter=vi_navigation_mode)
    def _replace(event: KeyPressEvent) -> None:
        count_of_r[0] = event.arg
        event.app.vi_state.input_mode = InputMode.REPLACE_SINGLE

    @kb.add(Keys.Any, filter=vi_replace_single_mode)
    @kb.add("enter", filter=vi_replace_single_mode)
    def _replace_chars(event: KeyPressEvent) -> None:
        """The character after ``r``: replace *count* characters with it."""
        count, count_of_r[0] = count_of_r[0], 1
        event.app.vi_state.input_mode = InputMode.NAVIGATION
        char = event.data
        buff = event.current_buffer
        doc = buff.document
        row, col = doc.cursor_position_row, doc.cursor_position_col
        line = doc.current_line
        if col + count > len(line):
            engine.fail()  # not that many characters
            return
        if char == "\r":
            # One line break replaces the characters; the new line keeps the indent.
            indent = line[: len(line) - len(line.lstrip(" \t"))]
            rest = line[col + count :].lstrip(" \t")
            lines = list(doc.lines)
            lines[row : row + 1] = [line[:col], indent + rest if rest else ""]
            # vim leaves the cursor one left of the indent (it Esc's out of
            # insert); an empty new line loses its autoindent.
            new_col = max(len(indent) - 1, 0) if rest else 0
            buff.document = Document("\n".join(lines), index(lines, (row + 1, new_col)))
        elif len(char) == 1 and char.isprintable():
            lines = list(doc.lines)
            lines[row] = line[:col] + char * count + line[col + count :]
            buff.document = Document(
                "\n".join(lines), index(lines, (row, col + count - 1))
            )

    def find(
        event: KeyPressEvent,
        char: str,
        backwards: bool,
        till: bool,
        repeating: bool = False,
    ):
        """Offset and type of the *count*-th *char* on the line, or None.

        A repeated till (``;`` / ``,``) with count 1 skips a match right next
        to the cursor (nvim without ``cpo-;``).
        """
        doc = event.current_buffer.document
        count = event.arg
        search = doc.find_backwards if backwards else doc.find
        found = search(char, in_current_line=True, count=count)
        if found and repeating and till and count == 1 and abs(found) == 1:
            found = search(char, in_current_line=True, count=2)
        if not found:
            return None
        if backwards:
            return TextObject(found + 1 if till else found)
        return TextObject(found - 1 if till else found, type=TextObjectType.INCLUSIVE)

    last_till = [False]

    movable = (
        vi_navigation_mode | vi_selection_mode
    ) & ~vi_waiting_for_text_object_mode

    def register(key: str, backwards: bool, till: bool) -> None:
        def target(event: KeyPressEvent) -> TextObject | None:
            event.app.vi_state.last_character_find = CharacterFind(
                event.data, backwards
            )
            last_till[0] = till
            return find(event, event.data, backwards, till)

        target = text_object(
            key, Keys.Any, no_move_handler=True, no_selection_handler=True
        )(target)

        @kb.add(key, Keys.Any, filter=movable)
        def _move(event: KeyPressEvent) -> None:
            obj = target(event)
            if obj is not None:
                event.current_buffer.cursor_position += obj.start

    for key, backwards, till in (
        ("f", False, False),
        ("F", True, False),
        ("t", False, True),
        ("T", True, True),
    ):
        register(key, backwards, till)

    def repeat(key: str, reverse: bool) -> None:
        def target(event: KeyPressEvent) -> TextObject | None:
            last = event.app.vi_state.last_character_find
            if last is None:
                return None
            backwards = last.backwards != reverse
            return find(event, last.character, backwards, last_till[0], True)

        target = text_object(key, no_move_handler=True, no_selection_handler=True)(
            target
        )

        @kb.add(key, filter=movable)
        def _move(event: KeyPressEvent) -> None:
            obj = target(event)
            if obj is not None:
                event.current_buffer.cursor_position += obj.start

    repeat(";", False)
    repeat(",", True)


def _add_marks(kb: KeyBindings, engine: ViEngine) -> None:
    """``m{a-z}``, and the ``'x`` (linewise) and `` `x `` (exclusive) motions.

    Marks live in ``engine.marks``; a jump clamps the mark to the text
    (vim keeps the stored column).  An unset mark is vim's E20: a warning
    and no motion, which also cancels a pending operator.
    """
    from tdb.vi.engine import Notify

    text_object = _text_objects(kb, engine)
    movable = (
        vi_navigation_mode | vi_selection_mode
    ) & ~vi_waiting_for_text_object_mode

    @kb.add("m", Keys.Any, filter=movable)
    def _set_mark(event: KeyPressEvent) -> None:
        name = event.data
        if len(name) == 1 and "a" <= name <= "z":
            doc = event.current_buffer.document
            engine.marks.set(name, (doc.cursor_position_row, doc.cursor_position_col))
        elif len(name) == 1 and name.isprintable():
            engine._events.append(Notify(f"Mark {name} is not supported", "warning"))
            engine.fail()
        else:
            engine.fail()  # m<Esc>

    def mark(event: KeyPressEvent) -> tuple[int, int] | None:
        """The mark named by the key, clamped to the text, or None."""
        name = event.data
        pos = engine.marks.get(name) if name in (*MARK_NAMES, "`") else None
        if pos is None:
            if len(name) == 1 and name.isprintable():
                engine._events.append(Notify(f"Mark not set: {name}", "warning"))
            return None
        lines = event.current_buffer.document.lines
        row = max(0, min(pos[0], len(lines) - 1))
        return row, max(0, min(pos[1], len(lines[row]) - 1))

    def first_nonblank(lines: list[str], row: int) -> int:
        line = lines[row]
        return len(line) - len(line.lstrip(" \t"))

    def line_target(event: KeyPressEvent) -> TextObject | None:
        pos = mark(event)
        if pos is None:
            return None
        doc = event.current_buffer.document
        lines = doc.lines
        target = index(lines, (pos[0], first_nonblank(lines, pos[0])))
        return TextObject(target - doc.cursor_position, type=TextObjectType.LINEWISE)

    def char_target(event: KeyPressEvent) -> TextObject | None:
        pos = mark(event)
        if pos is None:
            return None
        doc = event.current_buffer.document
        here = (doc.cursor_position_row, doc.cursor_position_col)
        if engine.app.vi_state.operator_func is None:
            return TextObject(index(doc.lines, pos) - doc.cursor_position)
        start, end = sorted((here, pos))
        return _charwise(doc.lines, doc.cursor_position, start, end, False)

    for key, target in (("'", line_target), ("`", char_target)):
        target = text_object(
            key, Keys.Any, no_move_handler=True, no_selection_handler=True
        )(target)

        @kb.add(key, Keys.Any, filter=movable)
        def _jump(event: KeyPressEvent, target=target) -> None:
            obj = target(event)
            if obj is not None:
                event.current_buffer.cursor_position += obj.start


def _add_search(kb: KeyBindings, engine: ViEngine) -> None:
    """``n N * #`` and the accepted ``/`` ``?`` line (``SEARCH_KEY``).

    They replace prompt_toolkit's bindings, which read
    ``app.current_search_state``: patterns are Python ``re`` and the last
    one is ``engine.session.last_search``.  Each is a charwise exclusive
    motion (also for operators and Visual mode) with a count.
    """
    from tdb.vi.engine import Notify

    text_object = _text_objects(kb, engine)
    movable = (
        vi_navigation_mode | vi_selection_mode
    ) & ~vi_waiting_for_text_object_mode

    def notify(text: str, severity: str = "information") -> None:
        engine._events.append(Notify(text, severity))  # type: ignore[arg-type]

    def motion(
        event: KeyPressEvent, pattern: str, backward: bool, offset: int
    ) -> TextObject | None:
        """The motion to the count'th match of *pattern* from *offset*."""
        try:
            rx = compile_pattern(pattern)
        except ViPatternError as exc:
            notify(f"Invalid pattern: {exc}", "error")
            return None
        doc = event.current_buffer.document
        found = find_nth(doc.text, rx, offset, backward, event.arg)
        if found is None:
            notify(f"Pattern not found: {pattern}", "warning")
            return None
        if found[2]:
            notify(
                "search hit TOP, continuing at BOTTOM"
                if backward
                else "search hit BOTTOM, continuing at TOP"
            )
        cur = doc.cursor_position
        if engine.app.vi_state.operator_func is None:
            return TextObject(place(doc.text, found[0]) - cur)
        here = (doc.cursor_position_row, doc.cursor_position_col)
        start, end = sorted((here, doc.translate_index_to_position(found[0])))
        return _charwise(doc.lines, cur, start, end, False)

    def repeat(reverse: bool):
        def target(event: KeyPressEvent) -> TextObject | None:
            last = engine.session.last_search
            if last is None:
                notify("No previous regular expression", "error")
                return None
            pattern, backward = last
            offset = event.current_buffer.cursor_position
            return motion(event, pattern, backward != reverse, offset)

        return target

    def word(backward: bool):
        """``*`` / ``#``: the word under or after the cursor.

        As vim: the keyword under or after the cursor (searched as a whole
        word), else the run of non-blank, non-keyword characters under or
        after it (searched literally, as vim's ``\\.\\.``).
        """

        def first_after(regex: str, line: str, col: int) -> re.Match | None:
            return next((m for m in re.finditer(regex, line) if m.end() > col), None)

        def target(event: KeyPressEvent) -> TextObject | None:
            doc = event.current_buffer.document
            col = doc.cursor_position_col
            line = doc.current_line
            m = first_after(r"\w+", line, col)
            if m is not None:
                pattern = word_pattern(m.group())
            else:
                m = first_after(r"[^\w\s]+", line, col)
                if m is None:
                    notify("No string under cursor", "error")
                    return None
                pattern = re.escape(m.group())
            engine.session.last_search = (pattern, backward)
            engine._remember_search(pattern)
            # From the word's start, so the word itself is skipped.
            offset = doc.cursor_position - col + m.start()
            return motion(event, pattern, backward, offset)

        return target

    @kb.add("/", filter=movable | vi_waiting_for_text_object_mode)
    @kb.add("?", filter=movable | vi_waiting_for_text_object_mode)
    def _open_search(event: KeyPressEvent) -> None:
        """Open the engine's search line; a pending operator waits for it."""
        engine._open_line(event.data, event._arg)

    for key, target in (
        (SEARCH_KEY, repeat(False)),
        ("n", repeat(False)),
        ("N", repeat(True)),
        ("*", word(False)),
        ("#", word(True)),
    ):
        target = text_object(key, no_move_handler=True, no_selection_handler=True)(
            target
        )

        @kb.add(key, filter=movable)
        def _move(event: KeyPressEvent, target=target) -> None:
            obj = target(event)
            if obj is not None:
                event.current_buffer.cursor_position += obj.start
