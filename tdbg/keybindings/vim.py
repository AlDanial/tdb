"""Vim-style keybindings for code navigation."""

from textual.binding import Binding

CODE_VIEW_BINDINGS = [
    Binding("j", "scroll_down", "Down", show=False),
    Binding("k", "scroll_up", "Up", show=False),
    Binding("g", "scroll_home", "Top", show=False),
    Binding("shift+g", "scroll_end", "Bottom", show=False),
    Binding("ctrl+d", "page_down", "Page Down", show=False),
    Binding("ctrl+u", "page_up", "Page Up", show=False),
]
