"""``:g`` and ``:v``: an ex command on each line that matches, or does not.

Pure Python: no prompt_toolkit or Textual imports.  As vim, in two
passes: first every line of the range (default: the whole file) whose
text matches the pattern is marked (``:v`` / ``:g!``: does not match);
then, while a line is still marked, the first one is unmarked and the
command runs with the cursor at its start.  A line deleted by an earlier
run is not visited, and a moved line keeps its mark.  An empty pattern
is the last search; the pattern becomes the last search.

The whole ``:g`` is one undo step (``run_ex`` opens the group).  As vim,
the previous-jump mark is set once, to where the cursor was; the counts
of the ``:s`` runs are reported once at the end, and an ``:s`` that
finds nothing on some line is no error.  The cursor ends where the last
run left it (on its line's first non-blank if any ``:s`` substituted).
The first error stops ``:g``, keeping the changes made so far.

Inside ``:g`` (also from a ``:normal`` it runs), another ``:g`` without
a range runs its command on the current line if that line matches, as
in vim; with a range it is an error.  ``:s///c`` is an error there (vim
asks about each match; tdb does not support that).

The ex commands run on a copy of the lines (``_Scope``), which goes to
the host in one ``replace_lines`` at the end, or before a ``:normal``
(which runs in the editor itself).  So a ``:g`` that changes thousands
of lines makes one text change, not thousands.  The marked lines follow
each ex command's row map exactly, also among identical lines; through
a ``:normal`` they follow each key's change, placed by the cursor rows
before and after it (``_place``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TypeVar

from tdb.vi.ex import (
    ExCommand,
    ExError,
    ExHost,
    LineWatch,
    SubstituteTally,
    resolve_range,
    run_ex,
)
from tdb.vi.search import ViPatternError, compile_pattern
from tdb.vi.substitute import SubstituteSpec, count_message, delimited_field

# How many ``:g`` are running (a nested one only checks the current line).
_depth = 0


def run_global(cmd: ExCommand, host: ExHost, invert: bool) -> None:
    """``:[range]g/pat/cmd`` (*invert*: ``:v``, the lines that do not match)."""
    global _depth
    pattern, command = _parse(cmd.args, host)
    try:
        rx = compile_pattern(pattern)
    except ViPatternError as exc:
        raise ExError(f"Invalid pattern: {exc}") from None
    host.set_last_search(pattern)
    first, last = resolve_range(cmd.range, host, "all")
    lines = host.lines()
    if _depth:
        if (first, last) != (0, len(lines) - 1):
            raise ExError("Cannot do :global recursive with a range")
        if (rx.search(lines[host.cursor_row()]) is not None) != invert:
            run_ex(command, host)
        return
    marked = [False] * len(lines)
    for row in range(first, last + 1):
        marked[row] = (rx.search(lines[row]) is not None) != invert
    if not any(marked):
        found = "found in every line" if invert else "not found"
        host.notify(f"Pattern {found}: {pattern}", "warning")
        return
    host.set_mark("'", (host.cursor_row(), host.cursor_col()))
    scope = _Scope(host, marked)
    _depth += 1
    try:
        while scope.next_marked():
            run_ex(command, scope)
    finally:
        _depth -= 1
        scope.finish()


def _parse(args: str, host: ExHost) -> tuple[str, str]:
    """The pattern and the command of ``/pat/cmd``."""
    if not args:
        raise ExError("No previous regular expression")
    delim = args[0]
    if delim.isalnum() or delim in ' \t\\"|':
        raise ExError("Regular expressions can't be delimited by letters")
    pattern, i = delimited_field(args, 1, delim, keep_escape=True)
    command = args[i:]
    if not command.strip():
        raise ExError(":global without a command is not supported")
    if not pattern:
        pattern = host.last_search() or ""
        if not pattern:
            raise ExError("No previous regular expression")
    return pattern, command


def _first_non_blank(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


class _Scope:
    """The ``ExHost`` the command runs on: a copy of the host's lines.

    Changes stay here until ``flush`` hands them to the host in one
    ``replace_lines``, with a row map from where each line was at the
    last flush (``_origin``: that row, or -1 for a line made since), so
    the host's marks follow their lines.  It also keeps which lines are
    still to be visited (``_marked``) through every change: exactly with
    the row map each ex command gives, and key by key in a ``:normal``.  It flushes before a ``:normal``
    and anything else that needs the real text, and at the end.

    In a run, the cursor is at the start of the marked line until the
    command moves it; ``:s`` adds to the tally; the previous-jump mark
    stays where ``:g`` set it; the run is part of the ``:g``'s undo step.
    """

    def __init__(self, host: ExHost, marked: list[bool]) -> None:
        self._host = host
        self._marked = marked
        self.tally = SubstituteTally()
        self._lines = host.lines()
        self._base = len(self._lines)  # rows at the last flush
        self._origin_rows: list[int] | None = None  # None: each its own row
        self._dirty = False
        self._undo_cursor: tuple[int, int] | None = None
        self._cursor = host.cursor_row(), host.cursor_col()
        # The column vim's ``:m`` / ``:t`` keep (the wanted column).
        self._want = self._cursor[1]
        self.moved = False

    @property
    def _origin(self) -> list[int]:
        """Each row's row at the last flush (-1: made since); built only
        when a command here needs it (a run of :normal keys does not)."""
        if self._origin_rows is None:
            self._origin_rows = list(range(self._base))
        return self._origin_rows

    @_origin.setter
    def _origin(self, rows: list[int]) -> None:
        self._origin_rows = rows

    def next_marked(self) -> bool:
        """Unmark the first marked line and start a run on it (False: none)."""
        try:
            row = self._marked.index(True)
        except ValueError:
            return False
        self._marked[row] = False
        if self.moved:
            self._want = self._cursor[1]
        self._cursor = row, 0
        self.moved = False
        return True

    def finish(self) -> None:
        """Where vim leaves the cursor, the text to the host, the count."""
        tally = self.tally
        if tally.substitutions:
            row = self._cursor[0]
            self._cursor = row, _first_non_blank(self._lines[row])
        self.flush()
        self._host.set_cursor(*self._cursor)
        if tally.substitutions > 2:
            self._host.notify(count_message(tally.substitutions, tally.lines))

    def flush(self) -> None:
        """Hand the changes made since the last flush to the host."""
        if not self._dirty:
            return
        row_map = [-1] * self._base
        for new, old in enumerate(self._origin):
            if old >= 0:
                row_map[old] = new
        self._host.replace_lines(
            self._lines, self._cursor, row_map=row_map, undo_cursor=self._undo_cursor
        )
        self._reload()

    def _reload(self) -> None:
        """Take the host's text as the new starting point."""
        self._lines = self._host.lines()
        self._base = len(self._lines)
        self._origin_rows = None
        self._dirty = False
        self._undo_cursor = None

    # -- ExHost --------------------------------------------------------

    def lines(self) -> list[str]:
        return self._lines  # the commands do not modify it

    def cursor_row(self) -> int:
        return self._cursor[0]

    def cursor_col(self) -> int:
        return self._cursor[1] if self.moved else self._want

    def set_cursor(self, row: int, col: int) -> None:
        self.moved = True
        self._cursor = self._clamp(row, col)

    def _clamp(self, row: int, col: int) -> tuple[int, int]:
        row = max(0, min(row, len(self._lines) - 1))
        return row, max(0, min(col, len(self._lines[row]) - 1))

    def mark(self, name: str) -> tuple[int, int] | None:
        pos = self._host.mark(name)
        if pos is None or not self._dirty:
            return pos
        try:
            return self._origin.index(pos[0]), pos[1]
        except ValueError:
            return None  # its line was deleted

    def set_mark(self, name: str, pos: tuple[int, int]) -> None:
        if name not in ("'", "`"):
            self.flush()
            self._host.set_mark(name, pos)

    def replace_lines(
        self,
        lines: list[str],
        cursor: tuple[int, int],
        *,
        row_map: Sequence[int] | None = None,
        undo_cursor: tuple[int, int] | None = None,
    ) -> None:
        old = self._lines
        new = lines or [""]
        row, col = self._cursor[0], self.cursor_col()
        self.moved = True
        self._lines = new
        self._cursor = self._clamp(*cursor)
        if row_map is not None:  # the ex commands here all give one
            self._marked = _follow_map(self._marked, row_map, len(new), row, False)
            self._origin = _follow_map(self._origin, row_map, len(new), row, -1)
        elif new != old:
            change = _text_change(self._marked, old, new, row, row, self._cursor[0])
            self._marked = _splice(self._marked, change, False)
            self._origin = _splice(self._origin, change, -1)
        if new == old:
            return
        if self._undo_cursor is None:
            if undo_cursor is None:
                differ = (r for r, (a, b) in enumerate(zip(old, new)) if a != b)
                undo_cursor = min(next(differ, len(new)), len(old) - 1), col
            self._undo_cursor = undo_cursor
        self._dirty = True

    def set_register(
        self, name: str | None, text: str, linewise: bool, *, yank: bool = False
    ) -> None:
        self._host.set_register(name, text, linewise, yank=yank)

    def run_normal(
        self, row: int | None, keys: str, *, watch: LineWatch | None = None
    ) -> None:
        self.flush()
        start = self._cursor[0] if row is None else row
        self._host.run_normal(start, keys, watch=self._watch)
        self._reload()
        self._cursor = self._host.cursor_row(), self._host.cursor_col()
        self.moved = True

    def _watch(self, old: list[str], before: int, after: int, record: list) -> None:
        """A key of a ``:normal`` changed the text: the marks follow, by
        what the code that changed it recorded, else by the text."""
        new = self._host.lines()
        if not record and len(old) == len(new):
            return  # no line came or went: each mark stays on its row
        marked = _replay(self._marked, record, before)
        if marked is None or len(marked) != len(new):
            change = _text_change(self._marked, old, new, before, before, after)
            marked = _splice(self._marked, change, False)
        self._marked = marked

    def last_search(self) -> str | None:
        return self._host.last_search()

    def set_last_search(self, pattern: str) -> None:
        self._host.set_last_search(pattern)

    def last_substitute(self) -> SubstituteSpec | None:
        return self._host.last_substitute()

    def set_last_substitute(self, spec: SubstituteSpec) -> None:
        self._host.set_last_substitute(spec)

    def confirm_substitute(self, spec: SubstituteSpec, first: int, last: int) -> None:
        raise ExError("The c flag of :s is not supported inside :global")

    def substitute_tally(self) -> SubstituteTally:
        return self.tally

    def save(self) -> None:
        self.flush()
        self._host.save()

    def leave(self, discard: bool) -> None:
        self._host.leave(discard)

    def notify(self, text: str, severity: str = "information") -> None:
        self._host.notify(text, severity)

    def fail(self) -> None:
        self._host.fail()

    def goto_line(self, row: int) -> None:
        self.flush()
        self._host.set_cursor(*self._cursor)
        self._host.goto_line(row)
        self._cursor = self._host.cursor_row(), self._host.cursor_col()
        self.moved = True

    @contextmanager
    def undo_group(self) -> Iterator[None]:
        yield  # the :g's own group holds every run


# ----------------------------------------------------------------------
# Following lines through a change

_T = TypeVar("_T")


def _replay(marked: list[bool], record: list, row: int) -> list[bool] | None:
    """*marked* through the key's records (``LineRecord``); None when it
    has none, or a change it did not record."""
    if not record or None in record:
        return None
    for entry in record:
        if entry[0] == "rows":
            _, first, deleted, inserted = entry
            marked = marked[:first] + [False] * inserted + marked[first + deleted :]
        else:
            _, row_map, count = entry
            if len(row_map) != len(marked):
                return None
            marked = _follow_map(marked, row_map, count, row, False)
    return marked


_IDS: list[int] = []


def _ids(n: int) -> list[int]:
    """``[0, 1, ...]``, at least *n* long and shared, so slices of it are
    cheap to compare with a row map."""
    if len(_IDS) < n:
        _IDS.extend(range(len(_IDS), n))
    return _IDS


def _follow_map(
    values: list[_T], row_map: Sequence[int], count: int, row: int, fill: _T
) -> list[_T]:
    """*values* (one per old row) moved by *row_map* (each old row's new
    row, -1: deleted) to *count* new rows; a new line gets *fill*.

    The rows the map moves are found from the map itself, with slice
    comparisons (*row* is where to look first): the rows before them
    keep their row and the rows after them shift by the change in line
    count.  Only the moved rows are visited one by one, so a ``:g`` whose
    command changes one line is not quadratic in Python steps.
    """
    rows = list(row_map)
    n = len(rows)
    ids = _ids(max(n, count))
    limit = min(n, count)
    p = _common(lambda lo, hi: rows[lo:hi] == ids[lo:hi], limit, row)
    s = _common(
        lambda lo, hi: rows[n - hi : n - lo] == ids[count - hi : count - lo],
        limit - p,
        n - row - 1,
    )
    middle = [fill] * (count - s - p)
    for r in range(p, n - s):
        to = rows[r] - p
        if 0 <= to < len(middle):
            middle[to] = values[r]
        elif rows[r] >= 0:  # not one block: follow the whole map
            out = [fill] * count
            for old, new in enumerate(rows):
                if new >= 0:
                    out[new] = values[old]
            return out
    return values[:p] + middle + values[n - s :]


# ``(first, old_end, new_end)``: old rows ``first..old_end-1`` became new
# rows ``first..new_end-1``; the rows around them are the same lines.
_Change = tuple[int, int, int]


def _splice(values: list[_T], change: _Change, fill: _T) -> list[_T]:
    """*values* through *change*: the changed rows keep theirs one for
    one, extra new rows get *fill*, deleted rows lose theirs."""
    p, old_end, new_end = change
    kept = min(old_end, new_end) - p
    middle = values[p : p + kept] + [fill] * (new_end - p - kept)
    return values[:p] + middle + values[old_end:]


def _text_change(
    marked: list[bool],
    old: list[str],
    new: list[str],
    row: int,
    before: int,
    after: int,
) -> _Change:
    """The change from *old* to *new*, found by comparing the text.

    Lines deleted or inserted among identical lines could have gone at
    several rows; ``_place`` picks one from the cursor row *before* and
    *after* the change (*row*: the run's line, *marked*: the lines still
    to visit).
    """
    n, m = len(old), len(new)
    p = _common(lambda lo, hi: old[lo:hi] == new[lo:hi], min(n, m), before)
    s = _common(
        lambda lo, hi: old[n - hi : n - lo] == new[m - hi : m - lo],
        min(n, m) - p,
        n - before - 1,
    )
    old_end, new_end = n - s, m - s
    if p in (old_end, new_end):  # only deleted, or only inserted lines
        size = old_end - new_end
        p = _place(marked, old if size > 0 else new, p, size, row, before, after)
        old_end, new_end = p + max(size, 0), p + max(-size, 0)
    return p, old_end, new_end


def _place(
    marked: list[bool],
    lines: list[str],
    last: int,
    size: int,
    row: int,
    before: int,
    after: int,
) -> int:
    """The row where *size* lines were deleted (``-size`` inserted, if
    negative); *lines* is the text that has them.

    *last* is the last row it can be; if the lines above are the same
    text, it can be any row from ``first`` to *last*.  A line delete
    (``dd``, ``dj``, ``dk``, ``:d``...) takes the cursor's line and
    leaves the cursor on the first line after the deleted ones, or on
    the one before them when they were the last; a put or ``o`` / ``O``
    adds lines at or below the cursor's line and puts the cursor on
    them.  The rows that fit both cursor rows are taken.  When that
    leaves a choice (or none), the deleted lines are taken to be marked
    ones: a line vim may have deleted is then not visited, so the
    command does not run more often than vim's would.
    """
    count = abs(size)
    first = last
    while first > 0 and lines[first - 1] == lines[first - 1 + count]:
        first -= 1
    if first == last:
        return last
    span = range(first, last + 1)
    if size < 0:
        fits = [c for c in (before, before + 1) if c in span and c <= after < c + count]
        return fits[0] if len(fits) == 1 else min(max(after, first), last)
    end = len(lines) - count  # the start of a block that was the last lines
    fits = [
        c
        for c in (after, after + 1)
        if c in span
        and c <= before < c + count
        and (c == after or (c == end and after == c - 1))
    ]
    if len(fits) == 1:
        return fits[0]
    choices = fits or [c for c in (after, after + 1) if c in span] or list(span)
    return max(choices, key=lambda c: (sum(marked[c : c + count]), -abs(c - row)))


def _common(same: Callable[[int, int], bool], limit: int, hint: int) -> int:
    """The largest ``k <= limit`` with ``same(0, k)``.

    ``same(lo, hi)`` says whether items ``lo..hi-1`` agree; *hint* is a
    guess at ``k``, checked first.  A bisection after that, so the work
    is a few slice comparisons, not one Python step per line.
    """
    lo, hi = 0, limit
    h = max(0, min(hint, limit))
    if same(0, h):
        lo = h
    else:
        hi = h - 1
    while lo < hi:
        if not same(lo, lo + 1):
            return lo
        mid = (lo + hi + 1) // 2
        if same(lo, mid):
            lo = mid
        else:
            hi = mid - 1
    return lo
