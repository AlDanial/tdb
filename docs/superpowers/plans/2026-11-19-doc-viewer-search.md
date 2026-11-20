# Documentation Viewer Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add find-in-page (highlight, counter, next/previous) to the documentation viewer shown by `tdb --doc` and Help → Documentation.

**Architecture:** A `SearchableMarkdownViewer(MarkdownViewer)` adds a docked, hidden search bar. Matching runs over the rendered text of the `Markdown` document's child widgets (paragraphs/headings, code fences, table cells); matches are highlighted by re-`update()`-ing each widget with a `Content.stylize`d copy of its original content, using component classes declared on the viewer. Navigation scrolls with `scroll_to_widget(..., center=True)`.

**Tech Stack:** Python, Textual 8.2.8 (`MarkdownViewer`, `Content`, `Input`), pytest + `App.run_test` pilot (`asyncio_mode = "auto"`).

**Spec:** `docs/superpowers/specs/2026-11-19-doc-viewer-search-design.md`

## Global Constraints

- Incremental search starts only when the query has **≥ 4 characters**; below 4, typing clears highlights and searches nothing. Enter always searches.
- Smartcase plain-substring matching: case-insensitive unless the query contains an uppercase letter. No regex.
- `/` and `Ctrl+F` open the bar; `n` / `N` next/previous (wrapping); ESC in the input closes the bar only; ESC/`q` with the bar closed close the viewer as today.
- Highlight colors come from theme variables (no hard-coded colors).
- Tests run with `uv run pytest`.

## Review Focus

- Query with zero matches: counter shows `0/0`, nothing scrolls, `n`/`N` are no-ops and don't raise.
- Re-opening the bar after closing it: old highlights are gone and the input starts empty.
- A match inside a code fence keeps the fence's syntax coloring outside the match, and restoring brings the syntax coloring back intact.
- Multiple matches in one block (`n` must step through each, not skip to the next block).
- Opening search, typing `q`, `n`, `/` into the input must type them (Input's `check_consume_key` already filters ancestor bindings for printable keys — the test pins it).

Tests for each of these are in Task 2 (and `q` also in Task 3).

---

### Task 1: Matching function

**Files:**
- Create: `src/tdb/widgets/searchable_markdown.py`
- Test: `tests/unit/test_searchable_markdown.py`

**Interfaces:**
- Produces: `find_matches(text: str, query: str) -> list[tuple[int, int]]` — non-overlapping `(start, end)` offsets into `text`, left to right; `[]` for empty query. Smartcase per Global Constraints.
- Produces: `MIN_INCREMENTAL = 4` module constant.

- [ ] **Step 1: Write the failing tests**

```python
from tdb.widgets.searchable_markdown import MIN_INCREMENTAL, find_matches

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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_searchable_markdown.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tdb.widgets.searchable_markdown'`

- [ ] **Step 3: Implement `find_matches` and `MIN_INCREMENTAL`** in `src/tdb/widgets/searchable_markdown.py` (use `str.find` in a loop on `text`/`query`, lowercased both when `query == query.lower()`).

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_searchable_markdown.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/searchable_markdown.py tests/unit/test_searchable_markdown.py
git commit -m "Add smartcase match finder for doc viewer search"
```

---

### Task 2: `SearchableMarkdownViewer` widget

**Files:**
- Modify: `src/tdb/widgets/searchable_markdown.py`
- Test: `tests/unit/test_searchable_markdown.py`

**Interfaces:**
- Consumes: `find_matches`, `MIN_INCREMENTAL` (Task 1).
- Produces: `class SearchableMarkdownViewer(MarkdownViewer)` — same constructor as `MarkdownViewer`. Public surface used by Task 3 and tests:
  - `action_open_search() -> None`, `action_close_search() -> None`, `action_next_match() -> None`, `action_prev_match() -> None`
  - `search_active: bool` property (bar visible)
  - `match_count: int` and `current_index: int | None` properties (index 0-based)
  - DOM ids: `#doc-search-bar` (container, `display: none` when closed), `#doc-search-input` (`Input`), `#doc-search-count` (`Static`, text `"{current+1}/{count}"`, `"0/0"` for none, empty when no search run).
  - `SEARCH_HINT = "/ or Ctrl+F search · n/N next/prev"` module constant.

Design decisions the implementer cannot infer:
- **Targets** (in document order, from `self.document.query("MarkdownBlock, MarkdownTableCellContents")`), each with an *original-content getter* and an *apply* callback:
  - `MarkdownFence`: getter `block._highlighted_code`, apply `block.query_one("#code-content", Label).update(c)`.
  - `MarkdownTableCellContents`: getter `cell.content` (Static's original), apply `cell.update(c)`.
  - Any other `MarkdownBlock` whose `_content.plain` is non-empty: getter `block._content`, apply `block.update(c)` (do **not** call `set_content`; `_content` must stay the unhighlighted original). Container blocks (lists, blockquotes, tables) have empty `_content` and are skipped; their nested paragraphs are found by the query.
  - Getters are read fresh at search and at restore time (a theme change re-highlights fences; restore must use the current original, not a cached one).
- **Highlight styles:** `COMPONENT_CLASSES = {"search-match", "search-current"}` on the viewer; stylize with `".search-match"` / `".search-current"` (Textual resolves dot-styles via ancestors' component classes). CSS: `SearchableMarkdownViewer > .search-match { background: $warning 40%; }` and `SearchableMarkdownViewer > .search-current { background: $accent; color: $text; text-style: bold; }`.
- **Bar:** `Horizontal#doc-search-bar` docked bottom, height 1, containing a private `_SearchInput(Input)` (placeholder `"Search (Enter to find, Esc to close)"`) whose own `BINDINGS = [Binding("escape", "close", show=False)]` call the viewer's `action_close_search` — the focused input's binding wins over the modal's/app's ESC.
- **Viewer bindings:** `/` and `ctrl+f` → `open_search`; `n` → `next_match`; `N` → `prev_match`.
- **Input events:** `Input.Changed` — if `len(value) >= MIN_INCREMENTAL`, run the search, select match 0 and scroll to it; else clear highlights and the counter. `Input.Submitted` — if the last search wasn't for this exact value, run it (index 0); otherwise advance to next match; then focus `self.document` (bar stays open, highlights stay).
- `next/prev` when no matches: no-op. Wrapping modulo `match_count`.
- `close_search`: restore all touched targets, clear the input, hide the bar, focus `self.document`.
- Scroll: `self.scroll_to_widget(target_widget, center=True, animate=False)`.

Test harness (top of the test module):

~~~python
from textual.app import App
from textual.widgets import Input, Markdown

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
    def compose(self):
        yield SearchableMarkdownViewer(DOC, show_table_of_contents=False)
~~~

Tests run inside `async with _Host().run_test(size=(100, 40)) as pilot:` with `await pilot.pause()` after each press. "Viewer still open" is asserted with `app.is_running`.

- [ ] **Step 1: Write the failing tests**

- `test_slash_opens_bar_and_focuses_input`: press `/` → `viewer.search_active` and `app.focused.id == "doc-search-input"`. Same with `ctrl+f` in `test_ctrl_f_opens_bar`.
- `test_three_chars_do_not_search`: open, type `g`,`d`,`b` → `viewer.match_count == 0`, count text `""`.
- `test_fourth_char_searches_incrementally`: open, type `brea` → `match_count == 1`, count text `"1/1"`.
- `test_enter_searches_short_query`: open, type `gdb`, press `enter` → `match_count == 4` (two in paragraph, one in fence, one table cell), `current_index == 0`, focus is the `Markdown` document, bar still visible.
- `test_dropping_below_threshold_clears`: type `brea` then `backspace` → `match_count == 0`.
- `test_n_and_shift_n_wrap`: after Enter on `gdb`: `n` → index 1 (second match in the same paragraph); `N`,`N` → index 3 (wrapped).
- `test_highlight_applied_and_restored_in_paragraph`: after Enter on `gdb`, the paragraph `MarkdownBlock`'s rendered visual contains a span with style `".search-current"` and one with `".search-match"`; after ESC (refocus input via `/` first, then `escape`) no span has a `.search-` style, and `block._content` is unchanged throughout.
- `test_fence_restore_keeps_syntax_spans`: record the fence `Label`'s content span count before searching; search `gdb`; close; span list equals the original.
- `test_escape_in_input_closes_bar_only`: open, press `escape` → `not viewer.search_active` and `app.is_running`.
- `test_escape_with_bar_closed_reaches_app`: press `escape` with no bar → app exits (`not app.is_running` after pause, or assert via a patched `action_quit` flag).
- `test_printable_keys_typed_not_bound`: open, type `q`,`n`,`/`,`N` → `Input.value == "qn/N"`, app still running.
- `test_no_matches_shows_zero`: type `zzzzz` → count text `"0/0"`; `n` and `N` raise nothing, `current_index is None`.
- `test_reopen_starts_clean`: search `gdb`, close, `/` again → input value `""`, `match_count == 0`, no `.search-` spans anywhere.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_searchable_markdown.py -v`
Expected: new tests FAIL with `ImportError: cannot import name 'SearchableMarkdownViewer'`

- [ ] **Step 3: Implement `SearchableMarkdownViewer`, `_SearchInput`, `SEARCH_HINT`** per the decisions above.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/test_searchable_markdown.py -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/tdb/widgets/searchable_markdown.py tests/unit/test_searchable_markdown.py
git commit -m "Add SearchableMarkdownViewer with incremental search"
```

---

### Task 3: Use it in `tdb --doc` and the documentation modal

**Files:**
- Modify: `src/tdb/cli.py:1230-1257` (`_run_doc`)
- Modify: `src/tdb/widgets/modals.py:441-482` (`_DocumentationModal`)
- Modify: `docs/reference/cli.md:51`, `docs/using/keybindings.md` (after the `Alt+H` table row's following paragraphs — one short paragraph)
- Test: `tests/unit/test_searchable_markdown.py`

**Interfaces:**
- Consumes: `SearchableMarkdownViewer`, `SEARCH_HINT` (Task 2).
- Produces: `make_doc_app(docs: str) -> App` in `src/tdb/cli.py` (module level; `_run_doc` calls `make_doc_app(docs).run()`), so tests can drive the `--doc` app.

- [ ] **Step 1: Write the failing tests**

- `test_doc_app_uses_searchable_viewer`: `app = make_doc_app(DOC)`; in `run_test`, `app.query_one(SearchableMarkdownViewer)` exists; `/`, type `q` → app still running, input value `"q"`; `escape` → bar closed, app running; `escape` again → app exits.
- `test_doc_app_footer_shows_search_binding`: the `--doc` app has a visible binding with key `/` and description `"Search"` (add `Binding("slash", ...)`-equivalent display: give the viewer's `/` binding `show=True, description="Search"`, and keep `n`/`N`/`ctrl+f` `show=False`).
- `test_documentation_modal_search_and_q`: `TdbApp(program="", config=TdbConfig())` with the `_isolate` fixture pattern from `tests/unit/test_theme_picker.py` (no-op `DebugController.start`/`stop`); `monkeypatch.setattr("tdb.documentation.load_docs", lambda: DOC)`; call `app.action_documentation()`; `/`, type `q` → `isinstance(app.screen, _DocumentationModal)`; `escape` → modal still the screen; `escape` → modal dismissed.
- `test_documentation_modal_footer_mentions_search`: `#doc-footer` text contains `SEARCH_HINT` and `"ESC or q to close"`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/test_searchable_markdown.py -v`
Expected: the four new tests FAIL (`ImportError: make_doc_app`, modal assertions)

- [ ] **Step 3: Implement**
  - `cli.py`: extract `_DocApp` into `make_doc_app(docs)`; yield `SearchableMarkdownViewer(docs, show_table_of_contents=True)`.
  - `modals.py`: yield `SearchableMarkdownViewer`; footer text `f"[dim]{SEARCH_HINT} · ESC or q to close[/dim]"`; CSS selector `_DocumentationModal MarkdownViewer` still matches the subclass (leave it).
  - `vi_app.py` needs no change (it pushes the same modal) — confirm by grep.
  - Docs: `cli.md` `--doc` row → append "; `/` or `Ctrl+F` searches, `n`/`N` step through matches". `keybindings.md`: one paragraph: "In **Documentation** (and `tdb --doc`), `/` or `Ctrl+F` opens a search bar. Matches highlight as you type once the query is 4 characters long; `Enter` searches shorter queries and jumps to the next match. `n`/`N` step forward/back, `Esc` closes the search bar. Search is case-insensitive unless the query has an uppercase letter."

- [ ] **Step 4: Run to verify pass, then the full suite**

Run: `uv run pytest tests/unit/test_searchable_markdown.py tests/unit/test_documentation.py tests/unit/test_cli.py -v` → all passed.
Run: `uv run pytest` → no new failures versus `main` (compare any failure against a `main` worktree before calling it pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/tdb/cli.py src/tdb/widgets/modals.py docs/reference/cli.md docs/using/keybindings.md tests/unit/test_searchable_markdown.py
git commit -m "Use searchable viewer for tdb --doc and Help > Documentation"
```
