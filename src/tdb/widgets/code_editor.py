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


class VimLayer:
    """Vim-lite: a normal/insert/command state machine over TextArea.

    Insert mode passes every key through to TextArea (so typing is
    native). Normal and command mode swallow keys that would insert or
    edit the buffer (a stray letter never edits it); everything else
    (Ctrl+S, Ctrl+Q, Alt+*, the Ctrl+O/V/E pane-focus keys, ...) is
    left unhandled so it bubbles to CodeEditor and the App. Deliberately
    NOT vim: no visual mode, registers, text objects, `.` repeat, or
    macros.
    """

    # Keys TextArea itself binds to an edit; normal mode still needs to
    # swallow these even though they are not "would insert" characters.
    _NORMAL_EDIT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "ctrl+k",
            "ctrl+u",
            "ctrl+w",
            "ctrl+x",
            "ctrl+v",
            "backspace",
            "delete",
            "enter",
            "tab",
        }
    )

    # Key names Textual delivers for the punctuation we use.
    _KEYS: ClassVar[dict[str, str]] = {
        "dollar_sign": "$",
        "circumflex_accent": "^",
        "colon": ":",
        "slash": "/",
        "question_mark": "?",
        "exclamation_mark": "!",
    }

    def __init__(self, editor: CodeEditor) -> None:
        self.ed = editor
        self._submode = "normal"
        self._command = ""
        self.count = ""
        self.pending = ""  # "", "d", "y", "g"
        self.yank_buffer = ""
        self.yank_linewise = False

    # ---- state exposed to CodeEditor / CodeView ----

    @property
    def submode(self) -> str:
        return self._submode

    @property
    def command_text(self) -> str:
        return self._command

    def _set_submode(self, submode: str) -> None:
        self._submode = submode
        self.ed.post_message(CodeEditor.SubmodeChanged())

    def _take_count(self) -> tuple[int, bool]:
        had = bool(self.count)
        n = int(self.count) if had else 1
        self.count = ""
        return max(1, n), had

    # ---- dispatch ----

    def handle_key(self, event: Key) -> bool:
        key = self._KEYS.get(event.key, event.key)
        if self._submode == "insert":
            if key == "escape":
                self._set_submode("normal")
                return True
            return False  # native TextArea typing
        if self._submode == "command":
            return self._handle_command_key(key, event.character)
        return self._handle_normal_key(key, event.character)

    # ---- command line ----

    def _handle_command_key(self, key: str, char: str | None) -> bool:
        if key == "escape":
            self._command = ""
            self._set_submode("normal")
        elif key == "enter":
            cmd = self._command.strip()
            self._command = ""
            self._set_submode("normal")
            self._run_command(cmd)
        elif key == "backspace":
            self._command = self._command[:-1]
            self.ed.post_message(CodeEditor.SubmodeChanged())
        elif char is not None and char.isprintable():
            self._command += char
            self.ed.post_message(CodeEditor.SubmodeChanged())
        else:
            return False  # e.g. ctrl+q, alt+*: let it bubble
        return True

    def _run_command(self, cmd: str) -> None:
        ed = self.ed
        if cmd == "w":
            ed.post_message(CodeEditor.SaveRequested())
        elif cmd == "q":
            ed.post_message(CodeEditor.LeaveRequested())
        elif cmd == "q!":
            ed.post_message(CodeEditor.LeaveRequested(discard=True))
        elif cmd in ("wq", "x"):
            ed.post_message(CodeEditor.SaveRequested())
            ed.post_message(CodeEditor.LeaveRequested())
        elif cmd.isdigit():
            line = max(1, min(int(cmd), ed.document.line_count))
            ed.move_cursor((line - 1, 0))
        elif cmd:
            ed.app.notify(f"Unknown command: :{cmd}", title="Edit", severity="warning")

    # ---- normal mode ----

    def _handle_normal_key(self, key: str, char: str | None = None) -> bool:
        ed = self.ed

        # Count prefix ('0' alone is a motion)
        if key.isdigit() and (self.count or key != "0"):
            self.count += key
            return True

        if self.pending:
            op, self.pending = self.pending, ""
            return self._handle_operator(op, key)

        count, had_count = self._take_count()
        row, col = ed.cursor_location
        line = ed.document.get_line(row)

        if key == "escape":
            return False  # CodeEditor turns this into LeaveRequested
        if key in ("h", "left"):
            for _ in range(count):
                ed.action_cursor_left()
        elif key in ("l", "right"):
            for _ in range(count):
                ed.action_cursor_right()
        elif key in ("j", "down"):
            for _ in range(count):
                ed.action_cursor_down()
        elif key in ("k", "up"):
            for _ in range(count):
                ed.action_cursor_up()
        elif key == "w":
            for _ in range(count):
                self._word_start_next()
        elif key == "b":
            for _ in range(count):
                ed.action_cursor_word_left()
        elif key == "e":
            for _ in range(count):
                self._word_end()
        elif key == "0":
            ed.move_cursor((row, 0))
        elif key == "^":
            ed.move_cursor((row, len(line) - len(line.lstrip())))
        elif key == "$":
            ed.move_cursor((row, len(line)))
        elif key == "G":
            if had_count:
                ed.move_cursor((min(count, ed.document.line_count) - 1, 0))
            else:
                ed.move_cursor((ed.document.line_count - 1, 0))
        elif key == "g":
            self.pending = "g"
        elif key in ("ctrl+f", "pagedown"):
            ed.action_cursor_page_down()
        elif key in ("ctrl+b", "pageup"):
            ed.action_cursor_page_up()
        elif key == "i":
            self._set_submode("insert")
        elif key == "a":
            if col < len(line):
                ed.move_cursor((row, col + 1))
            self._set_submode("insert")
        elif key == "I":
            ed.move_cursor((row, len(line) - len(line.lstrip())))
            self._set_submode("insert")
        elif key == "A":
            ed.move_cursor((row, len(line)))
            self._set_submode("insert")
        elif key == "o":
            ed.insert("\n", (row, len(line)))
            self._set_submode("insert")
        elif key == "O":
            ed.insert("\n", (row, 0))
            ed.move_cursor((row, 0))
            self._set_submode("insert")
        elif key == ":":
            self._command = ""
            self._set_submode("command")
        elif key == "/":
            ed.post_message(CodeEditor.SearchRequested(backward=False))
        elif key == "?":
            ed.post_message(CodeEditor.SearchRequested(backward=True))
        elif key == "n":
            ed.post_message(CodeEditor.SearchStepRequested(forward=True))
        elif key == "N":
            ed.post_message(CodeEditor.SearchStepRequested(forward=False))
        elif key in ("d", "y"):
            self.pending = key
            self.count = str(count) if had_count else ""
        elif not self._edit_key(key, count):
            # Not a recognized vim-lite command. Swallow it only if it
            # would type (a printable char) or is one of TextArea's own
            # edit bindings; everything else (ctrl+s, ctrl+q, alt+*,
            # the pane-focus ctrl+o/v/e ...) bubbles up so CodeEditor and
            # the App still see it.
            typed = char is not None and char.isprintable()
            if not (typed or key in self._NORMAL_EDIT_KEYS):
                return False
        return True

    def _handle_operator(self, op: str, key: str) -> bool:
        """Second key of gg / dd / dw / yy. Task 7 fills in d/y."""
        if op == "g":
            if key == "g":
                self.ed.move_cursor((0, 0))
            self.count = ""
            return True
        count, _ = self._take_count()
        return self._operator(op, key, count)

    def _operator(self, op: str, key: str, count: int) -> bool:
        ed = self.ed
        row, _ = ed.cursor_location
        if key == op:  # dd / yy
            start, end, text = self._line_range(row, count)
            self.yank_buffer, self.yank_linewise = text, True
            if op == "d":
                ed.delete(start, end)
                ed.move_cursor((min(row, ed.document.line_count - 1), 0))
        elif op == "d" and key == "w":
            start = ed.cursor_location
            for _ in range(count):
                self._word_start_next()
            end = ed.cursor_location
            self.yank_buffer, self.yank_linewise = ed.get_text_range(start, end), False
            ed.delete(start, end)
            ed.move_cursor(start)
        # any other second key: operator cancelled
        return True

    def _line_range(
        self, row: int, count: int
    ) -> tuple[tuple[int, int], tuple[int, int], str]:
        """Location span covering `count` whole lines from `row`, and
        the text (always newline-terminated) they contain. Deleting the
        last line takes the preceding newline instead of a trailing one."""
        ed = self.ed
        last = ed.document.line_count - 1
        end_row = min(row + count, last + 1)
        if end_row <= last:
            start, end = (row, 0), (end_row, 0)
            text = ed.get_text_range(start, end)
        else:
            end = ed.document.end
            text = ed.get_text_range((row, 0), end)
            if not text.endswith("\n"):
                text += "\n"
            start = (row - 1, len(ed.document.get_line(row - 1))) if row > 0 else (0, 0)
        return start, end, text

    def _edit_key(self, key: str, count: int) -> bool:
        """Recognized vim-lite edit commands. Returns False (unhandled)
        for anything else, so the caller can decide whether to swallow
        or let it bubble."""
        ed = self.ed
        row, col = ed.cursor_location
        line = ed.document.get_line(row)
        if key == "x":
            end_col = min(len(line), col + count)
            if end_col > col:
                self.yank_buffer, self.yank_linewise = line[col:end_col], False
                ed.delete((row, col), (row, end_col))
                ed.move_cursor((row, col))
        elif key == "X":
            start_col = max(0, col - count)
            if start_col < col:
                self.yank_buffer, self.yank_linewise = line[start_col:col], False
                ed.delete((row, start_col), (row, col))
                ed.move_cursor((row, start_col))
        elif key == "D":
            self.yank_buffer, self.yank_linewise = line[col:], False
            ed.delete((row, col), (row, len(line)))
            ed.move_cursor((row, col))
        elif key == "J":
            for _ in range(count):
                self._join_line()
        elif key == "p":
            self._put(after=True)
        elif key == "P":
            self._put(after=False)
        elif key == "u":
            for _ in range(count):
                ed.undo()
        elif key == "ctrl+r":
            for _ in range(count):
                ed.redo()
        else:
            return False
        return True

    def _join_line(self) -> None:
        ed = self.ed
        row, _ = ed.cursor_location
        if row + 1 >= ed.document.line_count:
            return
        line = ed.document.get_line(row)
        nxt = ed.document.get_line(row + 1)
        trimmed_len = len(line.rstrip(" \t"))
        lead = len(nxt) - len(nxt.lstrip(" \t"))
        ed.replace(" " if nxt.strip() else "", (row, trimmed_len), (row + 1, lead))
        ed.move_cursor((row, trimmed_len))

    def _put(self, *, after: bool) -> None:
        ed = self.ed
        if not self.yank_buffer:
            return
        row, col = ed.cursor_location
        if self.yank_linewise:
            text = self.yank_buffer
            if after:
                if row + 1 < ed.document.line_count:
                    ed.insert(text, (row + 1, 0))
                    ed.move_cursor((row + 1, 0))
                elif ed.document.get_line(row) == "":
                    # Phantom empty last row of a newline-terminated file
                    # (or an empty document): the "line after the cursor"
                    # is this row itself.
                    ed.insert(text, (row, 0))
                    ed.move_cursor((row, 0))
                else:
                    # Real last line with no trailing newline.
                    line = ed.document.get_line(row)
                    ed.insert("\n" + text.rstrip("\n"), (row, len(line)))
                    ed.move_cursor((row + 1, 0))
            else:
                ed.insert(text, (row, 0))
                ed.move_cursor((row, 0))
        else:
            line = ed.document.get_line(row)
            at = (row, min(len(line), col + 1)) if after else (row, col)
            result = ed.insert(self.yank_buffer, at)
            end = result.end_location
            ed.move_cursor((end[0], max(0, end[1] - 1)))

    def _word_start_next(self) -> None:
        """`w`: move to the start of the next word.

        Textual's `action_cursor_word_right` (used for emacs `M-f`)
        lands at the END of the current/next word, not vim's
        start-of-next-word. Land there, then skip the whitespace run
        that follows so the cursor sits on the next word's first
        character. At/past end of line, continue onto the first
        non-blank of the next line; stays put at end of document.
        """
        ed = self.ed
        ed.action_cursor_word_right()
        row, col = ed.cursor_location
        line = ed.document.get_line(row)
        while col < len(line) and line[col] in (" ", "\t"):
            col += 1
            ed.move_cursor((row, col))
        if col >= len(line) and row + 1 < ed.document.line_count:
            next_row = row + 1
            next_line = ed.document.get_line(next_row)
            first_non_blank = len(next_line) - len(next_line.lstrip(" \t"))
            ed.move_cursor((next_row, first_non_blank))

    def _word_end(self) -> None:
        """Approximate `e`: jump to the last char of the next word."""
        ed = self.ed
        ed.action_cursor_word_right()
        row, col = ed.cursor_location
        line = ed.document.get_line(row)
        # word_right lands after the word (on the space); step back onto
        # its last character when we're not at a line boundary.
        if col > 0 and (col >= len(line) or line[col] == " "):
            ed.move_cursor((row, col - 1))


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
