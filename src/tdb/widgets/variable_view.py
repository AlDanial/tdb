"""Variable view: displays scope variables in a tree."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Tree
from textual.widgets._tree import TreeNode

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

    def __init__(self, **kwargs) -> None:
        super().__init__("Variables", **kwargs)
        self.border_title = "[bold orange]V[/]ariables"
        self.show_root = False
        self.guide_depth = 3
        self._pending_expand: set[int] = set()

    def update_variables(
        self,
        scopes: list[Scope],
        variables: dict[int, list[Variable]],
    ) -> None:
        """Rebuild the tree with current scope/variable data."""
        self._pending_expand.clear()
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
                parent.add_leaf(label, data=0)

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


# Fix for Text import used in comparison
from rich.text import Text  # noqa: E402
