"""Vi marks for one file: ``a``-``z``, the previous-jump mark and ``<``/``>``.

Pure Python: no prompt_toolkit or Textual imports.  Positions are
``(row, column)``, 0-based; columns are kept as set (a jump clamps them
to the line, as vim does), so a linewise ``>`` mark can hold ``MAXCOL``.
"""

from __future__ import annotations

from string import ascii_lowercase

from tdb.vi.textedit import TextEdit

# Column of a linewise ``>`` mark: past the end of any line (vim's MAXCOL).
MAXCOL = 2**31 - 1

# The previous-jump mark; `` ` `` names the same slot.
JUMP = "'"

NAMES = (*ascii_lowercase, JUMP, "<", ">")

# Marks vim moves rather than deletes when their line is deleted.
_KEPT = frozenset((JUMP, "<", ">"))

Pos = tuple[int, int]


def _slot(name: str) -> str:
    return JUMP if name == "`" else name


class MarkTable:
    """The marks of one file, kept up to date through text changes."""

    def __init__(self) -> None:
        self._marks: dict[str, Pos] = {}

    def set(self, name: str, pos: Pos) -> None:
        """Set mark *name* (one of ``NAMES`` or `` ` ``) to *pos*."""
        name = _slot(name)
        if name not in NAMES:
            raise ValueError(f"invalid mark name: {name!r}")
        self._marks[name] = pos

    def get(self, name: str) -> Pos | None:
        """Position of mark *name*, or None when it is not set."""
        return self._marks.get(_slot(name))

    def names(self) -> list[str]:
        """Names of the marks that are set, in ``NAMES`` order."""
        return [n for n in NAMES if n in self._marks]

    def adjust(
        self,
        first_row: int,
        old_last_row: int,
        new_last_row: int,
        *,
        last_cols: tuple[int, int] | None = None,
    ) -> None:
        """Follow a change of rows ``first_row..old_last_row`` into
        ``first_row..new_last_row``; see ``adjust_pos``."""
        for name, pos in list(self._marks.items()):
            new = adjust_pos(
                pos,
                first_row,
                old_last_row,
                new_last_row,
                keep=name in _KEPT,
                last_cols=last_cols,
            )
            if new is None:
                del self._marks[name]
            else:
                self._marks[name] = new

    def snapshot(self) -> dict[str, Pos]:
        """A copy of the marks, for ``restore``."""
        return dict(self._marks)

    def restore(self, snapshot: dict[str, Pos]) -> None:
        """Put back a ``snapshot``."""
        self._marks = dict(snapshot)


def adjust_pos(
    pos: Pos,
    first_row: int,
    old_last_row: int,
    new_last_row: int,
    *,
    keep: bool = False,
    last_cols: tuple[int, int] | None = None,
) -> Pos | None:
    """Where *pos* goes when old rows ``first_row..old_last_row`` became new
    rows ``first_row..new_last_row``.

    The first row keeps its marks (its start survived the change) and the
    old last row's marks move to the new last row (its end survived);
    with ``last_cols = (old_col, new_col)`` their columns move too, as in
    a join: a mark at or after ``old_col`` keeps its distance from it, one
    before it lands on ``new_col``.  Rows strictly between are gone: their
    marks are deleted (None), or with *keep* move to the row after
    ``first_row`` (vim's ``'`` ``<`` ``>``).  Rows after the change shift by
    the change in line count.  A whole-line change is described by rows
    one above and one below it, which may be -1 or past the end.
    """
    row, col = pos
    if row <= first_row:
        return pos
    if row == old_last_row:
        if last_cols is not None:
            old_col, new_col = last_cols
            col = new_col + max(col - old_col, 0)
        return new_last_row, col
    if row > old_last_row:
        return row + new_last_row - old_last_row, col
    return (first_row + 1, col) if keep else None


def edit_rows(
    edit: TextEdit, old: str, typed_at: int | None = None
) -> tuple[int, int, int, tuple[int, int] | None]:
    """``adjust`` arguments for *edit* applied to *old*.

    ``(first_row, old_last_row, new_last_row, last_cols)``: like
    ``TextEdit.line_delta``, except that an edit of whole lines (from a
    line start to a line start, or from a line end to a line end) is
    widened to the rows around it, so the lines it removed lose their
    marks and an inserted line pushes the marks below it down.

    *typed_at* is the cursor offset when the edit was typed in insert
    mode: an edit there is never whole lines (nvim keeps the marks of a
    line split by ``<CR>`` on its first part, even at column 0).
    """
    if edit.start == typed_at:
        return _general(edit, old, *edit.line_delta(old))
    edit = _whole_lines(edit, old)
    first, old_last, new_last = edit.line_delta(old)
    start_col = edit.start - (old.rfind("\n", 0, edit.start) + 1)
    end_col = edit.old_end - (old.rfind("\n", 0, edit.old_end) + 1)
    new = edit.new_text
    at_line_start = start_col == 0 and end_col == 0
    if at_line_start and (not new or new.endswith("\n")):
        return first - 1, old_last, new_last, None
    start_eol = edit.start == len(old) or old[edit.start] == "\n"
    end_eol = edit.old_end == len(old) or old[edit.old_end] == "\n"
    if start_eol and end_eol and (not new or new.startswith("\n")):
        return first, old_last + 1, new_last + 1, None
    return _general(edit, old, first, old_last, new_last)


def _general(
    edit: TextEdit, old: str, first: int, old_last: int, new_last: int
) -> tuple[int, int, int, tuple[int, int] | None]:
    """``adjust`` arguments for an edit inside lines (the last one joined)."""
    if old_last == first:
        return first, old_last, new_last, None
    start_col = edit.start - (old.rfind("\n", 0, edit.start) + 1)
    end_col = edit.old_end - (old.rfind("\n", 0, edit.old_end) + 1)
    new = edit.new_text
    tail = new.rsplit("\n", 1)[-1]
    new_col = len(tail) if "\n" in new else start_col + len(tail)
    return first, old_last, new_last, (end_col, new_col)


def _whole_lines(edit: TextEdit, old: str) -> TextEdit:
    """A pure insert or delete moved left to its line start, if it then
    covers whole lines.

    ``diff_text`` puts an edit as far right as it can: deleting the line
    ``    y`` above ``    z`` comes out as deleting ``y\n    `` after the
    common indent.  The same change made from the line start is whole
    lines, which is what vim saw.
    """
    if edit.start != edit.old_end and edit.new_text:
        return edit
    text = edit.new_text or old[edit.start : edit.old_end]
    line_start = old.rfind("\n", 0, edit.start) + 1
    k = edit.start - line_start
    if not 0 < k < len(text) or old[line_start : edit.start] != text[-k:]:
        return edit
    moved = old[line_start : edit.start] + text[:-k]
    if not moved.endswith("\n"):
        return edit
    if edit.new_text:
        return TextEdit(line_start, line_start, moved)
    return TextEdit(line_start, edit.old_end - k, "")
