"""Modal that displays the full nested contents of a variable.

Opens when the user double-clicks (or presses Enter on) a container row
in the main Variables View. The chosen variable's subtree is pre-fetched
breadth-first by `tdb.inspection_full.bfs_load_full` and rendered here as
a fully-expanded `Tree` so the user doesn't have to chevron-expand every
nested `{...}` placeholder one by one.

Branches that exceed the BFS's depth/items caps surface as `"… N more"`
leaf placeholders whose `data` is a `("more", ref, next_start)` tuple;
activating one calls `client.variables(ref, start=next_start, count=N)`
and splices the result in place. The same mechanic handles the
depth-cap case (`more=(ref, 0)`) — pressing Enter at the leaf fetches
one more level on demand.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode

if TYPE_CHECKING:
    from tdb.inspection_full import FullContentsNode

log = logging.getLogger(__name__)

_PAGE_SIZE = 500


class FullContentsModal(ModalScreen[None]):
    """Scrollable modal showing the full nested contents of a variable.

    Lifecycle:
      1. App pushes the modal in a "Loading…" state.
      2. A background worker awaits the BFS and calls `populate(tree)`.
      3. The Tree is fully expanded; the user can dismiss with ESC/q,
         or activate "load more" placeholders to paginate deeper.
    """

    DEFAULT_CSS = """
    FullContentsModal {
        align: center middle;
    }
    FullContentsModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    FullContentsModal #title {
        height: auto;
        padding-bottom: 1;
    }
    FullContentsModal #status {
        height: auto;
        color: $text-muted;
    }
    FullContentsModal #contents-tree {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
    ]

    def __init__(self, label: str) -> None:
        super().__init__()
        self._label = label

    def compose(self) -> ComposeResult:
        # Escape `[` so labels like "x (list[int])" don't get parsed as markup.
        safe = self._label.replace("[", r"\[")
        with Vertical(id="dialog"):
            yield Static(f"[bold]{safe}[/bold]", id="title", markup=True)
            yield Static("Loading…", id="status")
            tree: Tree[Any] = Tree(self._label, id="contents-tree")
            tree.show_root = False
            yield tree

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    # --- Population ----------------------------------------------------

    def populate(self, root: "FullContentsNode") -> None:
        """Replace the Loading… placeholder with the materialized tree.

        Safe to call after the user has already dismissed: a stale call
        from the background worker silently no-ops.
        """
        if not self.is_attached:
            return
        try:
            status = self.query_one("#status", Static)
        except Exception:
            # Already populated once.
            return
        status.remove()
        tree = self.query_one("#contents-tree", Tree)
        tree.clear()
        self._materialize(tree.root, root)
        tree.root.expand_all()

    def _materialize(self, parent_node: TreeNode, fc: "FullContentsNode") -> None:
        if fc.truncated_by_budget and not fc.children:
            parent_node.add_leaf(
                "… (truncated — total-node budget exceeded)",
                data=("budget", fc.ref),
            )
            return
        for child in fc.children:
            if child.ref > 0 and (child.children or child.more is not None):
                node = parent_node.add(child.label, data=child.ref)
                self._materialize(node, child)
            elif child.ref > 0:
                # Container with no children fetched — emit a "load one
                # level" sentinel so the user can drill in on demand.
                node = parent_node.add(child.label, data=child.ref)
                node.add_leaf("… (Enter to load)", data=("more", child.ref, 0))
            else:
                parent_node.add_leaf(child.label, data=0)
        if fc.more is not None:
            ref, next_start = fc.more
            if next_start == 0:
                parent_node.add_leaf("… (Enter to load)", data=("more", ref, 0))
            else:
                parent_node.add_leaf(
                    f"… more items (Enter to load next {_PAGE_SIZE})",
                    data=("more", ref, next_start),
                )

    # --- "Load more" pagination ---------------------------------------

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Activate the "load more" placeholder under the cursor."""
        data = event.node.data
        if isinstance(data, tuple) and data and data[0] == "more":
            _, ref, start = data
            await self._load_more(event.node, ref, start)

    async def _load_more(
        self,
        placeholder: TreeNode,
        ref: int,
        start: int,
    ) -> None:
        """Fetch one page (or initial expansion) under `ref` and splice in.

        For start==0 this is "expand a depth-capped node" (replace the
        single placeholder with the actual children). For start>0 this is
        "next page of siblings"; the new items get inserted as siblings
        of the placeholder, and the placeholder is regenerated if more
        items remain.
        """
        ctrl = getattr(self.app, "controller", None)
        if ctrl is None:
            return
        client = ctrl.active_client
        try:
            raw_page = await client.variables(ref, start=start, count=_PAGE_SIZE)
        except Exception:
            log.debug("load_more: failed to fetch ref=%d start=%d", ref, start)
            placeholder.remove()
            return
        # Same filter the BFS uses — keeps methods/dunders out of the
        # tree when the user expands a dataclass/object on demand.
        from tdb.inspection_full import _filter_children

        page = _filter_children(raw_page)

        parent = placeholder.parent
        if parent is None:
            return

        if start == 0:
            # Depth-cap expansion: placeholder is a child of the container
            # node itself. Replace the placeholder with the fetched vars.
            container = parent
            placeholder.remove()
            for var in page:
                self._add_variable(container, var)
            if len(raw_page) == _PAGE_SIZE:
                container.add_leaf(
                    f"… more items (Enter to load next {_PAGE_SIZE})",
                    data=("more", ref, _PAGE_SIZE),
                )
        else:
            # Pagination: placeholder is a sibling of the prior page's
            # items. Add new items as siblings; replace placeholder.
            placeholder.remove()
            for var in page:
                self._add_variable(parent, var)
            if len(raw_page) == _PAGE_SIZE:
                parent.add_leaf(
                    f"… more items (Enter to load next {_PAGE_SIZE})",
                    data=("more", ref, start + _PAGE_SIZE),
                )

    def _add_variable(self, parent_node: TreeNode, var: Any) -> None:
        """Add a single Variable as a Tree node, mirroring VariableView."""
        # Lazy import to keep this module Textual-only at import time.
        from tdb.inspection_full import _format_label

        label = _format_label(var)
        if var.variables_reference > 0:
            node = parent_node.add(label, data=var.variables_reference)
            # Lazy-expand placeholder so the user can drill further if
            # interested; on activation it'll hit our load_more path.
            node.add_leaf(
                "… (Enter to load)", data=("more", var.variables_reference, 0)
            )
        else:
            parent_node.add_leaf(label, data=0)
