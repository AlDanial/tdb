"""Modal screens used by the main TdbApp.

Each modal is self-contained: it takes its inputs via __init__ and
returns a result via dismiss(). The App is responsible for composing
behavior around them (callbacks, follow-on actions). Splitting these
out of app.py keeps the App as a thin composition root.

The leading underscore on each class name is preserved from when they
lived inside app.py — these are internal collaborators, not part of any
public API. They get re-exported from app.py for any pre-existing test
that imports them by name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DirectoryTree,
    Label,
    RadioButton,
    RadioSet,
    Static,
)

from tdb.keybindings import KeybindingConfig, Mode


class _KeybindingsModal(ModalScreen[None]):
    """Modal showing the keybinding reference for both modes.

    Includes a scheme selector (vim / emacs / default); choosing a scheme
    updates the CodeView live and persists the choice to config.
    """

    DEFAULT_CSS = """
    _KeybindingsModal {
        align: center middle;
    }
    _KeybindingsModal #dialog {
        width: 62;
        height: 80%;
        max-height: 40;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    _KeybindingsModal #scheme-select {
        border: none;
        padding: 0;
        margin: 0 0 1 0;
        height: auto;
    }
    _KeybindingsModal RadioButton {
        border: none;
        padding: 0 1;
    }
    _KeybindingsModal #bindings-scroll {
        height: 1fr;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
    ]

    _SCHEMES = ("vim", "emacs", "default")

    def __init__(
        self,
        config: KeybindingConfig,
        on_scheme_change: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._config = config
        self._on_scheme_change = on_scheme_change

    def compose(self):
        with Vertical(id="dialog"):
            yield Static("[bold]Keybindings[/bold]  (ESC toggles mode)", markup=True)
            yield Static("Scheme:", markup=True)
            with RadioSet(id="scheme-select"):
                for scheme in self._SCHEMES:
                    yield RadioButton(
                        scheme.capitalize(),
                        value=(self._config.scheme == scheme),
                        id=f"scheme-{scheme}",
                    )
            with VerticalScroll(id="bindings-scroll"):
                yield Static(
                    self._render_bindings(), id="bindings-display", markup=True
                )

    def _render_bindings(self) -> str:
        def fmt(key_display: str, description: str) -> str:
            # Pad before escaping so the visible column width stays at 12;
            # \[ renders as one character, so visual alignment is preserved.
            key_padded = f"{key_display:<12}"
            key_escaped = key_padded.replace("[", r"\[")
            desc_escaped = description.replace("[", r"\[")
            return f"  [bold cyan]{key_escaped}[/bold cyan] {desc_escaped}"

        lines = []
        lines.append("[bold underline]Navigation Mode[/bold underline]")
        for key_display, description in self._config.format_bindings(Mode.NAVIGATION):
            lines.append(fmt(key_display, description))
        lines.append("")
        lines.append("[bold underline]Debug Mode[/bold underline]")
        for key_display, description in self._config.format_bindings(Mode.DEBUG):
            lines.append(fmt(key_display, description))
        lines.append("")
        lines.append("[dim]Press ESC or q to close[/dim]")
        return "\n".join(lines)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "scheme-select" or event.pressed is None:
            return
        pressed_id = event.pressed.id or ""
        if not pressed_id.startswith("scheme-"):
            return
        scheme = pressed_id[len("scheme-") :]
        if scheme == self._config.scheme:
            return
        self._config = KeybindingConfig.from_scheme(scheme)
        self._on_scheme_change(scheme)
        self.query_one("#bindings-display", Static).update(self._render_bindings())

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class _StepModeModal(ModalScreen[None]):
    """Choose how `n` (step-over) and `s` (step-in) traverse multi-line code.

    "Statement" loops the step until execution leaves the current source
    statement (so stepping over a multi-line call lands on the next
    statement, not the next sub-line). "Line" is debugpy's native per-line
    behavior. The selection is persisted via the caller's callback.
    """

    DEFAULT_CSS = """
    _StepModeModal {
        align: center middle;
    }
    _StepModeModal #dialog {
        width: 60;
        height: auto;
        max-height: 18;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    _StepModeModal #mode-select {
        border: none;
        padding: 0;
        margin: 0 0 1 0;
        height: auto;
    }
    _StepModeModal RadioButton {
        border: none;
        padding: 0 1;
    }
    _StepModeModal #explainer {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
    ]

    _MODES = ("statement", "line")

    def __init__(
        self,
        current_mode: str,
        on_mode_change: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._current_mode = (
            current_mode if current_mode in self._MODES else "statement"
        )
        self._on_mode_change = on_mode_change

    def compose(self):
        with Vertical(id="dialog"):
            yield Static("[bold]Step Mode[/bold]", markup=True)
            yield Static("", markup=True)
            with RadioSet(id="mode-select"):
                for mode in self._MODES:
                    yield RadioButton(
                        mode.capitalize(),
                        value=(self._current_mode == mode),
                        id=f"mode-{mode}",
                    )
            yield Static(
                "[dim]Statement: `n` / `s` execute the entire multi-line\n"
                "statement under the cursor as one step.\n"
                "Line: stop on each physical line, including sub-lines of a\n"
                "multi-line expression (debugpy default).[/dim]",
                id="explainer",
                markup=True,
            )
            yield Static("", markup=True)
            yield Static("[dim]Press ESC or q to close[/dim]", markup=True)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id != "mode-select" or event.pressed is None:
            return
        pressed_id = event.pressed.id or ""
        if not pressed_id.startswith("mode-"):
            return
        mode = pressed_id[len("mode-") :]
        if mode == self._current_mode:
            return
        self._current_mode = mode
        self._on_mode_change(mode)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class _PyFileTree(DirectoryTree):
    """DirectoryTree that only shows directories and .py files."""

    def filter_paths(self, paths):
        return [p for p in paths if p.is_dir() or p.suffix == ".py"]


class _OpenFileModal(ModalScreen[str | None]):
    """Modal file picker for selecting a .py file to debug."""

    DEFAULT_CSS = """
    _OpenFileModal {
        align: center middle;
    }
    _OpenFileModal #dialog {
        width: 80%;
        height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    _OpenFileModal DirectoryTree {
        height: 1fr;
    }
    _OpenFileModal DirectoryTree > .directory-tree--extension {
        text-style: none;
    }
    _OpenFileModal #open-header {
        height: auto;
    }
    _OpenFileModal #open-path {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    _OpenFileModal #up-dir {
        height: auto;
        margin-bottom: 1;
        padding: 0 1;
        background: $primary-background;
        color: $text;
    }
    _OpenFileModal #up-dir:hover {
        background: $accent;
    }
    _OpenFileModal #open-footer {
        height: auto;
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("backspace", "go_up", "Parent", show=False),
    ]

    def __init__(self, initial_path: str) -> None:
        super().__init__()
        self._current_path = Path(initial_path).resolve()

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(
                "[bold]Open file to debug[/bold]  (.py files only)",
                id="open-header",
                markup=True,
            )
            yield Static(str(self._current_path), id="open-path")
            yield Label(" ⬆  .. (parent directory) ", id="up-dir")
            yield _PyFileTree(str(self._current_path), id="file-tree")
            yield Static(
                "[dim]Enter: open   Backspace: up a directory   ESC: cancel[/dim]",
                id="open-footer",
                markup=True,
            )

    def on_mount(self) -> None:
        self.query_one("#file-tree", _PyFileTree).focus()

    def on_directory_tree_file_selected(
        self,
        event: DirectoryTree.FileSelected,
    ) -> None:
        path = Path(event.path)
        if path.suffix == ".py":
            self.dismiss(str(path))

    def on_click(self, event) -> None:
        target = getattr(event, "widget", None)
        if target is not None and target.id == "up-dir":
            self.action_go_up()
            event.stop()

    def action_go_up(self) -> None:
        parent = self._current_path.parent
        if parent == self._current_path:
            return
        self._current_path = parent
        tree = self.query_one("#file-tree", _PyFileTree)
        tree.path = str(parent)
        tree.reload()
        tree.focus()
        self.query_one("#open-path", Static).update(str(parent))

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


# Default modal header, used when a call site doesn't supply one (the
# live DAP "stopped: exception" path, which is Python/debugpy-only
# today) and as the fallback for `_TracebackModal.__init__`'s `header`
# parameter.
DEFAULT_TRACEBACK_HEADER = "Traceback (most recent call last):"


class _TracebackModal(ModalScreen[str | None]):
    """Scrollable modal showing a full exception traceback."""

    DEFAULT_CSS = """
    _TracebackModal {
        align: center middle;
    }
    _TracebackModal #dialog {
        width: 90%;
        height: 80%;
        border: solid $error;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("enter", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("R", "restart", "Restart", show=False),
    ]

    def __init__(
        self,
        exception_text: str,
        frames_text: str,
        can_restart: bool = True,
        header: str = DEFAULT_TRACEBACK_HEADER,
    ) -> None:
        super().__init__()
        self._exception_text = exception_text
        self._frames_text = frames_text
        self._can_restart = can_restart
        self._header = header

    def compose(self):
        # Escape bare '[' so exception messages like "list[int]" or
        # chained-traceback separator text don't get parsed as Rich markup.
        exc_safe = self._exception_text.replace("[", r"\[")
        frames_safe = self._frames_text.replace("[", r"\[")
        header_safe = self._header.replace("[", r"\[")
        lines = []
        lines.append(f"[bold red]{exc_safe}[/bold red]")
        lines.append("")
        lines.append(f"[bold]{header_safe}[/bold]")
        lines.append(frames_safe)
        lines.append("")
        if self._can_restart:
            footer = "Press ESC, Enter, or q to close · Press R to restart"
        else:
            footer = "Press ESC, Enter, or q to close"
        lines.append(f"[dim]{footer}[/dim]")

        with Vertical(id="dialog"):
            yield Static("\n".join(lines), markup=True)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    def action_restart(self) -> None:
        if self._can_restart:
            self.dismiss("restart")


class _DocumentationModal(ModalScreen):
    """Full-screen markdown viewer for README.md."""

    DEFAULT_CSS = """
    _DocumentationModal {
        align: center middle;
    }
    _DocumentationModal #dialog {
        width: 90%;
        height: 90%;
        border: solid $primary;
        background: $surface;
        padding: 0;
    }
    _DocumentationModal MarkdownViewer {
        height: 1fr;
    }
    _DocumentationModal #doc-footer {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
    ]

    def __init__(self, markdown_text: str) -> None:
        super().__init__()
        self._markdown_text = markdown_text

    def compose(self) -> ComposeResult:
        from textual.widgets import MarkdownViewer

        with Vertical(id="dialog"):
            yield MarkdownViewer(self._markdown_text, show_table_of_contents=True)
            yield Static("[dim]ESC or q to close[/dim]", id="doc-footer", markup=True)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class _AboutModal(ModalScreen):
    """About box: shows version and project info, dismissed with ESC."""

    DEFAULT_CSS = """
    _AboutModal {
        align: center middle;
    }
    _AboutModal #dialog {
        width: auto;
        height: auto;
        min-width: 56;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    _AboutModal #about-body {
        width: auto;
        height: auto;
    }
    _AboutModal #about-footer {
        height: 1;
        color: $text-muted;
        content-align: center middle;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("enter", "dismiss_modal", "Close", show=False),
    ]

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._body, id="about-body", markup=True)
            yield Static("[dim]ESC to close[/dim]", id="about-footer", markup=True)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class _PauseFailedModal(ModalScreen):
    """Shown when controller.pause() times out — explains why and what
    to do. Modal (not a toast) because the message is multi-line and
    the user is likely confused by an unresponsive `p` keypress.
    """

    DEFAULT_CSS = """
    _PauseFailedModal {
        align: center middle;
    }
    _PauseFailedModal #dialog {
        width: auto;
        height: auto;
        max-width: 80;
        min-width: 56;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }
    _PauseFailedModal #pause-title {
        height: 1;
        color: $warning;
        text-style: bold;
        margin-bottom: 1;
    }
    _PauseFailedModal #pause-body {
        height: auto;
        width: auto;
    }
    _PauseFailedModal #pause-footer {
        height: 1;
        color: $text-muted;
        content-align: center middle;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("enter", "dismiss_modal", "Close", show=False),
    ]

    BODY = (
        "[bold]The pause request didn't land.[/bold]\n\n"
        "The debuggee has no Python frames running, so debugpy has\n"
        "nowhere to deliver the pause. The most common cause is a\n"
        "fully-deadlocked asyncio loop blocked in epoll_wait.\n\n"
        "[bold]Workarounds:[/bold]\n"
        "  • For asyncio: add a periodic [italic]await asyncio.sleep(...)[/italic]\n"
        "    task that keeps the event loop ticking.\n"
        "  • Set a breakpoint at a known reachable line (executed by\n"
        "    some task before the deadlock)."
    )

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Pause failed", id="pause-title", markup=True)
            yield Static(self.BODY, id="pause-body", markup=True)
            yield Static("[dim]ESC to close[/dim]", id="pause-footer", markup=True)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class _QuitConfirmModal(ModalScreen):
    """Small modal: `q` confirms quit, escape/other cancels."""

    DEFAULT_CSS = """
    _QuitConfirmModal {
        align: center middle;
    }
    _QuitConfirmModal #dialog {
        width: 42;
        height: 5;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("q", "confirm", "Quit", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(
                "[bold]Hit q again to quit[/bold]     ESC: cancel", markup=True
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class _DetachQuitModal(ModalScreen):
    """Adopted-session quit (tdb --run): detach & resume, or terminate.

    `d`/`q` -> "detach" (default: program keeps running, tdb returns to
    headless run mode), `t` -> "terminate", ESC cancels.
    """

    DEFAULT_CSS = """
    _DetachQuitModal {
        align: center middle;
    }
    _DetachQuitModal #dialog {
        width: 56;
        height: 7;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
        content-align: center middle;
    }
    """

    BINDINGS = [
        Binding("d", "detach", "Detach", show=False),
        Binding("q", "detach", "Detach", show=False),
        Binding("t", "terminate", "Terminate", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("[bold]d[/bold]: detach & resume program", markup=True)
            yield Static("[bold]t[/bold]: terminate program & quit", markup=True)
            yield Static("[dim]ESC: cancel[/dim]", markup=True)

    def action_detach(self) -> None:
        self.dismiss("detach")

    def action_terminate(self) -> None:
        self.dismiss("terminate")

    def action_cancel(self) -> None:
        self.dismiss(None)
