"""Menu bar widget with dropdown menus.

Dropdowns are mounted on the Screen (not as children of the 1-row MenuBar)
so they aren't clipped by overflow.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.events import Click
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


class _MenuDropdown(OptionList):
    """An OptionList used as a menu dropdown, mounted on the Screen layer."""

    DEFAULT_CSS = """
    _MenuDropdown {
        display: none;
        layer: above;
        width: auto;
        min-width: 20;
        max-height: 12;
        border: solid $primary;
        background: $surface;
        padding: 0;
        margin: 0;
    }

    _MenuDropdown.--visible {
        display: block;
    }
    """


class MenuBar(Widget):
    """A horizontal menu bar with dropdown menus."""

    DEFAULT_CSS = """
    MenuBar {
        dock: top;
        height: 1;
        background: $primary-background;
        layout: horizontal;
    }

    MenuBar .menu-label {
        width: auto;
        padding: 0 2;
        height: 1;
        background: $primary-background;
        color: $text;
    }

    MenuBar .menu-label:hover {
        background: $accent;
        color: $text;
    }

    MenuBar .menu-label.--active {
        background: $accent;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("escape", "close_menu", "Close", show=False),
    ]

    class MenuItemSelected(Message):
        """Posted when a menu item is selected."""
        def __init__(self, menu: str, item: str) -> None:
            self.menu = menu
            self.item = item
            super().__init__()

    def __init__(self, menus: dict[str, list[str]], **kwargs) -> None:
        super().__init__(**kwargs)
        self._menus = menus
        self._open_menu: str | None = None
        self._dropdowns_mounted = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            for menu_name in self._menus:
                yield Label(
                    f" {menu_name} ",
                    classes="menu-label",
                    id=f"menu-{menu_name.lower().replace(' ', '-')}",
                )

    def on_mount(self) -> None:
        """Mount dropdown widgets on the Screen so they render above everything."""
        if self._dropdowns_mounted:
            return
        for menu_name, items in self._menus.items():
            options = [Option(item, id=f"{menu_name}:{item}") for item in items]
            dropdown = _MenuDropdown(
                *options,
                id=f"dropdown-{menu_name.lower().replace(' ', '-')}",
            )
            self.screen.mount(dropdown)
        self._dropdowns_mounted = True

    def on_click(self, event: Click) -> None:
        target = event.widget
        if isinstance(target, Label) and "menu-label" in target.classes:
            menu_name = target.render().plain.strip()
            if self._open_menu == menu_name:
                self._close_all()
            else:
                self._open_dropdown(menu_name, target)
            event.stop()

    def _open_dropdown(self, menu_name: str, label: Label) -> None:
        self._close_all()
        self._open_menu = menu_name
        label.add_class("--active")
        dropdown_id = f"dropdown-{menu_name.lower().replace(' ', '-')}"
        try:
            dropdown = self.screen.query_one(f"#{dropdown_id}", _MenuDropdown)
            # Position: below the menu bar, aligned to the label's x
            x = label.region.x
            y = self.region.y + self.region.height
            dropdown.styles.offset = (x, y)
            dropdown.add_class("--visible")
            dropdown.focus()
        except Exception:
            pass

    def _close_all(self) -> None:
        self._open_menu = None
        for label in self.query(".menu-label"):
            label.remove_class("--active")
        if self._dropdowns_mounted:
            for dropdown in self.screen.query(_MenuDropdown):
                dropdown.remove_class("--visible")

    def action_close_menu(self) -> None:
        self._close_all()
