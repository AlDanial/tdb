"""Python-``re`` search with vim's rules for where a search starts (pure).

Patterns are Python regular expressions, compiled case-sensitive with
``re.MULTILINE`` so ``^`` and ``$`` match at every line, as in vim.

Offsets are into the engine text (without its final newline).  As vim
with its default ``'cpoptions'`` flag ``c``, matches do not overlap: the
scan restarts at the end of each match, from the start of the cursor's
line.  A match at a line end (only a zero-width one, or one beginning at
the line break) counts as the line's last character, where the cursor
would be put in normal mode.
"""

from __future__ import annotations

import re


class ViPatternError(ValueError):
    """An invalid search pattern; the message is ``re``'s."""


def compile_pattern(pattern: str, *, ignore_case: bool = False) -> re.Pattern:
    """Compile *pattern* (Python ``re``, multiline, case-sensitive unless
    *ignore_case*)."""
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ViPatternError(str(exc)) from None


def word_pattern(word: str) -> str:
    """The pattern ``*`` and ``#`` search for: *word* as a whole word."""
    return r"\b" + re.escape(word) + r"\b"


def place(text: str, pos: int) -> int:
    """Where the normal-mode cursor goes for a match at *pos*."""
    at_end = pos == len(text) or text[pos] == "\n"
    if at_end and pos > 0 and text[pos - 1] != "\n":
        return pos - 1
    return pos


def find(
    text: str, pattern: re.Pattern, offset: int, backward: bool
) -> tuple[int, int, bool] | None:
    """The next match before/after *offset*: ``(start, end, wrapped)``.

    Forward, the first match placed after *offset*; backward, the last
    one placed before it.  Without one, the search wraps once around the
    text (then a match at *offset* itself counts).  None when *pattern*
    does not match anywhere.
    """
    if backward:
        before = last = None
        for m in pattern.finditer(text):
            last = m
            if place(text, m.start()) < offset:
                before = m
        if before is not None:
            return before.start(), before.end(), False
        return None if last is None else (last.start(), last.end(), True)
    line_start = text.rfind("\n", 0, offset) + 1
    for m in pattern.finditer(text, line_start):
        if place(text, m.start()) > offset:
            return m.start(), m.end(), False
    m = pattern.search(text)
    return None if m is None else (m.start(), m.end(), True)


def find_nth(
    text: str, pattern: re.Pattern, offset: int, backward: bool, count: int
) -> tuple[int, int, bool] | None:
    """``find`` *count* times from *offset*; ``wrapped`` if any step wrapped."""
    found = None
    wrapped = False
    for _ in range(max(count, 1)):
        found = find(text, pattern, offset, backward)
        if found is None:
            return None
        wrapped = wrapped or found[2]
        offset = place(text, found[0])
    assert found is not None
    return found[0], found[1], wrapped


def match_spans(
    text: str, pattern: re.Pattern, *, limit: int = 100_000
) -> dict[int, list[tuple[int, int]]]:
    """Every non-empty match of *pattern* in *text*, as ``row -> [(start
    column, end column), ...]`` (end exclusive) for ``'hlsearch'``.

    A match spanning lines is split at each line break (the break itself
    is not drawn).  Zero-width matches are skipped.  At most *limit*
    matches are collected, so a pattern such as ``.`` on a huge file
    stays cheap.
    """
    spans: dict[int, list[tuple[int, int]]] = {}
    row, row_start = 0, 0  # the row holding offset `row_start`
    for count, m in enumerate(pattern.finditer(text)):
        if count >= limit:
            break
        start, end = m.span()
        if start == end:
            continue
        # Advance to the row of `start` (matches arrive in order).
        nl = text.count("\n", row_start, start)
        if nl:
            row += nl
            row_start = text.rfind("\n", 0, start) + 1
        r, line_start = row, row_start
        while True:
            line_end = text.find("\n", line_start)
            if line_end == -1:
                line_end = len(text)
            seg_end = min(end, line_end)
            if seg_end > start:
                spans.setdefault(r, []).append(
                    (start - line_start, seg_end - line_start)
                )
            if end <= line_end + 1:
                break
            r, line_start, start = r + 1, line_end + 1, line_end + 1
    return spans
