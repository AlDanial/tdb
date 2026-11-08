"""A flat picker containing only the program's source files."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static


class _SourceList(OptionList):
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
    SourceModal OptionList { height: 1fr; }
    SourceModal #source-header { height: auto; margin-bottom: 1; }
    SourceModal #source-footer { height: auto; margin-top: 1; color: $text-muted; }
    SourceModal #source-empty { height: 1fr; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("q", "cancel", "Close", show=False),
    ]

    def __init__(
        self, paths: list[Path], cwd: Path, *, program: Path | None = None
    ) -> None:
        super().__init__()
        self._paths = sorted(set(paths), key=lambda path: (path != program, str(path)))
        self._cwd = cwd

    def compose(self):
        with Vertical(id="source-dialog"):
            yield Static("Source files", id="source-header")
            if self._paths:
                yield _SourceList(
                    *(
                        Text("./" + path.relative_to(self._cwd).as_posix())
                        if path.is_relative_to(self._cwd)
                        else Text(str(path))
                        for path in self._paths
                    ),
                    id="source-list",
                )
            else:
                yield Static(
                    "No local source files found. Add files with --source=FILE.",
                    id="source-empty",
                )
            yield Static(
                "↑/↓ or j/k: move   Enter: open   Esc: cancel",
                id="source-footer",
            )

    def on_mount(self) -> None:
        if self._paths:
            self.query_one(_SourceList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(str(self._paths[event.option_index]))

    def action_cancel(self) -> None:
        self.dismiss(None)
