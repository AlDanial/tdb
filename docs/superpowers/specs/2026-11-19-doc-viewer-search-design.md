# Documentation viewer search — design

## Intent

`tdb --doc` and the in-TUI Help → Documentation modal show the bundled docs in
Textual's `MarkdownViewer`, which has no search. Users need to find text in the
docs without scrolling through the table of contents.

## Goals / non-goals

- Goal: find-in-page with match highlighting and next/previous navigation, in
  both viewer call sites (`cli._run_doc` and `widgets/modals._DocumentationModal`,
  used by `app.py` and `vi_app.py`).
- Non-goals: regex search, searching the TOC sidebar, cross-session history.
- Anti-goal: breaking the existing ESC/`q` close behaviour, or letting typed
  characters (e.g. `q`) trigger viewer bindings while the search box has focus.

## Approaches

1. **Search rendered blocks (chosen).** Subclass `MarkdownViewer`; walk the
   `Markdown` document's `MarkdownBlock` children, match against each block's
   rendered `_content.plain`, highlight with `Content.stylize`, scroll with
   `scroll_to_widget`. Matches what the reader sees; reuses the TOC jump path.
2. Search raw markdown source, map line → block via `source_range`. Rejected:
   markdown syntax and link URLs cause false misses/hits, and highlighting
   still needs approach 1's machinery.
3. Re-render with injected markup. Rejected: loses scroll position, corrupts
   code fences.

## Decided behaviour

- New widget `SearchableMarkdownViewer` (in `src/tdb/widgets/`) used by both
  call sites, plus a docked, initially hidden search bar (`Input` + match
  counter like `3/17`).
- `/` or `Ctrl+F` opens the bar and focuses the input.
- **Incremental search starts only once the query has ≥ 4 characters.**
  Below 4, typing does nothing live and any existing highlights are cleared.
  Pressing Enter always searches, so short terms (`gdb`, `-r`) still work.
- Enter: search (if not already live), jump to the next match, return focus to
  the document while keeping highlights.
- `n` / `N` (document focused, search active): next / previous match, wrapping.
- ESC in the search input: close the bar and clear highlights; the viewer stays
  open. ESC/`q` with the bar closed: close the viewer as today.
- Footer hints updated (modal footer text and `--doc` app `Footer` bindings).

## Builder's calls (proposed, correctable)

- Smartcase: case-insensitive unless the query contains an uppercase letter.
  Plain substring matching.
- All matches get a muted highlight; the current match a stronger one (theme
  variables, so it follows the active theme).
- Block types needing special handling: `MarkdownFence` (text lives in its
  `Label`, highlighted Content), table cells (`MarkdownTableCellContents`
  Statics inside `MarkdownTable`). Nested list/blockquote paragraphs are
  ordinary mounted `MarkdownBlock`s and are found by `query`.
- Original `Content` is cached per block and restored on clear; theme changes
  (fence re-highlight via `notify_style_update`) must not leave stale
  highlights.
- Scrolling: bring the matching block into view (centered); finer line-level
  positioning is optional.
- Tests: Textual pilot tests covering the 4-char threshold, Enter on short
  queries, n/N wrap, ESC closing only the bar, `q` typed into the input, and
  both call sites. Run with `uv run pytest`.
