"""Pure ports of vim's text objects and ``J`` (no prompt_toolkit).

Ports of ``current_word``, ``current_par``, ``current_block``,
``current_quote`` and ``do_join``.  Positions are ``(row, col)`` over a list
of lines; ``col == len(line)`` is the line end.  prompt_toolkit's versions
differ from vim on whitespace runs, paragraphs and "no enclosing pair".
"""

from __future__ import annotations

Pos = tuple[int, int]  # (row, column); column == len(line) is the line end


class Cursor:
    """vim's cursor primitives (``inc``/``dec``/``cls`` ...) over *lines*."""

    def __init__(self, lines: list[str], pos: Pos, bigword: bool = False) -> None:
        self.lines = lines
        self.row, self.col = pos
        self.bigword = bigword

    @property
    def pos(self) -> Pos:
        return self.row, self.col

    def set(self, pos: Pos) -> None:
        self.row, self.col = pos

    def char(self) -> str:
        line = self.lines[self.row]
        return line[self.col] if self.col < len(line) else ""

    def cls(self) -> int:
        """Character class: 0 blank (or line end), 1 punctuation, 2 word."""
        c = self.char()
        if c in ("", " ", "\t"):
            return 0
        if self.bigword:
            return 1
        return 2 if c.isalnum() or c == "_" else 1

    def inc(self) -> int:
        """vim ``inc()``: 0 normal, 2 onto the line end, 1 next line, -1 at EOF."""
        line = self.lines[self.row]
        if self.col < len(line):
            self.col += 1
            return 0 if self.col < len(line) else 2
        if self.row < len(self.lines) - 1:
            self.row += 1
            self.col = 0
            return 1
        return -1

    def dec(self) -> int:
        if self.col > 0:
            self.col -= 1
            return 0
        if self.row > 0:
            self.row -= 1
            self.col = len(self.lines[self.row])
            return 1
        return -1

    def incl(self) -> int:
        r = self.inc()
        if r >= 1 and self.col:
            r = self.inc()
        return r

    def decl(self) -> int:
        r = self.dec()
        if r == 1 and self.col:
            r = self.dec()
        return r

    def in_indent(self, extra: int) -> bool:
        """Whether the cursor is within the leading white space of its line."""
        line = self.lines[self.row]
        return len(line) - len(line.lstrip(" \t")) >= self.col + extra

    def back_in_line(self) -> None:
        sclass = self.cls()
        while self.col > 0:
            self.dec()
            if self.cls() != sclass:
                self.inc()
                break

    def skip_chars(self, cclass: int) -> bool:
        """Move forward over *cclass*; True when the buffer end was hit."""
        while self.cls() == cclass:
            if self.inc() == -1:
                return True
        return False

    def end_word(self) -> bool:
        sclass = self.cls()
        if self.inc() == -1:
            return False
        if self.cls() == sclass and sclass != 0:
            if self.skip_chars(sclass):
                return False
        elif sclass == 0:
            while self.cls() == 0:
                if self.col == 0 and not self.lines[self.row]:
                    return True  # stop at an empty line
                if self.inc() == -1:
                    return False
            if self.skip_chars(self.cls()):
                return False
        self.dec()
        return True

    def fwd_word(self) -> bool:
        """Start of the next word, stopping at a line end (vim ``fwd_word``)."""
        sclass = self.cls()
        last_line = self.row == len(self.lines) - 1
        i = self.inc()
        if i == -1 or (i >= 1 and last_line):
            return False
        if i >= 1:
            return True
        if sclass != 0:
            while self.cls() == sclass:
                if self.inc() != 0:
                    return True
        while self.cls() == 0:
            if self.col == 0 and not self.lines[self.row]:
                break
            i = self.inc()
            if i == -1 or (i >= 1 and last_line):
                return True
        return True


class NoObject(Exception):
    """No such text object; ``pos`` is where vim's failed search left the cursor."""

    def __init__(self, pos: Pos) -> None:
        super().__init__(pos)
        self.pos = pos


def index(lines: list[str], pos: Pos) -> int:
    row, col = pos
    return sum(len(line) + 1 for line in lines[:row]) + col


def current_word(
    lines: list[str],
    pos: Pos,
    count: int,
    include: bool,
    bigword: bool,
    visual_start: Pos | None,
) -> tuple[Pos, Pos, bool]:
    """Port of vim's ``current_word``: ``(start, end, inclusive)``.

    Raises ``NoObject`` (carrying vim's final cursor) when there is none.
    """
    c = Cursor(lines, pos, bigword)
    inclusive = True
    include_white = False
    if visual_start is None or visual_start == pos:
        c.back_in_line()
        start = c.pos
        if (c.cls() == 0) == include:
            if not c.end_word():
                raise NoObject(c.pos)
        else:
            c.fwd_word()
            if c.col == 0:
                c.decl()
            else:
                c.col -= 1
            include_white = include
        count -= 1
    elif visual_start < pos:
        start = visual_start
    else:
        raise NoObject(c.pos)
    while count > 0:
        inclusive = True
        if c.incl() == -1:
            raise NoObject(c.pos)
        if include != (c.cls() == 0):
            if not c.fwd_word() and count > 1:
                raise NoObject(c.pos)
            if c.col == 0:
                inclusive = False
            else:
                c.col -= 1
        elif not c.end_word():
            raise NoObject(c.pos)
        count -= 1
    if include_white and (c.cls() != 0 or (c.col == 0 and not inclusive)):
        end = c.pos
        c.set(start)
        if c.col > 0:
            c.col -= 1
            c.back_in_line()
            if c.cls() == 0 and c.col > 0:
                start = c.pos
        c.set(end)
    return start, c.pos, inclusive


def current_par(
    lines: list[str], row: int, count: int, include: bool, visual_row: int | None
) -> tuple[int, int] | None:
    """Port of vim's ``current_par``: ``(first_row, last_row)`` or None."""
    last = len(lines) - 1

    def white(r: int) -> bool:
        return not lines[r].strip(" \t")

    if visual_row is not None and visual_row != row:
        step = -1 if row < visual_row else 1
        edge = 0 if step < 0 else last
        for _ in range(count):
            if row == edge:
                return None
            prev_white = None
            for _t in range(2):
                row += step
                now_white = white(row)
                if prev_white == now_white:
                    row -= step
                    break
                while row != edge and now_white == white(row + step):
                    row += step
                if not include or row == edge:
                    break
                prev_white = now_white
        return visual_row, row
    start = row
    white_in_front = white(start)
    while start > 0 and white(start - 1) == white_in_front:
        start -= 1
    end = start
    while end <= last and white(end):
        end += 1
    end -= 1
    i = count
    if not include and white_in_front:
        i -= 1
    do_white = False
    while i > 0:
        i -= 1
        if end == last:
            return None
        if not include:
            do_white = white(end + 1)
        if include or not do_white:
            end += 1
            while end < last and not white(end + 1):
                end += 1
        if i == 0 and white_in_front and include:
            break
        if include or do_white:
            while end < last and white(end + 1):
                end += 1
    if not white_in_front and not white(end) and include:
        while start > 0 and white(start - 1):
            start -= 1
    return start, end


PAIRS = {
    "(": ("(", ")"), ")": ("(", ")"), "b": ("(", ")"),
    "{": ("{", "}"), "}": ("{", "}"), "B": ("{", "}"),
    "[": ("[", "]"), "]": ("[", "]"),
    "<": ("<", ">"), ">": ("<", ">"),
}  # fmt: skip


def _find_open(text: str, idx: int, open_: str, close: str) -> int | None:
    """Index of the unmatched *open_* before *idx* (``text[idx]`` is not examined)."""
    depth = 0
    for i in range(idx - 1, -1, -1):
        ch = text[i]
        if ch == close:
            depth += 1
        elif ch == open_:
            if depth == 0:
                return i
            depth -= 1
    return None


def _find_close(text: str, idx: int, open_: str, close: str) -> int | None:
    """Index of the *close* matching the *open_* at *idx*."""
    depth = 0
    for i in range(idx + 1, len(text)):
        ch = text[i]
        if ch == open_:
            depth += 1
        elif ch == close:
            if depth == 0:
                return i
            depth -= 1
    return None


def block_span(
    lines: list[str],
    pos: Pos,
    key: str,
    count: int,
    inner: bool,
    selection: tuple[Pos, Pos] | None,
) -> tuple[Pos, Pos, bool] | None:
    """Port of vim's ``current_block``: ``(start, end, sol)`` or None.

    *end* is inclusive.  For an inner object *start* may lie after *end*
    (nothing between the brackets).  ``sol`` is vim's flag for a closing
    bracket that starts its line.  *selection* is the Visual range, if any.
    """
    open_, close = PAIRS[key]
    text = "\n".join(lines)
    cursor = index(lines, pos)
    old_start = old_end = pos
    if selection is not None and selection[0] != selection[1]:
        old_start, old_end = selection
        cursor = index(lines, old_start)
    elif open_ == "{":
        c = Cursor(lines, pos)
        while c.in_indent(1):
            if c.inc() != 0:
                break
        cursor = index(lines, c.pos)
    if cursor < len(text) and text[cursor] == open_:
        cursor += 1
    open_idx = cursor
    for _ in range(count):
        found = _find_open(text, open_idx, open_, close)
        if found is None:
            return None
        open_idx = found

    def locate(i: int) -> Pos:
        before = text[:i]
        return before.count("\n"), i - (before.rfind("\n") + 1)

    while True:
        close_idx = _find_close(text, open_idx, open_, close)
        if close_idx is None:
            return None
        start, end, sol = locate(open_idx), locate(close_idx), False
        if inner:
            c = Cursor(lines, start)
            c.incl()
            start = c.pos
            c.set(end)
            sol = c.col == 0
            c.decl()
            while c.in_indent(1):
                sol = True
                if c.decl() != 0:
                    break
            end = c.pos
        if selection is None or start < old_start or end > old_end or start == end:
            return start, end, sol
        # Not bigger than the current selection: take the enclosing block.
        found = _find_open(text, open_idx, open_, close)
        if found is None:
            return None
        open_idx = found


def quote_span(
    line: str, col: int, quote: str, include: bool
) -> tuple[int, int] | None:
    """Port of vim's ``current_quote``: the ``[start, end)`` columns in *line*."""

    def next_quote(i: int) -> int:
        while i < len(line):
            if line[i] == "\\":
                i += 1
            elif line[i] == quote:
                return i
            i += 1
        return -1

    def prev_quote(i: int) -> int:
        while i > 0:
            i -= 1
            n = 0
            while i - n > 0 and line[i - n - 1] == "\\":
                n += 1
            if n & 1:
                i -= n
            elif line[i] == quote:
                break
        return i

    if col < len(line) and line[col] == quote:
        start = 0
        while True:
            start = next_quote(start)
            if start < 0 or start > col:
                return None
            end = next_quote(start + 1)
            if end < 0:
                return None
            if start <= col <= end:
                break
            start = end + 1
    else:
        start = prev_quote(col)
        if start >= len(line) or line[start] != quote:
            start = next_quote(start)
            if start < 0:
                return None
        end = next_quote(start + 1)
        if end < 0:
            return None
    if include:
        if end + 1 < len(line) and line[end + 1] in " \t":
            while end + 1 < len(line) and line[end + 1] in " \t":
                end += 1
        else:
            while start > 0 and line[start - 1] in " \t":
                start -= 1
        return start, end + 1
    return start + 1, end


def join_lines(
    lines: list[str], row: int, count: int, space: bool
) -> tuple[list[str], int]:
    """vim ``do_join``: join *count* lines from *row*; returns (lines, cursor col)."""
    count = min(count, len(lines) - row)
    joined = lines[row]
    col = len(joined)
    prev = joined  # previous (white-space trimmed) piece
    for nxt in lines[row + 1 : row + count]:
        sep = ""
        if space:
            nxt = nxt.lstrip(" \t")
            # No space before ")", after a trailing blank, or onto/from nothing.
            if nxt and nxt[0] != ")" and joined and prev[-1:] not in (" ", "\t"):
                sep = " "
        col = len(joined)
        joined += sep + nxt
        prev = nxt
    return lines[:row] + [joined] + lines[row + count :], col
