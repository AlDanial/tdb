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


# Actions that raise SkipAction unless they have something to do, so the
# key goes on down the chain: Ctrl+C copies a selection, else reaches the
# app's "focus Code View". Both meanings are listed.
_FALLS_THROUGH = frozenset({"screen.copy_text", "copy_text"})


def _handlers(screen: Screen) -> list[tuple[object, str, Binding]]:
    """(node, key, binding) for every binding that can act on its key, in the
    order Textual tries them: priority bindings first (app end first), then
    the rest from the focused widget out. A binding only shadows the ones
    after it for the same key when it never falls through."""
    app = screen.app
    chain = screen._modal_binding_chain
    ordered = [
        (node, key, binding)
        for node, bindings in reversed(chain)
        for key, binding in bindings
        if binding.priority
    ] + [
        (node, key, binding)
        for node, bindings in chain
        for key, binding in bindings
        if not binding.priority
    ]
    taken: set[str] = set()
    out = []
    for node, key, binding in ordered:
        if key in taken:
            continue
        if app._check_action_state(binding.action, node) is False:
            continue  # disabled: Textual skips it too
        if binding.action not in _FALLS_THROUGH:
            taken.add(key)
        out.append((node, key, binding))
    return out


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
    # A view that reads keys itself (the Code View's _on_key) takes them
    # before any binding further out sees them.
    consumes = getattr(provider, "consumes_key", None)
    everywhere: list[tuple[str, str]] = []
    seen = set(view)
    app = screen.app
    for node, key, binding in _handlers(screen):
        if not binding.description or node is provider:
            continue
        if consumes is not None and consumes(key):
            continue
        entry = (app.get_key_display(binding), binding.description)
        if entry in seen:
            continue
        seen.add(entry)
        if node is screen or node is app:
            everywhere.append(entry)
        elif node in chain:
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
