"""Live-preview color-theme picker.

A narrow list docked on the right over an undimmed screen: highlighting
a theme applies it to the whole app at once, Enter keeps it, Esc (or q)
restores the theme that was active when the list opened. The App owns
persistence; this modal only sets ``app.theme``.
"""

from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class ThemePickerModal(ModalScreen[str | None]):
    """Dismisses with the kept theme name, or None after reverting."""

    DEFAULT_CSS = """
    ThemePickerModal {
        align: right top;
        background: transparent;
    }
    ThemePickerModal #theme-dialog {
        width: 28;
        height: 100%;
        border: solid $primary;
        background: $surface;
    }
    ThemePickerModal #theme-title {
        text-style: bold;
        padding: 0 1;
    }
    ThemePickerModal OptionList {
        height: 1fr;
        border: none;
    }
    ThemePickerModal #theme-hint {
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "revert", "Revert", show=False),
        Binding("q", "revert", "Revert", show=False),
    ]

    def __init__(self, original: str) -> None:
        super().__init__()
        self._original = original

    def compose(self):
        names = sorted(self.app.available_themes)
        with Vertical(id="theme-dialog"):
            yield Static("Color theme", id="theme-title")
            yield OptionList(*(Option(n, id=n) for n in names), id="theme-list")
            yield Static("↑↓ preview  Enter keep\nEsc revert", id="theme-hint")

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        names = sorted(self.app.available_themes)
        if self._original in names:
            ol.highlighted = names.index(self._original)
        ol.focus()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        name = event.option.id
        if name and name != self.app.theme:
            self.app.theme = name

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def action_revert(self) -> None:
        if self.app.theme != self._original:
            self.app.theme = self._original
        self.dismiss(None)
