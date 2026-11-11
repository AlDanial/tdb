"""`tdb --vi FILE`: a full-screen vim-style editor with no debugger.

A small dedicated App around a standalone CodeView: no controller, no
debug adapter, no subprocess. Leaving Edit mode (`:q`, `:wq`, `:x`,
`:q!`, Ctrl+Q) exits the app; every path but `:q!` goes through the
CodeView's Save / Discard / Cancel prompt when the buffer is dirty.
"""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import OptionList, TextArea

from tdb.keybindings import KeybindingConfig
from tdb.widgets.code_editor import _UnsavedChangesModal
from tdb.widgets.code_view import _BINARY_PROBE_BYTES, CodeView
from tdb.widgets.menu_bar import MenuBar, _MenuDropdown
from tdb.widgets.modals import _AboutModal, _DocumentationModal, _OpenFileModal
from tdb.widgets.vi_status_bar import ViStatusBar


def validate_vi_target(path: str) -> str | None:
    """Why `path` cannot be edited by `tdb --vi`, or None when it can.

    A missing file is fine (it is created on the first `:w`) as long as
    its directory exists. Files Edit mode would refuse — binary or not
    valid UTF-8 — are refused here up front.
    """
    p = Path(path)
    if p.is_dir():
        return f"{path} is a directory"
    if not p.exists():
        parent = os.path.dirname(path) or "."
        if not Path(parent).is_dir():
            return f"{parent}: no such directory"
        return None
    try:
        with open(p, "rb") as f:
            head = f.read(_BINARY_PROBE_BYTES)
            if b"\x00" in head:
                return f"{path} is a binary file"
            raw = head + f.read()
    except OSError as exc:
        return str(exc)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"{path} is not valid UTF-8"
    return None


class ViApp(App):
    """The editor app behind `tdb --vi FILE`."""

    CSS = """
    #code-view {
        height: 1fr;
        width: 1fr;
    }
    """

    # The command palette offers Quit (and an unpersisted theme switch);
    # Ctrl+P belongs to the vi engine here.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        # Overrides App's priority ctrl+q → quit, which would exit without
        # the unsaved-changes prompt.
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        # Textual's ANSI parser turns Alt+F (ESC f) into ctrl+right; bind
        # both, as TdbApp does.
        Binding("alt+f,ctrl+right", "menu_file", "File", show=False),
        Binding("alt+h", "menu_help", "Help menu", show=False),
        # Reached only when focus is outside the editor (an open menu
        # dropdown): close it and return to the editor.
        Binding("escape", "close_menus", "Close menu", show=False),
    ]

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def compose(self) -> ComposeResult:
        yield MenuBar(
            {"Help": ["Documentation", "About"]},
            leading_action_labels={"open-file-label": "File"},
            right_menus=["Help"],
            id="menu-bar",
        )
        view = CodeView(id="code-view")
        view.standalone = True
        yield view
        yield ViStatusBar(id="vi-status")

    async def on_mount(self) -> None:
        from tdb.persist import load_config

        config = load_config()
        if config.theme and config.theme in self.available_themes:
            self.theme = config.theme
        view = self._view
        view.keybindings = KeybindingConfig.from_scheme("vim")
        if Path(self._path).exists():
            view.load_file(self._path)
        else:
            view.load_new_file(self._path)
        await view.enter_edit_mode()
        self.watch(view, "source_path", self._refresh_status)
        self._refresh_status()

    @property
    def _view(self) -> CodeView:
        return self.query_one("#code-view", CodeView)

    # --- Status bar -------------------------------------------------------

    def _refresh_status(self, *_args: object) -> None:
        self.query_one("#vi-status", ViStatusBar).show(self._view)

    def on_code_view_mode_changed(self, message: CodeView.ModeChanged) -> None:
        # Posted on every vi mode / command-line / dirty change.
        self._refresh_status()

    def on_text_area_selection_changed(
        self, message: TextArea.SelectionChanged
    ) -> None:
        self._refresh_status()

    def _focus_editor(self) -> None:
        editor = self._view._editor
        if editor is not None:
            editor.focus()

    # --- Quitting ---------------------------------------------------------

    async def action_quit(self) -> None:
        """Every quit request (Ctrl+Q, Textual's own quit action) leaves
        Edit mode the safe way; EditClosed then exits."""
        if any(isinstance(s, _UnsavedChangesModal) for s in self.screen_stack):
            return  # the prompt is already up
        view = self._view
        if view.is_editing:
            view.leave_edit_mode()
        else:
            self.exit(0)

    def on_code_view_edit_closed(self, message: CodeView.EditClosed) -> None:
        message.stop()
        # A repeated `:q` before the exit lands re-posts EditClosed.
        if self.return_code is None:
            self.exit(0)

    # --- File menu --------------------------------------------------------

    def action_open_file(self) -> None:
        def on_dismiss(path: str | None) -> None:
            if not path:
                self._focus_editor()
                return
            error = validate_vi_target(path)
            if error is not None:
                self.notify(error, title="Open", severity="warning")
                self._focus_editor()
                return
            view = self._view
            view.confirm_unsaved(lambda: view.replace_file(path))

        self.push_screen(
            _OpenFileModal(os.getcwd(), title="Open file to edit"),
            callback=on_dismiss,
        )

    def on_menu_bar_action_label_clicked(
        self, message: MenuBar.ActionLabelClicked
    ) -> None:
        if message.label_id == "open-file-label":
            self.action_open_file()

    def action_menu_file(self) -> None:
        self.action_close_menus(refocus=False)
        self.action_open_file()

    # --- Help menu --------------------------------------------------------

    def action_menu_help(self) -> None:
        self.query_one("#menu-bar", MenuBar).open_menu("Help")

    def action_close_menus(self, refocus: bool = True) -> None:
        self.query_one("#menu-bar", MenuBar).close_menus()
        if refocus:
            self._focus_editor()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if not isinstance(event.option_list, _MenuDropdown):
            return
        event.stop()
        self.action_close_menus()
        if event.option.id == "Help:Documentation":
            self.action_documentation()
        elif event.option.id == "Help:About":
            self.action_about()

    def action_documentation(self) -> None:
        from tdb.documentation import load_docs

        docs = load_docs()
        if docs is None:
            self.notify(
                "Documentation not found in the installation.",
                title="Documentation",
                severity="warning",
            )
            return
        self.push_screen(_DocumentationModal(docs), callback=self._refocus)

    def action_about(self) -> None:
        from tdb.app_helpers import about_text

        self.push_screen(_AboutModal(about_text(markup=True)), callback=self._refocus)

    def _refocus(self, _result: object = None) -> None:
        self._focus_editor()
