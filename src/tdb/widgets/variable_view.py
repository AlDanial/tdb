"""Variable view: displays scope variables in a tree."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from textual.binding import Binding
from textual.events import Click
from textual.message import Message
from textual.widgets import Tree
from textual.widgets._tree import TreeNode

from tdb.session.state import DISPLAY_SCOPE_REF

if TYPE_CHECKING:
    from tdb.dap.types import Scope, Variable


class VariableView(Tree[int]):
    """Tree widget showing variables grouped by scope.

    Node data stores the variablesReference for lazy-loading children.
    """

    DEFAULT_CSS = """
    VariableView {
        height: 1fr;
    }
    """

    # Double-click / Enter on a container row opens the Full-Contents
    # modal. Mirror code_view.py's threshold so timing is uniform across
    # the app.
    DOUBLE_CLICK_THRESHOLD = 0.4  # seconds

    # `priority=True` makes this Enter binding win over Tree's default
    # `Binding("enter", "select_cursor")` — otherwise Enter just moves
    # focus / fires NodeSelected and never opens the modal.
    BINDINGS = [
        Binding(
            "enter",
            "show_full_contents",
            "full contents",
            show=False,
            priority=True,
        ),
    ]

    class ShowFullContents(Message):
        """Posted when the user double-clicks (or hits Enter on) a
        container row. The app handler pushes the Full-Contents modal.
        """

        def __init__(self, variables_reference: int, label: str) -> None:
            self.variables_reference = variables_reference
            self.label = label
            super().__init__()

    class DisplayRequested(Message):
        """Posted on right-click of a variable row: add its expression
        to the gdb-style display list."""

        def __init__(self, expression: str) -> None:
            self.expression = expression
            super().__init__()

    class UndisplayRequested(Message):
        """Posted on left-click of a row under the "Display" scope:
        drop its expression from the display list."""

        def __init__(self, expression: str) -> None:
            self.expression = expression
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__("Variables", **kwargs)
        self.border_title = "[bold orange]V[/]ariables"
        self.show_root = False
        self.guide_depth = 3
        self._pending_expand: set[int] = set()
        # Double-click tracking — see DOUBLE_CLICK_THRESHOLD.
        self._last_click_time: float = 0.0
        self._last_click_y: int = -1
        # node.id -> expression to `display` for that row (the DAP
        # evaluateName when the adapter gives one, else the bare name,
        # so a nested attribute displays as e.g. `obj.count`). Scope
        # headers and placeholders have no entry.
        self._node_expr: dict[int, str] = {}

    def update_variables(
        self,
        scopes: list[Scope],
        variables: dict[int, list[Variable]],
    ) -> None:
        """Rebuild the tree with current scope/variable data."""
        self._pending_expand.clear()
        self._node_expr.clear()
        self.clear()
        for scope in scopes:
            scope_node = self.root.add(scope.name, data=scope.variables_reference)
            scope_vars = variables.get(scope.variables_reference, [])
            self._add_variables(scope_node, scope_vars)
            scope_node.expand()

    def _add_variables(self, parent: TreeNode[int], variables: list[Variable]) -> None:
        for var in variables:
            label = self._format_variable(var)
            if var.variables_reference > 0:
                # Has children — add as expandable node
                node = parent.add(label, data=var.variables_reference)
                # Add a placeholder so the node shows as expandable
                node.add_leaf("...")
            else:
                node = parent.add_leaf(label, data=0)
            self._node_expr[node.id] = var.evaluate_name or var.name

    @staticmethod
    def _format_variable(var: Variable) -> str:
        type_str = f" ({var.type})" if var.type else ""
        value = var.value
        if len(value) > 80:
            value = value[:77] + "..."
        return f"{var.name}{type_str} = {value}"

    class VariableExpand:
        """Request to fetch child variables. Posted as a message."""

        def __init__(self, variables_reference: int, node: TreeNode[int]):
            self.variables_reference = variables_reference
            self.node = node

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[int]) -> None:
        """When a node is expanded, check if we need to lazy-load children."""
        node = event.node
        if node.data and node.data > 0 and node.data not in self._pending_expand:
            # Check if the node only has the placeholder child
            children = list(node.children)
            if len(children) == 1 and children[0].label == Text("..."):
                self._pending_expand.add(node.data)
                self.post_message(
                    self.app.LazyLoadVariables(node.data, node, self)  # type: ignore[attr-defined]
                )

    def load_children(self, node: TreeNode[int], variables: list[Variable]) -> None:
        """Replace placeholder children with actual variable data."""
        if node.data:
            self._pending_expand.discard(node.data)
        node.remove_children()
        self._add_variables(node, variables)

    # --- Full-Contents trigger ----------------------------------------

    def _in_display_scope(self, node: TreeNode[int]) -> bool:
        """True when `node` sits under the synthetic "Display" scope."""
        while node.parent is not None and node.parent is not self.root:
            node = node.parent
        return node.data == DISPLAY_SCOPE_REF

    def on_click(self, event: Click) -> None:
        """Route clicks: right-click displays a variable, left-click on a
        Display row undisplays it, a left double-click opens Full
        Contents; anything else falls through to Tree.

        Tree's own `_on_click` will run on every click and handle cursor
        positioning + posting `NodeSelected`. We MUST NOT `event.stop()`
        on the single-click branch or that machinery breaks. Only the
        double-click branch consumes the event.
        """
        line = event.style.meta.get("line")
        node = self.get_node_at_line(line) if line is not None else None
        expr = self._node_expr.get(node.id) if node is not None else None
        if event.button != 1:
            # A right-click must not prime the double-click detector:
            # right-then-left on the same row is two separate clicks.
            self._last_click_time = 0.0
            self._last_click_y = -1
            if event.button == 3 and expr is not None and node is not None:
                if not self._in_display_scope(node):
                    self.post_message(self.DisplayRequested(expr))
            return
        if expr is not None and node is not None and self._in_display_scope(node):
            self._last_click_time = 0.0
            self._last_click_y = -1
            self.post_message(self.UndisplayRequested(expr))
            return
        now = time.monotonic()
        is_double = (
            event.y == self._last_click_y
            and (now - self._last_click_time) < self.DOUBLE_CLICK_THRESHOLD
        )
        if is_double:
            self._last_click_time = 0.0
            self._last_click_y = -1
            self._fire_show_full_contents_at_cursor()
            event.stop()
        else:
            self._last_click_time = now
            self._last_click_y = event.y

    def action_show_full_contents(self) -> None:
        """Enter binding — open Full-Contents modal for the cursor row.

        Falls through to Tree's default `select_cursor` action for non-
        container leaves so plain Enter still feels normal.
        """
        if not self._fire_show_full_contents_at_cursor():
            # Not a container — preserve default Tree behavior (NodeSelected).
            self.action_select_cursor()

    def _fire_show_full_contents_at_cursor(self) -> bool:
        """Post ShowFullContents for the cursor row if it's a container.

        Returns True if a message was posted, False if the cursor row is
        not a container (so the caller can decide on a fallback).
        """
        if self.cursor_line < 0:
            return False
        node = self.get_node_at_line(self.cursor_line)
        if node is None or not node.data or node.data <= 0:
            return False
        # Take the label text; Rich Text → str for the modal title.
        label = str(node.label) if not isinstance(node.label, str) else node.label
        self.post_message(self.ShowFullContents(node.data, label))
        return True


# Fix for Text import used in comparison
from rich.text import Text  # noqa: E402
