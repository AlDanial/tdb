"""Base modal for the Threads / Processes / Async Tasks inspection views.

Before this base existed, the three inspection modals each carried
their own ~90-LOC copy of the same skeleton: identical CSS, identical
compose() layout, identical `on_data_table_row_highlighted` /
`on_data_table_row_selected` / `action_dismiss_modal` / `action_refresh`
handlers, identical "header label with `(N)` count" pattern. Changing
the layout (e.g. adding the double-click-to-jump hint) meant three
parallel edits, and a bug in one (like the all-lines-blue text-selection
issue) trivially existed in the other two.

This base lifts the skeleton. Subclasses provide the item-specific
pieces via small overrides (`_format_row`, `_render_loading_detail`,
`_select_id_for`) and define their own `Select*` / `LoadDetail*` /
`Refresh*` messages so the workflow side keeps a typed per-modal
surface. Subclasses that need extra UI (the wait-graph pane on
AsyncTasksModal, the per-pid cache on ProcessesModal) override the
relevant compose / lifecycle hook without disturbing the others.

Standardized widget IDs inside the modal:
  - ``#dialog``       — outer Vertical container
  - ``#header``       — top Label (with the `Kind (N)` count text)
  - ``#footer``       — bottom Label (with the keybinding hints)
  - ``#body``         — horizontal split of list + detail
  - ``#list-pane``    — left column holding the DataTable
  - ``#detail-pane``  — right column holding info + variables
  - ``#table``        — the DataTable itself
  - ``#info``         — Static showing the basic per-item header
  - ``#vars``         — VariableView for the per-item variables

The bare names work without prefixes because only one modal is mounted
at a time and CSS selectors are scoped per class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static

from tdb.widgets.variable_view import VariableView

if TYPE_CHECKING:
    from typing import ClassVar


T = TypeVar("T")


class _InspectableListModal(ModalScreen[None], Generic[T]):
    """Reusable modal with a left-pane DataTable and a right-pane detail view.

    Subclasses provide:
      - `KIND_LABEL` class var: header prefix (e.g. "Threads")
      - `TABLE_COLUMNS` class var: tuple of column headers
      - `FOOTER_HINT` class var: text shown in the bottom label
      - `_items` instance attr: the backing list[T]
      - `_format_row(item)`: returns a tuple of Text cells
      - `_render_loading_detail(item)`: returns the Text shown in #info
        while the detail-pane is loading (or fully populated for items
        that need no async fetch)
      - `_select_id_for(item)`: returns the Hashable to put in the Select
        message, or None if the row should be ignored on double-click
      - `_make_select_message(item_id)`: returns the per-modal Select message
      - `_make_refresh_message()`: returns the per-modal Refresh message
      - `_on_after_show_detail(item)`: optional hook called after the
        initial detail rendering — typical use is to post a LoadDetail
        message that triggers the workflow to fetch frames + scopes

    Optional overrides:
      - `_compose_body()`: add extra panes alongside the list/detail
      - `_initial_cursor_index()`: open with cursor on a specific row
      - `_after_populate_table()`: extra decoration after `_populate_table`
    """

    DEFAULT_CSS = """
    _InspectableListModal {
        align: center middle;
    }
    _InspectableListModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 0;
    }
    _InspectableListModal #header {
        dock: top;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text;
        text-style: bold;
    }
    _InspectableListModal #footer {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text-muted;
    }
    _InspectableListModal #body {
        height: 1fr;
    }
    _InspectableListModal #list-pane {
        width: 2fr;
        border-right: solid $primary;
    }
    _InspectableListModal #detail-pane {
        width: 3fr;
        overflow-y: auto;
    }
    _InspectableListModal #info {
        padding: 1 2;
        height: auto;
        max-height: 50%;
    }
    _InspectableListModal #vars {
        height: 1fr;
        padding: 0 1;
    }
    _InspectableListModal DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("r", "request_refresh", "Refresh", show=False),
    ]

    # Subclass overrides (declared here to surface the contract).
    if TYPE_CHECKING:
        KIND_LABEL: ClassVar[str]
        TABLE_COLUMNS: ClassVar[tuple[str, ...]]
        FOOTER_HINT: ClassVar[str]

    _items: list[T]

    # --- Compose / mount ---------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._header_text(), id="header")
            with Horizontal(id="body"):
                yield from self._compose_body()
            yield Label(self.FOOTER_HINT, id="footer")

    def _compose_body(self) -> ComposeResult:
        """Default body: list pane + detail pane. Override to add panes."""
        with Vertical(id="list-pane"):
            table = DataTable(id="table")
            table.cursor_type = "row"
            yield table
        with Vertical(id="detail-pane"):
            yield Static("", id="info")
            yield VariableView(id="vars")

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns(*self.TABLE_COLUMNS)
        if self._items:
            self._populate_table()
            self._after_populate_table()
            idx = self._initial_cursor_index()
            table.move_cursor(row=idx)
            self._show_detail(idx)
        else:
            self._render_empty_state()

    # --- Subclass-overridable behavior --------------------------------

    def _format_row(self, item: T) -> tuple:
        """Return a tuple of cells (Text or str) for one DataTable row."""
        raise NotImplementedError

    def _render_loading_detail(self, item: T) -> Text:
        """Return the Text shown in #info before the async detail loads.

        Subclasses that don't load asynchronously can return the full
        rendering here and leave `_on_after_show_detail` as the default
        no-op.
        """
        raise NotImplementedError

    def _select_id_for(self, item: T):
        """Return the value to ship in the Select message, or None to
        ignore double-click for this row (e.g., a dead process)."""
        raise NotImplementedError

    def _make_select_message(self, item_id) -> Message:
        raise NotImplementedError

    def _make_refresh_message(self) -> Message:
        raise NotImplementedError

    def _on_after_show_detail(self, item: T) -> None:
        """Called after the loading-detail is rendered. Default no-op.
        Typically subclasses post a `LoadDetail*` message here.
        """
        return None

    def _initial_cursor_index(self) -> int:
        """Index of the row to highlight on first mount. Default 0."""
        return 0

    def _after_populate_table(self) -> None:
        """Hook for extra setup after the table is populated (e.g.,
        deadlock-cycle decoration, header suffix updates). Default no-op.
        """
        return None

    def _empty_state_text(self) -> str:
        """Text shown in #info when the items list is empty."""
        return f"No {self.KIND_LABEL.lower()} found"

    def _header_text(self) -> str:
        """Header label text, including the `(N)` count."""
        return f"{self.KIND_LABEL} ({len(self._items)})"

    # --- Public read-only accessors for workflow callers --------------

    @property
    def items(self) -> list[T]:
        """The current items list. Read-only by convention — callers
        that need to refresh should go through the subclass's update_*
        method so the table and detail pane stay in sync.
        """
        return self._items

    @property
    def has_items(self) -> bool:
        return bool(self._items)

    # --- Common mechanics --------------------------------------------

    def _populate_table(self) -> None:
        """Refresh the DataTable from `self._items` via `_format_row`."""
        table = self.query_one("#table", DataTable)
        table.clear()
        for item in self._items:
            table.add_row(*self._format_row(item))

    def _show_detail(self, index: int) -> None:
        """Render the loading-detail for items[index] and let the
        subclass post any follow-up Load message."""
        if not (0 <= index < len(self._items)):
            return
        item = self._items[index]
        info = self.query_one("#info", Static)
        info.update(self._render_loading_detail(item))
        # Clear variables while loading. Subclasses that re-populate the
        # var view themselves in `_on_after_show_detail` overwrite this.
        var_view = self.query_one("#vars", VariableView)
        var_view.clear()
        self._on_after_show_detail(item)

    def _render_empty_state(self) -> None:
        info = self.query_one("#info", Static)
        info.update(Text(self._empty_state_text(), style="dim"))
        var_view = self.query_one("#vars", VariableView)
        var_view.clear()

    def _update_header(self) -> None:
        """Re-render the header label after `self._items` changes."""
        header = self.query_one("#header", Label)
        header.update(self._header_text())

    def _reload_after_items_change(self) -> None:
        """Repaint after `self._items` has been swapped. Call from
        subclass `update_*` methods after they've stored the new list.
        """
        self._update_header()
        if not self._items:
            self._render_empty_state()
            return
        self._populate_table()
        self._after_populate_table()
        self._show_detail(0)

    # --- Event handlers ----------------------------------------------

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted,
    ) -> None:
        if event.cursor_row is not None:
            self._show_detail(event.cursor_row)

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected,
    ) -> None:
        # Fires on Enter or double-click.
        row = event.cursor_row
        if row is None or not (0 <= row < len(self._items)):
            return
        item_id = self._select_id_for(self._items[row])
        if item_id is None:
            return  # subclass said "ignore this row" (e.g., dead process)
        self.post_message(self._make_select_message(item_id))

    # --- Actions ------------------------------------------------------

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_request_refresh(self) -> None:
        """Post the subclass's Refresh message. Workflow side fetches a
        fresh list and calls back via the subclass's `update_*` method.
        """
        self.post_message(self._make_refresh_message())
