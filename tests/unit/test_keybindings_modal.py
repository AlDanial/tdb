"""Keybindings dialog: Notepad-style label for the `default` scheme
and the Edit Mode section."""

from __future__ import annotations

from textual.app import App
from textual.widgets import RadioButton

from tdb.keybindings import KeybindingConfig
from tdb.widgets.modals import _KeybindingsModal


class _App(App):
    def __init__(self, scheme: str):
        super().__init__()
        self._scheme = scheme
        self.chosen: list[str] = []

    def on_mount(self):
        self.push_screen(
            _KeybindingsModal(
                KeybindingConfig.from_scheme(self._scheme), self.chosen.append
            )
        )


async def test_default_scheme_is_labelled_notepad_style():
    app = _App("default")
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = [str(rb.label) for rb in app.screen.query(RadioButton)]
        assert labels == ["Vim", "Emacs", "Notepad-style"]
        pressed = [rb for rb in app.screen.query(RadioButton) if rb.value]
        assert pressed and pressed[0].id == "scheme-default"


async def test_edit_mode_section_lists_scheme_keys():
    for scheme, marker in (
        ("vim", ":w"),
        ("emacs", "Ctrl+X Ctrl+S"),
        ("default", "Ctrl+S"),
    ):
        app = _App(scheme)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = app.screen._render_bindings()
            assert "Edit Mode" in text
            assert marker in text


def test_cli_help_mentions_notepad():
    from tdb.cli import build_parser

    help_text = build_parser().format_help()
    assert "Notepad-style" in help_text
