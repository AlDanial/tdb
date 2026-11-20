"""Find-in-page for the documentation viewer (`tdb --doc`,
Help -> Documentation)."""

from __future__ import annotations

from textual.app import App
from textual.widgets import Input, Label, Markdown, Static
from textual.widgets._markdown import (
    MarkdownFence,
    MarkdownParagraph,
    MarkdownTableCellContents,
)

from tdb.widgets.searchable_markdown import (
    MIN_INCREMENTAL,
    SearchableMarkdownViewer,
    find_matches,
)


def test_min_incremental_is_four():
    assert MIN_INCREMENTAL == 4


def test_lowercase_query_is_case_insensitive():
    assert find_matches("GDB and gdb", "gdb") == [(0, 3), (8, 11)]


def test_uppercase_in_query_makes_it_case_sensitive():
    assert find_matches("GDB and gdb", "GDB") == [(0, 3)]


def test_matches_do_not_overlap():
    assert find_matches("aaaa", "aa") == [(0, 2), (2, 4)]


def test_empty_query_matches_nothing():
    assert find_matches("anything", "") == []


# --- SearchableMarkdownViewer ------------------------------------------------

DOC = """# Title

Alpha bravo gdb. Another gdb here.

```python
gdb_value = 1
```

| Col | Other |
|-----|-------|
| gdb cell | x |

- list item mentioning breakpoint
"""


class _Host(App):
    BINDINGS = [("q", "quit"), ("escape", "quit")]

    def __init__(self) -> None:
        super().__init__()
        self.quit_called = False

    def compose(self):
        yield SearchableMarkdownViewer(DOC, show_table_of_contents=False)

    def action_quit(self) -> None:
        self.quit_called = True


def _viewer(app) -> SearchableMarkdownViewer:
    return app.query_one(SearchableMarkdownViewer)


def _count(app) -> str:
    return str(app.query_one("#doc-search-count", Static).visual)


def _search_spans(app) -> list[str]:
    """Every `.search-*` style currently rendered in the document."""
    widgets = list(app.query("MarkdownBlock, MarkdownTableCellContents"))
    widgets += list(app.query("#code-content"))
    out = []
    for w in widgets:
        for span in getattr(w.visual, "spans", []):
            if isinstance(span.style, str) and span.style.startswith(".search-"):
                out.append(span.style)
    return out


async def _press(pilot, *keys: str) -> None:
    for key in keys:
        await pilot.press(key)
        await pilot.pause()


async def test_slash_opens_bar_and_focuses_input():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        assert not _viewer(app).search_active
        await _press(pilot, "slash")
        assert _viewer(app).search_active
        assert app.focused.id == "doc-search-input"


async def test_ctrl_f_opens_bar():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "ctrl+f")
        assert _viewer(app).search_active
        assert app.focused.id == "doc-search-input"


async def test_three_chars_do_not_search():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", "g", "d", "b")
        assert _viewer(app).match_count == 0
        assert _count(app) == ""
        assert _search_spans(app) == []


async def test_fourth_char_searches_incrementally():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", *"brea")
        assert _viewer(app).match_count == 1
        assert _count(app) == "1/1"


async def test_enter_searches_short_query():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", *"gdb", "enter")
        viewer = _viewer(app)
        # two in the paragraph, one in the fence, one table cell
        assert viewer.match_count == 4
        assert viewer.current_index == 0
        assert isinstance(app.focused, Markdown)
        assert viewer.search_active


async def test_dropping_below_threshold_clears():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", *"brea")
        assert _viewer(app).match_count == 1
        await _press(pilot, "backspace")
        assert _viewer(app).match_count == 0
        assert _search_spans(app) == []


async def test_n_and_shift_n_wrap():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", *"gdb", "enter")
        viewer = _viewer(app)
        await _press(pilot, "n")
        assert viewer.current_index == 1  # second match, same paragraph
        assert _count(app) == "2/4"
        await _press(pilot, "N", "N")
        assert viewer.current_index == 3  # wrapped backwards
        await _press(pilot, "n")
        assert viewer.current_index == 0  # wrapped forwards


async def test_highlight_applied_and_restored_in_paragraph():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        para = next(
            p for p in app.query(MarkdownParagraph) if "Alpha" in p._content.plain
        )
        original = para._content
        await _press(pilot, "slash", *"gdb", "enter")
        styles = [s.style for s in para.visual.spans]
        assert ".search-current" in styles
        assert ".search-match" in styles
        assert para._content is original
        await _press(pilot, "slash", "escape")
        assert _search_spans(app) == []
        assert para._content is original


async def test_fence_restore_keeps_syntax_spans():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        label = app.query_one(MarkdownFence).query_one("#code-content", Label)
        before = list(label.visual.spans)
        assert before  # syntax highlighting is present
        await _press(pilot, "slash", *"gdb", "enter")
        assert any(
            isinstance(s.style, str) and s.style.startswith(".search-")
            for s in label.visual.spans
        )
        await _press(pilot, "slash", "escape")
        assert list(label.visual.spans) == before


async def test_table_cell_is_highlighted():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", *"gdb", "space", "c")
        cell = next(
            c for c in app.query(MarkdownTableCellContents) if "gdb" in str(c.visual)
        )
        assert ".search-current" in [s.style for s in cell.visual.spans]


async def test_escape_in_input_closes_bar_only():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", "escape")
        assert not _viewer(app).search_active
        assert not app.quit_called
        assert isinstance(app.focused, Markdown)


async def test_escape_with_bar_closed_reaches_app():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "escape")
        assert app.quit_called


async def test_printable_keys_typed_not_bound():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", "q", "n", "slash", "N")
        assert app.query_one("#doc-search-input", Input).value == "qn/N"
        assert not app.quit_called


async def test_no_matches_shows_zero():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", *"zzzzz")
        assert _count(app) == "0/0"
        await _press(pilot, "enter", "n", "N")
        viewer = _viewer(app)
        assert viewer.current_index is None
        assert viewer.match_count == 0


async def test_reopen_starts_clean():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "slash", *"gdb", "enter", "slash", "escape", "slash")
        assert app.query_one("#doc-search-input", Input).value == ""
        assert _viewer(app).match_count == 0
        assert _count(app) == ""
        assert _search_spans(app) == []


async def test_n_without_search_is_noop():
    app = _Host()
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.pause()
        await _press(pilot, "n", "N")
        assert _viewer(app).current_index is None
        assert not app.quit_called
