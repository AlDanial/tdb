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
    """A horizontal menu bar with dropdown menus and direct-action labels."""

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

    MenuBar .action-label {
        width: auto;
        padding: 0 2;
        height: 1;
        background: $primary-background;
        color: $text;
    }

    MenuBar .action-label:hover {
        background: $accent;
        color: $text;
    }

    MenuBar .menu-spacer {
        width: 1fr;
        height: 1;
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

    class ActionLabelClicked(Message):
        """Posted when a direct-action label is clicked."""
        def __init__(self, label_id: str) -> None:
            self.label_id = label_id
            super().__init__()

    def __init__(
        self,
        menus: dict[str, list[str]],
        *,
        action_labels: dict[str, str] | None = None,
        leading_action_labels: dict[str, str] | None = None,
        right_menus: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._right_menu_names = set(right_menus or [])
        self._menus = menus
        self._leading_action_labels = leading_action_labels or {}
        self._action_labels = action_labels or {}  # id -> display text
        self._open_menu: str | None = None
        self._dropdowns_mounted = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            for label_id, text in self._leading_action_labels.items():
                yield Label(
                    f" {text} ",
                    classes="action-label",
                    id=label_id,
                )
            for menu_name in self._menus:
                if menu_name in self._right_menu_names:
                    continue
                yield Label(
                    f" {menu_name} ",
                    classes="menu-label",
                    id=f"menu-{menu_name.lower().replace(' ', '-')}",
                )
            for label_id, text in self._action_labels.items():
                yield Label(
                    f" {text} ",
                    classes="action-label",
                    id=label_id,
                )
            # Spacer pushes right-aligned menus to the end
            if self._right_menu_names:
                yield Label("", classes="menu-spacer")
                for menu_name in self._menus:
                    if menu_name not in self._right_menu_names:
                        continue
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
        if isinstance(target, Label) and "action-label" in target.classes:
            self._close_all()
            self.post_message(self.ActionLabelClicked(target.id or ""))
            event.stop()
        elif isinstance(target, Label) and "menu-label" in target.classes:
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
            y = self.region.y + self.region.height
            if menu_name in self._right_menu_names:
                # Right-align the dropdown's right edge to the label's right
                # edge so longer entries (e.g. "Documentation" in Help) stay
                # on-screen instead of spilling past the terminal edge.
                dropdown_width = max(
                    (len(opt.prompt.plain) if hasattr(opt.prompt, "plain") else len(str(opt.prompt)))
                    for opt in dropdown._options
                ) + 2  # +2 for the border
                screen_width = self.screen.region.width
                x = min(
                    label.region.right - dropdown_width,
                    screen_width - dropdown_width,
                )
                x = max(0, x)
            else:
                x = label.region.x
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

    def close_menus(self) -> None:
        """Close any open dropdown. Safe to call when none is open."""
        self._close_all()

    def open_menu(self, menu_name: str) -> bool:
        """Programmatically open (or toggle closed) a dropdown by its label.

        Returns True if the menu was opened, False if the name is unknown
        or it was toggled closed. Used by keyboard shortcuts.
        """
        if menu_name not in self._menus:
            return False
        if self._open_menu == menu_name:
            self._close_all()
            return False
        label_id = f"menu-{menu_name.lower().replace(' ', '-')}"
        try:
            label = self.query_one(f"#{label_id}", Label)
        except Exception:
            return False
        self._open_dropdown(menu_name, label)
        return True

    def update_action_label(self, label_id: str, text: str) -> None:
        """Update the display text of a direct-action label."""
        try:
            label = self.query_one(f"#{label_id}", Label)
            label.update(f" {text} ")
        except Exception:
            pass

    def action_close_menu(self) -> None:
        self._close_all()
