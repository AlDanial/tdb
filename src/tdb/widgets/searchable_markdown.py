"""Find-in-page for the documentation viewer (`tdb --doc` and
Help -> Documentation): a MarkdownViewer with a search bar."""

from __future__ import annotations

MIN_INCREMENTAL = 4
"""Query length at which search runs as you type; Enter searches shorter
queries."""


def find_matches(text: str, query: str) -> list[tuple[int, int]]:
    """Non-overlapping (start, end) offsets of `query` in `text`, left to
    right. Smartcase: case-insensitive unless `query` has an uppercase
    letter."""
    if not query:
        return []
    if query == query.lower():
        text = text.lower()
    matches: list[tuple[int, int]] = []
    start = text.find(query)
    while start != -1:
        end = start + len(query)
        matches.append((start, end))
        start = text.find(query, end)
    return matches
