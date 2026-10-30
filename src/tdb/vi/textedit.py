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
    """Single edit turning *old* into *new* (common prefix/suffix); None if equal."""
    if old == new:
        return None
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    limit -= prefix
    while suffix < limit and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    return TextEdit(prefix, len(old) - suffix, new[prefix : len(new) - suffix])
