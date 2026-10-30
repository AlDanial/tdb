"""Editor widget behind Code View Edit mode.

`CodeEditor` is Textual's TextArea plus:

- tdb's key routing. Textual checks BINDINGS only *after* every
  `_on_key` handler up the DOM has run and none stopped the event, so
  handling a key here and calling `event.stop()` pre-empts both
  TextArea's own bindings and the App's (ctrl+s = focus Stack, etc.).
- a per-scheme key layer (EmacsLayer / ViLayer). With scheme "default"
  there is no layer: TextArea's stock behavior *is* the Notepad-style
  editor. ViLayer hands every key to the headless vi engine in
  `tdb.vi` and mirrors the engine's text, cursor and selection back
  into the TextArea; TextArea's own edit keys are never used then.
- messages instead of reach-ups: the editor never touches CodeView or
  the App. It asks to leave, asks to save, reports sub-mode changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol

from textual import events
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.events import Key
from textual.keys import key_to_character
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea
from textual.widgets.text_area import Selection

from tdb.vi import (
    LeaveRequest,
    Notify,
    SaveRequest,
    ViEngine,
    ViKey,
    ViSession,
    diff_text,
)

if TYPE_CHECKING:
    from tdb.vi.marks import MarkTable

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

    def sync_layer_from_widget(self) -> None:
        """Tell the key layer the TextArea text/cursor changed behind its back."""
        resync = getattr(self._layer, "resync_from_widget", None)
        if resync is not None:
            resync()

    # ---- footer hint no-ops ----

    def action_footer_hint_leave(self) -> None: ...
    def action_footer_hint_save(self) -> None: ...

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "footer_hint_save":
            return self.scheme != "emacs"
        # With ViLayer the vi engine owns the buffer: TextArea's own
        # bindings (ctrl+e line end, ctrl+v paste, ...) would bypass it.
        # Declining them lets keys ViLayer did not take (pane keys) reach
        # the App.
        return not isinstance(self._layer, ViLayer) or action.startswith("footer_hint_")

    # ---- key routing ----

    def check_consume_key(self, key: str, character: str | None = None) -> bool:
        # Textual's App binds ctrl+p to the command palette as a
        # *priority* binding, which is resolved before any focused
        # widget's `_on_key` runs — our layer would never see it.
        # Claiming it here (Screen._binding_chain's "filter out keys
        # consumed by a focused widget" pass) strips that App-level
        # binding out of the chain, so ctrl+p (emacs cursor-up) reaches
        # `_on_key`. Only the emacs scheme binds ctrl+p to anything —
        # vim and Notepad users keep the command palette.
        if key == "ctrl+p" and self.scheme == "emacs":
            return True
        return super().check_consume_key(key, character)

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
        if isinstance(self._layer, ViLayer):
            # A key ViLayer declined belongs to tdb (pane keys, menus).
            # prevent_default skips TextArea._on_key (which would type a
            # terminal's alt+e, whose character is "e") but, unlike
            # stop(), still lets the key bubble to the App's bindings.
            event.prevent_default()
            return
        if key == "insert":
            # TextArea has no overwrite mode; swallow so the key does
            # nothing surprising (spec: documented no-op).
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    # ---- Viewport (what the vi engine needs for H/M/L, ^D/^F, zt ...) ----

    def visible_rows(self) -> tuple[int, int]:
        """First and last (inclusive) document rows currently on screen."""
        top = int(self.scroll_offset.y)
        height = max(self.scrollable_content_region.height, 1)
        return top, top + height - 1

    def scroll_to_top_row(self, row: int) -> None:
        self.scroll_to(y=row, animate=False)

    # ---- paste / mouse: the vi engine owns the buffer ----

    async def _on_paste(self, event: events.Paste) -> None:
        layer = self._layer
        if hasattr(layer, "handle_paste") and not self.read_only:
            if layer.handle_paste(event.text):
                event.stop()
                event.prevent_default()
                return
        await super()._on_paste(event)

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        selecting = self._selecting
        await super()._on_mouse_up(event)
        layer = self._layer
        if selecting and hasattr(layer, "on_mouse_selection"):
            start, end = self.selection
            layer.on_mouse_selection(start, end)

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


class EmacsLayer:
    """Emacs-flavored overrides on top of TextArea.

    Only two bits of state: a pending Ctrl+X chord and a one-slot kill
    buffer (Ctrl+K fills it, Ctrl+Y inserts it). Keys not listed here
    fall through to TextArea's defaults, which are already emacs-ish
    (Ctrl+A/E, Ctrl+W, Ctrl+U ...).
    """

    def __init__(self, editor: CodeEditor) -> None:
        self.ed = editor
        self.pending_ctrl_x = False
        self.kill_buffer = ""

    @property
    def submode(self) -> str | None:
        return None

    @property
    def command_text(self) -> str:
        return ""

    def handle_key(self, event: Key) -> bool:
        key = event.key
        ed = self.ed

        if self.pending_ctrl_x:
            self.pending_ctrl_x = False
            if key == "ctrl+s":
                ed.post_message(CodeEditor.SaveRequested())
            elif key == "ctrl+c":
                ed.post_message(CodeEditor.LeaveRequested())
            # ctrl+g and any unknown second key: chord cancelled, key eaten
            return True

        if key == "ctrl+x":
            self.pending_ctrl_x = True
            return True
        if key == "ctrl+g":
            return True
        if key == "ctrl+n":
            ed.action_cursor_down()
        elif key == "ctrl+p":
            ed.action_cursor_up()
        elif key == "ctrl+f":
            ed.action_cursor_right()
        elif key == "ctrl+b":
            ed.action_cursor_left()
        elif key in ("alt+f", "ctrl+right"):
            ed.action_cursor_word_right()
        elif key in ("alt+b", "ctrl+left"):
            ed.action_cursor_word_left()
        elif key == "ctrl+a":
            ed.action_cursor_line_start()
        elif key == "ctrl+e":
            ed.action_cursor_line_end()
        elif key == "alt+less_than_sign":
            ed.move_cursor((0, 0))
        elif key == "alt+greater_than_sign":
            ed.move_cursor(ed.document.end)
        elif key == "ctrl+d":
            ed.action_delete_right()
        elif key == "ctrl+k":
            self._kill_line()
        elif key == "ctrl+y":
            if self.kill_buffer:
                ed.insert(self.kill_buffer)
        elif key in ("ctrl+underscore", "ctrl+slash"):
            ed.undo()
        elif key == "ctrl+s":
            ed.post_message(CodeEditor.SearchRequested(backward=False))
        elif key == "ctrl+r":
            ed.post_message(CodeEditor.SearchRequested(backward=True))
        else:
            return False
        return True

    def _kill_line(self) -> None:
        ed = self.ed
        row, col = ed.cursor_location
        line = ed.document.get_line(row)
        if col < len(line):
            start, end = (row, col), (row, len(line))
        elif row + 1 < ed.document.line_count:
            start, end = (row, col), (row + 1, 0)
        else:
            return
        self.kill_buffer = ed.get_text_range(start, end)
        ed.delete(start, end)
        ed.move_cursor(start)


# Keys tdb owns in the vim scheme: never fed to the vi engine.
_VI_TDB_KEYS = frozenset(
    {"ctrl+c", "ctrl+o", "ctrl+e", "ctrl+v", "ctrl+s", "ctrl+b", "ctrl+q", "ctrl+p"}
)
# Textual key names that are ViKey names as they are.
_VI_NAMED_KEYS = frozenset(
    {
        "escape",
        "enter",
        "backspace",
        "tab",
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
    }
)
# Textual's rewrites of a fast Esc f / Esc b (readline word motion).
_VI_ESC_REWRITES = {"ctrl+right": "f", "ctrl+left": "b"}
_VI_ESC = ViKey("escape")


def _is_function_key(key: str) -> bool:
    return key[:1] == "f" and key[1:].isdigit()


def _vi_key(name: str, character: str | None) -> ViKey | None:
    """The ViKey for a Textual key name (without alt+), or None."""
    if name in _VI_NAMED_KEYS:
        return ViKey(name)
    if character is not None and len(character) == 1 and character.isprintable():
        return ViKey(character)
    if len(name) == 6 and name.startswith("ctrl+") and "a" <= name[5] <= "z":
        return ViKey(name)
    if len(name) == 7 and name.startswith("shift+") and name[6].isalpha():
        return ViKey(name[6].upper())  # Pilot's alt+shift+j has no character
    char = key_to_character(name)
    if char is not None and len(char) == 1 and char.isprintable():
        return ViKey(char)
    return None


def _widget_text(editor: TextArea) -> str:
    """The TextArea text with ``\n`` line ends (never raw ``\r\n``)."""
    return "\n".join(editor.document.lines)


def _location(text: str, offset: int) -> tuple[int, int]:
    """``(row, column)`` of character *offset* in *text*."""
    row = text.count("\n", 0, offset)
    return row, offset - (text.rfind("\n", 0, offset) + 1)


class ViLayer:
    """Vim scheme: every key goes to a headless prompt_toolkit vi engine.

    The engine (`tdb.vi.ViEngine`) is the source of truth. After each key
    its text change is applied to the TextArea as one `replace`, then the
    cursor and visual selection are copied over. Keys tdb owns (pane
    keys, Ctrl+S/Q/P, F-keys, and Alt+* outside insert/replace mode) are
    declined so they bubble to CodeEditor and the App.
    """

    def __init__(
        self,
        editor: CodeEditor,
        *,
        session: ViSession,
        marks: MarkTable | None = None,
    ) -> None:
        self.ed = editor
        text = _widget_text(editor)
        indent = "\t" if editor.indent_type == "tabs" else " " * editor.indent_width
        self.engine = ViEngine(
            text,
            cursor=editor.cursor_location,
            session=session,
            marks=marks,
            viewport=editor,
            indent_unit=indent,
        )
        self._text = text  # engine text as last mirrored into the TextArea
        self._label = (self.engine.mode, self.engine.status)

    # ---- state exposed to CodeEditor / CodeView ----

    @property
    def submode(self) -> str:
        return self.engine.mode

    @property
    def command_text(self) -> str:
        return self.engine.status

    # ---- input ----

    def handle_key(self, event: Key) -> bool:
        engine = self.engine
        if event.key == "escape" and not engine.has_pending:
            return False  # CodeEditor turns this into LeaveRequested
        keys = self._translate(event.key, event.character)
        if keys is None:
            return False
        for key in keys:
            engine.feed(key)
        self._mirror()
        return True  # unmapped keys are swallowed: a stray key never edits

    def _translate(self, key: str, character: str | None) -> list[ViKey] | None:
        """ViKeys for a Textual key; None when the key belongs to tdb."""
        if key in _VI_TDB_KEYS or _is_function_key(key):
            return None
        inserting = self.engine.mode in ("insert", "replace")
        if key in _VI_ESC_REWRITES:
            return [_VI_ESC, ViKey(_VI_ESC_REWRITES[key])] if inserting else None
        if key.startswith("alt+"):
            if not inserting:
                return None  # menu shortcuts
            # A fast Esc+key (ssh/tmux) arrives as alt+key: split it back.
            rest = _vi_key(key[4:], character)
            return [_VI_ESC] if rest is None else [_VI_ESC, rest]
        vi_key = _vi_key(key, character)
        return [] if vi_key is None else [vi_key]

    def handle_paste(self, text: str) -> bool:
        """Insert pasted text at the cursor as one undo step (any mode)."""
        self.engine.insert_text(text.replace("\r\n", "\n").replace("\r", "\n"))
        self._mirror()
        return True

    def on_mouse_selection(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        """A click (start == end) moves the cursor; a drag selects."""
        if start == end:
            self.engine.set_cursor(*start)
        else:
            self.engine.select(start, end)
        self._mirror()

    def resync_from_widget(self) -> None:
        """Adopt TextArea text/cursor changed outside the engine (undoable)."""
        text = _widget_text(self.ed)
        if text != self.engine.text:
            self.engine.reset_text(text)
        self.engine.set_cursor(*self.ed.cursor_location)
        self._text = text
        self._mirror()

    # ---- engine -> TextArea ----

    def _mirror(self) -> None:
        ed, engine = self.ed, self.engine
        new = engine.text
        edit = diff_text(self._text, new)
        if edit is not None:
            start = _location(self._text, edit.start)
            end = _location(self._text, edit.old_end)
            ed.replace(edit.new_text, start, end)
            self._text = new
        ed.selection = self._selection()
        for event in engine.drain_events():
            if isinstance(event, SaveRequest):
                ed.post_message(CodeEditor.SaveRequested())
            elif isinstance(event, LeaveRequest):
                ed.post_message(CodeEditor.LeaveRequested(discard=event.discard))
            elif isinstance(event, Notify):
                ed.app.notify(event.text, title="Edit", severity=event.severity)
        label = (engine.mode, engine.status)
        if label != self._label:
            self._label = label
            ed.post_message(CodeEditor.SubmodeChanged())

    def _selection(self) -> Selection:
        """TextArea selection for the engine's cursor / visual selection."""
        engine = self.engine
        cursor = engine.cursor
        sel = engine.selection
        if sel is None:
            return Selection.cursor(cursor)
        (r1, c1), (r2, c2) = sel
        if engine.mode == "visual-line":
            start, end = (r1, 0), (r2, len(self.ed.document.get_line(r2)))
            backward = cursor[0] == r1 and r1 != r2
        else:
            start = (r1, c1)
            end = (r2, min(c2 + 1, len(self.ed.document.get_line(r2))))
            backward = cursor == sel[0] and sel[0] != sel[1]
        return Selection(end, start) if backward else Selection(start, end)


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
