"""Emacs-style keybindings for code navigation."""

from textual.binding import Binding

CODE_VIEW_BINDINGS = [
    Binding("ctrl+n", "scroll_down", "Down", show=False),
    Binding("ctrl+p", "scroll_up", "Up", show=False),
    Binding("alt+shift+comma", "scroll_home", "Top", show=False),
    Binding("alt+shift+period", "scroll_end", "Bottom", show=False),
    Binding("ctrl+v", "page_down", "Page Down", show=False),
    Binding("alt+v", "page_up", "Page Up", show=False),
]
