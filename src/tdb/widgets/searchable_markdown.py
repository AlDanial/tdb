"""Find-in-page for the documentation viewer (`tdb --doc` and
Help -> Documentation): a MarkdownViewer with a search bar."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.css.query import NoMatches
from textual.geometry import Offset, Region
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Label, Markdown, MarkdownViewer, Static
from textual.widgets._markdown import (
    MarkdownBlock,
    MarkdownFence,
    MarkdownTableCellContents,
    MarkdownTableOfContents,
)

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


SEARCH_HINT = "/ or Ctrl+F search · n/N next/prev"
"""Key hint shown in the documentation modal's footer."""

RESULTS_HINT = "n/→ next · p/←/N previous · q exit search"
"""The footer hint once Enter has put the results in front of the user."""


_Spans = list[tuple[str, int, int]]
"""(style, start, end) highlight spans for one widget's text."""


def _stylized(content: Content, spans: _Spans) -> Content:
    for style, start, end in spans:
        content = content.stylize(style, start, end)
    return content


@dataclass(eq=False)
class _Target:
    """One searchable widget: its unhighlighted text (read fresh each
    time) and how to display it with a set of highlight spans; an empty
    list restores the original."""

    widget: Widget
    get: Callable[[], Content]
    paint: Callable[[_Spans], None]


class _SearchFence(MarkdownFence):
    """A code fence that keeps search highlights across style updates:
    MarkdownFence.notify_style_update re-renders the syntax-highlighted
    code (on every focus or theme change), which would wipe them."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._search_spans: _Spans = []

    def paint_search(self, spans: _Spans) -> None:
        self._search_spans = spans
        with suppress(NoMatches):
            self.query_one("#code-content", Label).update(
                _stylized(self._highlighted_code, spans)
            )

    def notify_style_update(self) -> None:
        super().notify_style_update()
        if self._search_spans:
            self.paint_search(self._search_spans)


_NEXT = Binding.Group("next")
_PREVIOUS = Binding.Group("previous")
# One action per key: Textual's Footer shows a single key per action.
_RESULT_ACTIONS = {
    "next_match": 1,
    "next_match_arrow": 1,
    "prev_match": -1,
    "prev_match_p": -1,
    "prev_match_arrow": -1,
}


class _SearchableMarkdown(Markdown):
    """The document. Its result keys act only while results show (the
    search bar open, focus back here); otherwise check_action disables
    them, so each key falls through to what it does without a search:
    the viewer's arrow scrolling, the host's `q`."""

    BLOCKS = {**Markdown.BLOCKS, "fence": _SearchFence, "code_block": _SearchFence}

    BINDINGS = [
        Binding("n", "next_match", "next", group=_NEXT),
        Binding("right", "next_match_arrow", "next", group=_NEXT),
        Binding("p", "prev_match_p", "previous", group=_PREVIOUS),
        Binding("left", "prev_match_arrow", "previous", group=_PREVIOUS),
        Binding("N", "prev_match", "previous", group=_PREVIOUS),
        Binding("q", "exit_search", "exit search"),
    ]

    def _viewer(self) -> SearchableMarkdownViewer | None:
        return next(
            (a for a in self.ancestors if isinstance(a, SearchableMarkdownViewer)),
            None,
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in _RESULT_ACTIONS or action == "exit_search":
            viewer = self._viewer()
            return viewer is not None and viewer.results_mode
        return super().check_action(action, parameters)

    def _step(self, action: str) -> None:
        viewer = self._viewer()
        if viewer is not None:
            viewer._step(_RESULT_ACTIONS[action])

    def action_next_match(self) -> None:
        self._step("next_match")

    def action_next_match_arrow(self) -> None:
        self._step("next_match_arrow")

    def action_prev_match(self) -> None:
        self._step("prev_match")

    def action_prev_match_p(self) -> None:
        self._step("prev_match_p")

    def action_prev_match_arrow(self) -> None:
        self._step("prev_match_arrow")

    def action_exit_search(self) -> None:
        viewer = self._viewer()
        if viewer is not None:
            viewer.action_close_search()


def _fence_target(fence: _SearchFence) -> _Target:
    return _Target(fence, lambda: fence._highlighted_code, fence.paint_search)


def _cell_target(cell: MarkdownTableCellContents) -> _Target | None:
    content = cell.content
    if not isinstance(content, Content):
        return None
    return _Target(
        cell, lambda: content, lambda spans: cell.update(_stylized(content, spans))
    )


def _block_target(block: MarkdownBlock) -> _Target | None:
    if not block._content.plain:
        # Containers (lists, block quotes, tables): their text lives in
        # nested blocks/cells, which the query finds on their own.
        return None
    return _Target(
        block,
        lambda: block._content,
        lambda spans: block.update(_stylized(block._content, spans)),
    )


class _SearchInput(Input):
    """The search box. Its own ESC binding outranks the modal's / app's
    ESC (focused widget first), so ESC here closes only the search bar."""

    BINDINGS = [Binding("escape", "close", show=False)]

    def action_close(self) -> None:
        viewer = next(
            (a for a in self.ancestors if isinstance(a, SearchableMarkdownViewer)),
            None,
        )
        if viewer is not None:
            viewer.action_close_search()


class SearchableMarkdownViewer(MarkdownViewer):
    """MarkdownViewer with find-in-page over the rendered document."""

    COMPONENT_CLASSES = {"search-match", "search-current"}

    DEFAULT_CSS = """
    SearchableMarkdownViewer > .search-match {
        background: $warning 40%;
    }
    SearchableMarkdownViewer > .search-current {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    SearchableMarkdownViewer > #doc-search-bar {
        dock: bottom;
        height: 1;
        display: none;
        background: $panel;
    }
    SearchableMarkdownViewer > #doc-search-bar.-active {
        display: block;
    }
    SearchableMarkdownViewer #doc-search-input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0 1;
    }
    SearchableMarkdownViewer #doc-search-count {
        width: auto;
        min-width: 7;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("slash", "open_search", "Search"),
        Binding("ctrl+f", "open_search", "Search", show=False),
        Binding("escape", "escape_search", show=False),
    ]
    # n/N and the other result keys live on the document (_SearchableMarkdown).

    class ResultsModeChanged(Message):
        """Results mode began (`active`) or ended: hosts swap footer hints."""

        def __init__(self, active: bool) -> None:
            super().__init__()
            self.active = active

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._matches: list[tuple[_Target, int, int]] = []
        self._current: int | None = None
        self._last_query: str | None = None
        self._touched: list[_Target] = []
        self._results_mode = False

    def compose(self) -> ComposeResult:
        # As MarkdownViewer.compose, but with fences that keep highlights.
        markdown = _SearchableMarkdown(
            parser_factory=self._parser_factory, open_links=self._open_links
        )
        markdown.can_focus = True
        yield markdown
        yield MarkdownTableOfContents(markdown)
        with Horizontal(id="doc-search-bar"):
            yield _SearchInput(
                placeholder="Search (Enter to find, Esc to close)",
                id="doc-search-input",
            )
            yield Static("", id="doc-search-count")

    # -- public state ------------------------------------------------------

    @property
    def search_active(self) -> bool:
        return self.query_one("#doc-search-bar").has_class("-active")

    @property
    def match_count(self) -> int:
        return len(self._matches)

    @property
    def current_index(self) -> int | None:
        return self._current

    @property
    def results_mode(self) -> bool:
        """The search bar is open and focus is back in the document (after
        Enter): the result keys n/→, p/←/N and q are live."""
        return self.search_active and self.document.has_focus

    def _sync_results_mode(self) -> None:
        active = self.results_mode
        if active != self._results_mode:
            self._results_mode = active
            self.refresh_bindings()
            self.post_message(self.ResultsModeChanged(active))

    def on_descendant_focus(self) -> None:
        self._sync_results_mode()

    def on_descendant_blur(self) -> None:
        self._sync_results_mode()

    # -- actions -----------------------------------------------------------

    def action_open_search(self) -> None:
        self.query_one("#doc-search-bar").add_class("-active")
        self.query_one("#doc-search-input", Input).focus()

    def action_close_search(self) -> None:
        self._clear()
        search_input = self.query_one("#doc-search-input", Input)
        with search_input.prevent(Input.Changed):
            search_input.value = ""
        self.query_one("#doc-search-bar").remove_class("-active")
        self.document.focus(scroll_visible=False)
        # Focus may not move (it is often in the document already), so no
        # focus event would end results mode.
        self._sync_results_mode()

    def action_next_match(self) -> None:
        self._step(1)

    def action_prev_match(self) -> None:
        self._step(-1)

    def action_escape_search(self) -> None:
        """ESC with the bar still showing (focus back in the document after
        Enter) closes the bar; otherwise ESC goes on to the modal / app."""
        if not self.search_active:
            raise SkipAction()
        self.action_close_search()

    # -- input events ------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "doc-search-input":
            return
        event.stop()
        if len(event.value) >= MIN_INCREMENTAL:
            self._search(event.value)
        else:
            self._clear()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "doc-search-input":
            return
        event.stop()
        if event.value and event.value == self._last_query:
            self._step(1)
        elif event.value:
            self._search(event.value)
        self.document.focus(scroll_visible=False)

    # -- search machinery --------------------------------------------------

    def _targets(self) -> list[_Target]:
        targets: list[_Target] = []
        for widget in self.document.query("MarkdownBlock, MarkdownTableCellContents"):
            if isinstance(widget, _SearchFence):
                target: _Target | None = _fence_target(widget)
            elif isinstance(widget, MarkdownTableCellContents):
                target = _cell_target(widget)
            elif isinstance(widget, MarkdownBlock):
                target = _block_target(widget)
            else:
                target = None
            if target is not None:
                targets.append(target)
        return targets

    def _search(self, query: str) -> None:
        self._clear()
        self._last_query = query
        for target in self._targets():
            for start, end in find_matches(target.get().plain, query):
                self._matches.append((target, start, end))
        self._touched = list(dict.fromkeys(t for t, _, _ in self._matches))
        self._current = 0 if self._matches else None
        for target in self._touched:
            self._paint(target)
        self._show_current()

    def _clear(self) -> None:
        for target in self._touched:
            target.paint([])
        self._matches = []
        self._touched = []
        self._current = None
        self._last_query = None
        self._update_count()

    def _paint(self, target: _Target) -> None:
        target.paint(
            [
                (
                    ".search-current" if index == self._current else ".search-match",
                    start,
                    end,
                )
                for index, (match_target, start, end) in enumerate(self._matches)
                if match_target is target
            ]
        )

    def _step(self, delta: int) -> None:
        if self._current is None:
            return
        previous = self._matches[self._current][0]
        self._current = (self._current + delta) % len(self._matches)
        current = self._matches[self._current][0]
        self._paint(previous)
        if current is not previous:
            self._paint(current)
        self._show_current()

    def _show_current(self) -> None:
        self._update_count()
        if self._current is not None:
            target, start, _end = self._matches[self._current]
            self._scroll_to_match(target, start)

    def _scroll_to_match(self, target: _Target, start: int) -> None:
        """Center the row holding the match, not the block's top: a block
        can be taller than the viewport. Fences give the exact row (one
        code line per row); wrapped text gets a proportional estimate."""
        widget: Widget = target.widget
        plain = target.get().plain
        if isinstance(widget, _SearchFence):
            with suppress(NoMatches):
                widget = widget.query_one("#code-content", Label)
            row = plain.count("\n", 0, start)
        else:
            row = widget.content_size.height * start // max(len(plain), 1)
        region = widget.virtual_region_with_margin
        if not region.size:
            return
        styles = widget.styles
        row += styles.margin.top + styles.border.spacing.top + styles.padding.top
        region = Region(region.x, region.y + row, region.width, 1)
        # Same container walk as Widget.scroll_to_widget, from that row.
        while isinstance(widget.parent, Widget) and widget is not self:
            if not region:
                break
            container = widget.parent
            if widget.styles.dock != "none":
                offset = Offset(0, 0)
            else:
                offset = container.scroll_to_region(
                    region, spacing=widget.dock_gutter, animate=False, center=True
                )
            region = (
                region.translate(-offset)
                .translate(container.styles.margin.top_left)
                .translate(container.styles.border.spacing.top_left)
                .translate(container.virtual_region_with_margin.offset)
                .grow(container.styles.margin)
                .intersection(container.virtual_region_with_margin)
            )
            widget = container

    def _update_count(self) -> None:
        if self._last_query is None:
            text = ""
        elif self._current is None:
            text = "0/0"
        else:
            text = f"{self._current + 1}/{len(self._matches)}"
        self.query_one("#doc-search-count", Static).update(text)
