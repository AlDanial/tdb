"""Editor widget behind Code View Edit mode.

`CodeEditor` is Textual's TextArea plus:

- tdb's key routing. Textual checks BINDINGS only *after* every
  `_on_key` handler up the DOM has run and none stopped the event, so
  handling a key here and calling `event.stop()` pre-empts both
  TextArea's own bindings and the App's (ctrl+s = focus Stack, etc.).
- a per-scheme key layer (EmacsLayer / VimLayer, Tasks 5-6). With
  scheme "default" there is no layer: TextArea's stock behavior *is*
  the Notepad-style editor.
- messages instead of reach-ups: the editor never touches CodeView or
  the App. It asks to leave, asks to save, reports sub-mode changes.
"""

from __future__ import annotations

from typing import ClassVar, Protocol

from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

# Lexer names (LanguageProfile.presentation.lexer) that Textual's
# bundled tree-sitter grammars can highlight.
_TEXTUAL_LANGUAGES = {"python": "python", "bash": "bash", "go": "go", "rust": "rust"}


def editor_language_for(lexer: str | None) -> str | None:
    """Tree-sitter language for TextArea, or None for plain text.

    None when the optional `textual[syntax]` extra (tdb's `[edit]`
    extra) is not installed, or when the lexer has no bundled grammar.
    """
    import textual._tree_sitter as ts

    if not ts.TREE_SITTER or lexer is None:
        return None
    return _TEXTUAL_LANGUAGES.get(lexer)


class KeyLayer(Protocol):
    """A scheme-specific key interpreter installed on a CodeEditor."""

    def handle_key(self, event: Key) -> bool:
        """Return True when the key was consumed."""
        ...

    @property
    def submode(self) -> str | None: ...

    @property
    def command_text(self) -> str: ...


class CodeEditor(TextArea):
    DEFAULT_CSS = """
    CodeEditor {
        width: 100%;
        height: 100%;
        border: none;
        padding: 0;
    }
    """

    # Footer hints only. `_on_key` stops these keys before the binding
    # system sees them, so the no-op action methods never run (same
    # trick as CodeView's footer_hint_* bindings).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "footer_hint_leave", "leave edit"),
        Binding("ctrl+s", "footer_hint_save", "save"),
    ]

    class LeaveRequested(Message):
        def __init__(self, discard: bool = False) -> None:
            self.discard = discard
            super().__init__()

    class SaveRequested(Message):
        pass

    class SubmodeChanged(Message):
        pass

    class SearchRequested(Message):
        def __init__(self, backward: bool) -> None:
            self.backward = backward
            super().__init__()

    class SearchStepRequested(Message):
        def __init__(self, forward: bool) -> None:
            self.forward = forward
            super().__init__()

    def __init__(
        self,
        text: str,
        *,
        scheme: str,
        lexer: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            text,
            language=editor_language_for(lexer),
            soft_wrap=False,
            tab_behavior="indent",
            show_line_numbers=True,
            **kwargs,
        )
        self.scheme = scheme
        self._clean_text = text
        self._layer: KeyLayer | None = None

    # ---- state ----

    @property
    def is_dirty(self) -> bool:
        return self.text != self._clean_text

    def mark_clean(self) -> None:
        self._clean_text = self.text

    @property
    def submode(self) -> str | None:
        return self._layer.submode if self._layer is not None else None

    @property
    def command_text(self) -> str:
        return self._layer.command_text if self._layer is not None else ""

    def _set_layer(self, layer: KeyLayer | None) -> None:
        self._layer = layer

    # ---- footer hint no-ops ----

    def action_footer_hint_leave(self) -> None: ...
    def action_footer_hint_save(self) -> None: ...

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "footer_hint_save":
            return self.scheme != "emacs"
        return True

    # ---- key routing ----

    async def _on_key(self, event: Key) -> None:
        if self._layer is not None and self._layer.handle_key(event):
            event.stop()
            event.prevent_default()
            return
        key = event.key
        if key == "escape":
            # Must run before TextArea._on_key: with tab_behavior="indent"
            # it turns Esc into screen.focus_next().
            event.stop()
            event.prevent_default()
            self.post_message(self.LeaveRequested())
            return
        if key == "ctrl+s" and self.scheme != "emacs":
            event.stop()
            event.prevent_default()
            self.post_message(self.SaveRequested())
            return
        if key == "insert":
            # TextArea has no overwrite mode; swallow so the key does
            # nothing surprising (spec: documented no-op).
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    # ---- search (used by all schemes via CodeView's search modal) ----

    def find(self, term: str, backward: bool, *, from_cursor: bool = True) -> bool:
        """Move the caret to the next/previous line containing `term`
        (case-insensitive, wrapping). Returns False when not found."""
        if not term:
            return False
        needle = term.lower()
        total = self.document.line_count
        if total == 0:
            return False
        row = (
            self.cursor_location[0]
            if from_cursor
            else (total - 1 if not backward else 0)
        )
        step = -1 if backward else 1
        for i in range(1, total + 1):
            idx = (row + step * i) % total
            if needle in self.document.get_line(idx).lower():
                self.move_cursor((idx, 0))
                return True
        return False


class _UnsavedChangesModal(ModalScreen[str]):
    """Save / Discard / Cancel prompt used by every path that would
    abandon unsaved edits (leave Edit mode, quit, restart, open)."""

    DEFAULT_CSS = """
    _UnsavedChangesModal {
        align: center middle;
    }
    _UnsavedChangesModal #dialog {
        width: 52;
        height: auto;
        border: solid $warning;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("s", "save", "Save", show=False),
        Binding("d", "discard", "Discard", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, filename: str) -> None:
        super().__init__()
        self._filename = filename

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(
                f"[bold]{self._filename} has unsaved changes[/bold]", markup=True
            )
            yield Static("[bold]s[/bold]: save and continue", markup=True)
            yield Static("[bold]d[/bold]: discard changes and continue", markup=True)
            yield Static("[dim]ESC: cancel, keep editing[/dim]", markup=True)

    def action_save(self) -> None:
        self.dismiss("save")

    def action_discard(self) -> None:
        self.dismiss("discard")

    def action_cancel(self) -> None:
        self.dismiss("cancel")
