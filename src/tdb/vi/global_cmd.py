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
of lines makes one text change, not thousands.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TypeVar

from tdb.vi.ex import (
    ExCommand,
    ExError,
    ExHost,
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
    scope = _Scope(host)
    _depth += 1
    try:
        while True:
            try:
                row = marked.index(True)
            except ValueError:
                break
            marked[row] = False
            old = scope.begin(row)
            run_ex(command, scope)
            marked = _follow(marked, old, scope.lines(), row, scope.row_map, False)
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
    the host's marks follow their lines.  It flushes before a ``:normal``
    and anything else that needs the real text, and at the end.

    In a run, the cursor is at the start of the marked line until the
    command moves it; ``:s`` adds to the tally; the previous-jump mark
    stays where ``:g`` set it; the run is part of the ``:g``'s undo step.
    """

    def __init__(self, host: ExHost) -> None:
        self._host = host
        self.tally = SubstituteTally()
        self._lines = host.lines()
        self._base = len(self._lines)  # rows at the last flush
        self._origin = list(range(self._base))
        self._dirty = False
        self._undo_cursor: tuple[int, int] | None = None
        self._cursor = host.cursor_row(), host.cursor_col()
        # The column vim's ``:m`` / ``:t`` keep (the wanted column).
        self._want = self._cursor[1]
        self.moved = False
        # The row map of the run's one change, if it had one.
        self.row_map: Sequence[int] | None = None
        self._changes = 0

    def begin(self, row: int) -> list[str]:
        """Start a run on *row*; the lines before it."""
        if self.moved:
            self._want = self._cursor[1]
        self._cursor = row, 0
        self.moved = False
        self.row_map = None
        self._changes = 0
        return self._lines

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
        self._origin = list(range(self._base))
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
        hint, col = self._cursor[0], self.cursor_col()
        self.moved = True
        self._changes += 1
        self.row_map = row_map if self._changes == 1 else None
        self._lines = new
        self._cursor = self._clamp(*cursor)
        if new == old:
            return
        if self._undo_cursor is None:
            if undo_cursor is None:
                differ = (r for r, (a, b) in enumerate(zip(old, new)) if a != b)
                row = min(next(differ, len(new)), len(old) - 1)
                undo_cursor = row, col
            self._undo_cursor = undo_cursor
        self._origin = _follow(self._origin, old, new, hint, row_map, -1)
        self._dirty = True

    def set_register(
        self, name: str | None, text: str, linewise: bool, *, yank: bool = False
    ) -> None:
        self._host.set_register(name, text, linewise, yank=yank)

    def run_normal(self, row: int | None, keys: str) -> None:
        self.flush()
        self._host.run_normal(self._cursor[0] if row is None else row, keys)
        self._reload()
        self._cursor = self._host.cursor_row(), self._host.cursor_col()
        self.moved = True
        self._changes += 1
        self.row_map = None

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


def _follow(
    values: list[_T],
    old: list[str],
    new: list[str],
    row: int,
    row_map: Sequence[int] | None,
    fill: _T,
) -> list[_T]:
    """*values* (one per row of *old*) moved to the rows of *new*; a new
    line gets *fill*.

    The change is found by comparing the text; the lines before and after
    it keep their values.  With *row_map* (every old row's new row: a
    ``:m``, ``:t`` or ``:s``) the changed lines' values follow it.
    Otherwise the changed lines keep theirs one for one and a deleted
    line loses its value; a deletion or insertion among identical lines
    is put as near *row* (the line the command ran on) as it can be, as
    that is where commands change the text.

    Only the changed rows are visited one by one, so a ``:g`` whose
    command changes one line is not quadratic in Python steps.
    """
    if old == new:
        return values
    p, old_end, new_end = _changed_rows(old, new, row, slide=row_map is None)
    if row_map is None:
        kept = min(old_end - p, new_end - p)
        middle = values[p : p + kept] + [fill] * (new_end - p - kept)
        return values[:p] + middle + values[old_end:]
    middle = [fill] * (new_end - p)
    for r in range(p, old_end):
        to = row_map[r] - p
        if not 0 <= to < len(middle):
            break  # identical lines around the change moved too
        middle[to] = values[r]
    else:
        return values[:p] + middle + values[old_end:]
    out = [fill] * len(new)
    for r, to in enumerate(row_map):
        out[to] = values[r]
    return out


def _changed_rows(
    old: list[str], new: list[str], row: int, *, slide: bool
) -> tuple[int, int, int]:
    """``(first, old_end, new_end)``: rows ``first..old_end-1`` of *old*
    became rows ``first..new_end-1`` of *new*; the rest is the same.

    With *slide*, a deletion or insertion among identical lines is moved
    up towards *row*.
    """
    n, m = len(old), len(new)
    p = _common(lambda lo, hi: old[lo:hi] == new[lo:hi], min(n, m), row)
    s = _common(
        lambda lo, hi: old[n - hi : n - lo] == new[m - hi : m - lo],
        min(n, m) - p,
        n - row - 1,
    )
    old_end, new_end = n - s, m - s
    if slide and new_end == p:  # lines deleted
        while p > row and old[p - 1] == old[old_end - 1]:
            p, old_end, new_end = p - 1, old_end - 1, new_end - 1
    elif slide and old_end == p:  # lines inserted
        while p > row and new[p - 1] == new[new_end - 1]:
            p, old_end, new_end = p - 1, old_end - 1, new_end - 1
    return p, old_end, new_end


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
