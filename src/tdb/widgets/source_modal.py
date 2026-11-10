"""A source picker with on-demand module resolution and session curation."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import work
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from tdb.source_search import SourceSelection, write_source_list


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
    SourceModal #source-status { height: auto; }
    SourceModal #source-empty { height: 1fr; }
    SourceModal #source-filename { display: none; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Close", show=False),
        Binding("q", "cancel", "Close", show=False),
        Binding("d", "discover", "Find modules", show=False),
        Binding("x", "remove", "Remove", show=False),
        Binding("s", "save", "Save list", show=False),
    ]

    def __init__(
        self,
        paths: list[Path],
        cwd: Path,
        *,
        program: Path | None = None,
        selection: SourceSelection | None = None,
        dynamic_search: Callable[[], list[Path]] | None = None,
    ) -> None:
        super().__init__()
        self._selection = selection if selection is not None else SourceSelection()
        self._available = set(paths) | self._selection.discovered
        self._program = program
        self._paths = self._sorted_paths()
        self._cwd = cwd
        self._dynamic_search = dynamic_search
        self._searching = False

    def _sorted_paths(self) -> list[Path]:
        return sorted(
            self._available - self._selection.excluded,
            key=lambda path: (path != self._program, str(path)),
        )

    def compose(self):
        with Vertical(id="source-dialog"):
            yield Static("Source files", id="source-header")
            yield _SourceList(id="source-list")
            yield Static(
                "No source files. Use d to find modules or add files with --source FILE.",
                id="source-empty",
            )
            yield Static("", id="source-status", markup=False)
            yield Input(
                placeholder="Save source list to filename (Enter to save, Esc to cancel)",
                id="source-filename",
            )
            yield Static(
                "↑/↓ or j/k: move   Enter: open   d: find modules   x: remove\ns: save list   Esc: cancel",
                id="source-footer",
            )

    def on_mount(self) -> None:
        self._refresh_list()
        self.query_one(_SourceList).focus()

    def _refresh_list(self, index: int = 0) -> None:
        self._paths = self._sorted_paths()
        files = self.query_one(_SourceList)
        files.clear_options()
        files.add_options(
            Text("./" + path.relative_to(self._cwd).as_posix())
            if path.is_relative_to(self._cwd)
            else Text(str(path))
            for path in self._paths
        )
        files.display = bool(self._paths)
        self.query_one("#source-empty").display = not self._paths
        files.highlighted = min(index, len(self._paths) - 1) if self._paths else None

    def _status(self, message: str) -> None:
        self.query_one("#source-status", Static).update(message)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not (
            action in {"discover", "remove", "save"} and isinstance(self.focused, Input)
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_index < len(self._paths):
            self.dismiss(str(self._paths[event.option_index]))

    def action_remove(self) -> None:
        files = self.query_one(_SourceList)
        index = files.highlighted
        if index is None or index >= len(self._paths):
            return
        self._selection.excluded.add(self._paths[index])
        self._refresh_list(index)

    def action_discover(self) -> None:
        if self._dynamic_search is None:
            self._status("Dynamic module search is available only for Python sessions.")
        elif not self._searching:
            self._searching = True
            self._status("Searching module files…")
            self._discover()

    @work(group="source-discovery")
    async def _discover(self) -> None:
        try:
            assert self._dynamic_search is not None
            paths = await asyncio.to_thread(self._dynamic_search)
            if not self.is_mounted:
                return
            self._selection.discovered.update(paths)
            self._available.update(paths)
            files = self.query_one(_SourceList)
            highlighted = files.highlighted
            selected = self._paths[highlighted] if highlighted is not None else None
            self._refresh_list()
            if selected in self._paths:
                files.highlighted = self._paths.index(selected)
            self._status(f"Module search complete: {len(self._paths)} source files.")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            if self.is_mounted:
                self._status(f"Cannot search modules: {exc}")
        finally:
            self._searching = False

    def action_save(self) -> None:
        if self._selection.saving:
            self._status("Source list save already in progress.")
            return
        if self._selection.filename is not None:
            self._start_save(self._selection.filename)
        else:
            entry = self.query_one(Input)
            entry.display = True
            entry.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self._selection.saving:
            return
        name = event.value.strip()
        if not name:
            self._status("Enter a filename to save the source list.")
            return
        path = Path(name).expanduser()
        self._start_save(path if path.is_absolute() else self._cwd / path)

    def _start_save(self, filename: Path) -> None:
        # App ownership lets the write and filename bookkeeping finish after
        # this modal is dismissed. Shared state prevents overlapping writers.
        self._selection.saving = True
        self._status("Saving source list…")
        self.app.run_worker(
            self._save(filename, list(self._paths)), group="source-save"
        )

    async def _save(self, filename: Path, paths: list[Path]) -> None:
        app = self.app
        try:
            await asyncio.to_thread(write_source_list, filename, paths)
            self._selection.filename = filename
            if self in app.screen_stack:
                self.query_one(Input).display = False
                self.query_one(_SourceList).focus()
                self._status(f"Saved source list to {filename}")
            else:
                app.notify(f"Saved source list to {filename}")
        except (OSError, ValueError) as exc:
            if self in app.screen_stack:
                self._status(f"Cannot save source list: {exc}")
            else:
                app.notify(f"Cannot save source list: {exc}", severity="error")
        finally:
            self._selection.saving = False

    def action_cancel(self) -> None:
        entry = self.query_one(Input)
        if entry.display:
            entry.display = False
            self.query_one(_SourceList).focus()
        else:
            self.dismiss(None)
