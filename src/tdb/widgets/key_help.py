"""`K`: every key the focused view accepts, in a modal.

The footer has room for only a few keys; some views (the Code View in
Debug mode above all) accept far more. The list is read live, so it stays
right as bindings change: a view whose keys are not Textual bindings
supplies them through a `key_help_entries()` method (the Code View's
mode tables), every other view contributes the bindings Textual has in
effect for the focused widget and its ancestors.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Static

from tdb.widgets.modals import format_key_line


@dataclass(frozen=True)
class KeyHelp:
    """What the modal shows: (key display, description) pairs."""

    title: str
    view: list[tuple[str, str]]
    everywhere: list[tuple[str, str]]


def _plain(title: object) -> str:
    plain = getattr(title, "plain", None)
    if isinstance(plain, str):
        return plain
    return Text.from_markup(str(title)).plain


def collect_key_help(screen: Screen) -> KeyHelp:
    """The keys in effect on `screen` for whatever has focus."""
    chain = []
    node = screen.focused
    while node is not None and node is not screen:
        chain.append(node)
        node = node.parent

    provider = next((n for n in chain if hasattr(n, "key_help_entries")), None)
    view: list[tuple[str, str]] = (
        list(provider.key_help_entries()) if provider is not None else []
    )
    everywhere: list[tuple[str, str]] = []
    app = screen.app
    for active in screen.active_bindings.values():
        binding = active.binding
        if not binding.description or active.node is provider:
            continue
        entry = (app.get_key_display(binding), binding.description)
        if active.node is screen or active.node is app:
            everywhere.append(entry)
        elif active.node in chain:
            view.append(entry)

    titled = next((n for n in chain if getattr(n, "border_title", None)), None)
    title = f"Keys: {_plain(titled.border_title)}" if titled is not None else "Keys"
    return KeyHelp(title=title, view=view, everywhere=everywhere)


class KeyHelpModal(ModalScreen[None]):
    """The key reference for one view. ESC, q or K closes it."""

    DEFAULT_CSS = """
    KeyHelpModal {
        align: center middle;
    }
    KeyHelpModal #dialog {
        width: 70;
        height: 80%;
        max-height: 44;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    KeyHelpModal #key-help-scroll {
        height: 1fr;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close", show=False),
        Binding("q", "dismiss_modal", "Close", show=False),
        Binding("K", "dismiss_modal", "Close", show=False),
    ]

    def __init__(self, help: KeyHelp) -> None:
        super().__init__()
        self.help = help

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(f"[bold]{_escape(self.help.title)}[/bold]", markup=True)
            with VerticalScroll(id="key-help-scroll"):
                yield Static(self.render_text(), id="key-help-display", markup=True)

    def render_text(self) -> str:
        lines = []
        for heading, entries in (
            ("This view", self.help.view),
            ("Everywhere", self.help.everywhere),
        ):
            if not entries:
                continue
            lines.append(f"[bold underline]{heading}[/bold underline]")
            lines.extend(format_key_line(key, desc) for key, desc in entries)
            lines.append("")
        lines.append("[dim]Press ESC, q or K to close[/dim]")
        return "\n".join(lines)

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


def _escape(text: str) -> str:
    return text.replace("[", r"\[")
