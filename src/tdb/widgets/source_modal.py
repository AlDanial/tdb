"""An eagerly expanded tree containing only the program's source files."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static, Tree


class _SourceTree(Tree[Path]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


class SourceModal(ModalScreen[str | None]):
    DEFAULT_CSS = """
    SourceModal { align: center middle; }
    SourceModal #source-dialog {
        width: 80%;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    SourceModal Tree { height: 1fr; }
    SourceModal #source-header { height: auto; margin-bottom: 1; }
    SourceModal #source-footer { height: auto; margin-top: 1; color: $text-muted; }
    SourceModal #source-empty { height: 1fr; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("q", "cancel", "Close", show=False),
    ]

    def __init__(self, paths: list[Path], cwd: Path) -> None:
        super().__init__()
        self._paths = sorted(set(paths))
        self._cwd = cwd

    def compose(self):
        with Vertical(id="source-dialog"):
            yield Static("Source files", id="source-header")
            if self._paths:
                root = self._cwd
                if not all(path.is_relative_to(root) for path in self._paths):
                    root = next(
                        (
                            parent
                            for parent in self._paths[0].parents
                            if all(path.is_relative_to(parent) for path in self._paths)
                        ),
                        None,
                    )
                label = (
                    "." if root == self._cwd else str(root) if root else "Source files"
                )
                tree = _SourceTree(Text(label), id="source-tree")
                tree.auto_expand = False
                directories = {root: tree.root}
                # A synthetic root accommodates files on separate Windows drives.
                parents = {
                    parent
                    for path in self._paths
                    for parent in path.parents
                    if parent != root and (root is None or parent.is_relative_to(root))
                }
                for parent in sorted(parents, key=lambda p: (len(p.parts), str(p))):
                    container = None if parent.parent == parent else parent.parent
                    directories[parent] = directories[container].add(
                        Text(parent.name or str(parent)), expand=True
                    )
                for path in self._paths:
                    directories[path.parent].add_leaf(Text(path.name), data=path)
                tree.root.expand()
                yield tree
            else:
                yield Static(
                    "No local source files found. Add files with --source=FILE.",
                    id="source-empty",
                )
            yield Static(
                "↑/↓ or j/k: move   Enter: open / toggle directory   Esc: cancel",
                id="source-footer",
            )

    def on_mount(self) -> None:
        if self._paths:
            self.query_one(_SourceTree).focus()

    def on_tree_node_selected(self, event: Tree.NodeSelected[Path]) -> None:
        event.stop()
        if event.node.data is not None:
            self.dismiss(str(event.node.data))
        else:
            event.node.toggle()

    def action_cancel(self) -> None:
        self.dismiss(None)
