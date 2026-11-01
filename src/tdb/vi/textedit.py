"""Minimal text diff used to mirror vi-engine edits into the TextArea.

Pure Python: no prompt_toolkit or Textual imports.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextEdit:
    """Replace ``old[start:old_end]`` with ``new_text`` (character offsets)."""

    start: int
    old_end: int
    new_text: str

    def line_delta(
        self, old: str, first_row: int | None = None
    ) -> tuple[int, int, int]:
        """Return ``(first_row, old_last_row, new_last_row)`` of the edit.

        Rows are 0-based rows of *old* (the text the edit applies to);
        ``new_last_row`` is the last row the replacement occupies in the
        new text.  *first_row*, when the caller knows it, saves counting
        the lines before the edit.
        """
        if first_row is None:
            first_row = old.count("\n", 0, self.start)
        old_last_row = first_row + old.count("\n", self.start, self.old_end)
        new_last_row = first_row + self.new_text.count("\n")
        return first_row, old_last_row, new_last_row


# The last diff_text call: (old, new, result).  The vi engine diffs the
# same two texts twice per change (its line cache, then the marks).
_last: tuple[str, str, TextEdit | None] = ("", "", None)


def diff_text(old: str, new: str) -> TextEdit | None:
    """Single edit turning *old* into *new* (common prefix/suffix); None if equal.

    The common prefix and suffix are found by bisection over slice
    comparisons (done in C), so one keystroke on a large file stays cheap.
    """
    global _last
    if old is _last[0] and new is _last[1]:
        return _last[2]
    edit = _diff(old, new)
    _last = (old, new, edit)
    return edit


def remember_insert(old: str, pos: int, data: str, new: str) -> None:
    """*new* is *old* with *data* inserted at *pos*: record what
    ``diff_text(old, new)`` returns, without comparing the whole texts.

    The edit is the one ``diff_text`` finds (as far right as the texts
    allow), so a caller sees no difference.  An insert whose text repeats
    what follows it for long is left to ``diff_text``.
    """
    global _last
    if not data:
        return
    n = len(data)
    shift = 0  # how far the common prefix runs past pos
    rest = len(old) - pos
    while shift < rest and shift < 64:
        here = data[shift] if shift < n else old[pos + shift - n]
        if old[pos + shift] != here:
            break
        shift += 1
    else:
        if shift < rest:
            return  # a long run: let diff_text find it
    start = pos + shift
    _last = (old, new, TextEdit(start, start, new[start : start + n]))


def _diff(old: str, new: str) -> TextEdit | None:
    if old == new:
        return None
    limit = min(len(old), len(new))
    prefix = _common_length(old, new, limit, lambda s, a, b: s[a:b])
    n_old, n_new = len(old), len(new)
    suffix = _common_length(
        old,
        new,
        limit - prefix,
        lambda s, a, b: s[len(s) - b : len(s) - a],
    )
    return TextEdit(prefix, n_old - suffix, new[prefix : n_new - suffix])


def _common_length(a: str, b: str, limit: int, part) -> int:
    """Largest ``n <= limit`` with ``part(a, 0, n) == part(b, 0, n)``.

    ``part(s, i, j)`` returns the chunk ``[i, j)`` of *s* measured from
    whichever end is being compared.
    """
    lo, hi = 0, limit  # part(a, 0, lo) == part(b, 0, lo); answer <= hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if part(a, lo, mid) == part(b, lo, mid):
            lo = mid
        else:
            hi = mid - 1
    return lo
