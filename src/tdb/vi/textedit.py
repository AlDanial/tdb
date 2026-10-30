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

    def line_delta(self, old: str) -> tuple[int, int, int]:
        """Return ``(first_row, old_last_row, new_last_row)`` of the edit.

        Rows are 0-based rows of *old* (the text the edit applies to);
        ``new_last_row`` is the last row the replacement occupies in the
        new text.
        """
        first_row = old.count("\n", 0, self.start)
        old_last_row = first_row + old.count("\n", self.start, self.old_end)
        new_last_row = first_row + self.new_text.count("\n")
        return first_row, old_last_row, new_last_row


def diff_text(old: str, new: str) -> TextEdit | None:
    """Single edit turning *old* into *new* (common prefix/suffix); None if equal.

    The common prefix and suffix are found by bisection over slice
    comparisons (done in C), so one keystroke on a large file stays cheap.
    """
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
