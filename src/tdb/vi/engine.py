"""Headless prompt_toolkit vi engine.

A prompt_toolkit ``Application`` is built but never run: keys are fed
straight into its key processor and the resulting buffer text, cursor
and mode are read back.  The application always uses ``DummyInput`` /
``DummyOutput`` so no real terminal (or Windows console) is touched.

The engine stores the text *without* its final newline (prompt_toolkit
would otherwise show a phantom empty last row that vim does not have)
and remembers whether there was one in the buffer's ``eol`` flag, which
is part of the undo history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import InMemoryClipboard
from prompt_toolkit.document import Document
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.input import DummyInput
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.key_binding.vi_state import InputMode, ViState
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.selection import SelectionType

from tdb.vi.bindings import load_tdb_vi_bindings

if TYPE_CHECKING:
    from tdb.vi.marks import MarkTable
    from tdb.vi.substitute import SubstituteSpec

log = logging.getLogger(__name__)

# ViKey name -> (prompt_toolkit key, data) for the non-printable keys.
_SPECIAL_KEYS: dict[str, tuple[Keys, str]] = {
    "escape": (Keys.Escape, "\x1b"),
    "enter": (Keys.ControlM, "\r"),
    "backspace": (Keys.ControlH, "\x7f"),
    "tab": (Keys.ControlI, "\t"),
    "delete": (Keys.Delete, "\x1b[3~"),
    "up": (Keys.Up, "\x1b[A"),
    "down": (Keys.Down, "\x1b[B"),
    "left": (Keys.Left, "\x1b[D"),
    "right": (Keys.Right, "\x1b[C"),
    "home": (Keys.Home, "\x1b[H"),
    "end": (Keys.End, "\x1b[F"),
    "pageup": (Keys.PageUp, "\x1b[5~"),
    "pagedown": (Keys.PageDown, "\x1b[6~"),
}

# Vim notation (lower-cased, without <>) -> ViKey name.
_NOTATION: dict[str, str] = {
    "esc": "escape",
    "cr": "enter",
    "enter": "enter",
    "return": "enter",
    "bs": "backspace",
    "tab": "tab",
    "del": "delete",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "home": "home",
    "end": "end",
    "pageup": "pageup",
    "pagedown": "pagedown",
    "lt": "<",
    "space": " ",
}

# ViKey name -> vim notation, for showing pending keys in the status.
_DISPLAY: dict[str, str] = {
    "escape": "<Esc>",
    "enter": "<CR>",
    "backspace": "<BS>",
    "tab": "<Tab>",
    "delete": "<Del>",
    "up": "<Up>",
    "down": "<Down>",
    "left": "<Left>",
    "right": "<Right>",
    "home": "<Home>",
    "end": "<End>",
    "pageup": "<PageUp>",
    "pagedown": "<PageDown>",
}


@dataclass(frozen=True)
class ViKey:
    """One key for the engine.

    ``name`` is a single printable character, one of ``escape enter
    backspace tab delete up down left right home end pageup pagedown``,
    or ``ctrl+<letter>``.
    """

    name: str

    def __post_init__(self) -> None:
        n = self.name
        ok = (
            (len(n) == 1 and n.isprintable())
            or n in _SPECIAL_KEYS
            or (len(n) == 6 and n.startswith("ctrl+") and "a" <= n[5] <= "z")
        )
        if not ok:
            raise ValueError(f"invalid ViKey name: {n!r}")

    @classmethod
    def parse(cls, notation: str) -> list[ViKey]:
        """Parse vim key notation such as ``"ciw<Esc>"`` or ``"<C-r>"``.

        Recognised ``<...>`` names are case-insensitive; an unrecognised
        ``<`` is taken literally.
        """
        keys: list[ViKey] = []
        i = 0
        while i < len(notation):
            ch = notation[i]
            if ch == "<":
                close = notation.find(">", i + 1)
                if close != -1:
                    name = _parse_bracketed(notation[i + 1 : close])
                    if name is not None:
                        keys.append(cls(name))
                        i = close + 1
                        continue
            keys.append(cls(ch))
            i += 1
        return keys

    @property
    def notation(self) -> str:
        """Vim notation for display (``"d"``, ``"<Esc>"``, ``"<C-r>"``)."""
        if self.name.startswith("ctrl+"):
            return f"<C-{self.name[5:]}>"
        return _DISPLAY.get(self.name, self.name)

    def to_key_press(self) -> KeyPress:
        """The prompt_toolkit ``KeyPress`` for this key."""
        n = self.name
        if n in _SPECIAL_KEYS:
            key, data = _SPECIAL_KEYS[n]
            return KeyPress(key, data)
        if n.startswith("ctrl+"):
            letter = n[5:]
            return KeyPress(Keys(f"c-{letter}"), chr(ord(letter) - ord("a") + 1))
        return KeyPress(n, n)


def _parse_bracketed(inner: str) -> str | None:
    """ViKey name for the text between ``<`` and ``>``, or None."""
    low = inner.lower()
    if low in _NOTATION:
        return _NOTATION[low]
    if len(low) == 3 and low.startswith("c-") and "a" <= low[2] <= "z":
        return f"ctrl+{low[2]}"
    return None


@dataclass
class Notify:
    """Engine event: show a notification."""

    text: str
    severity: Literal["information", "warning", "error"] = "information"


@dataclass
class SaveRequest:
    """Engine event: the user asked to save (``:w``)."""


@dataclass
class LeaveRequest:
    """Engine event: the user asked to leave Edit mode (``:q``)."""

    discard: bool = False


EngineEvent = Notify | SaveRequest | LeaveRequest


class Viewport(Protocol):
    """What the engine needs to know about the on-screen view."""

    def visible_rows(self) -> tuple[int, int]:
        """First and last (inclusive) rows currently visible."""
        ...

    def scroll_to_top_row(self, row: int) -> None:
        """Scroll so that *row* is the first visible row."""
        ...


@dataclass
class ViSession:
    """Process-wide vi state shared by every engine (registers, history...)."""

    vi_state: object = field(default_factory=ViState)
    clipboard: object = field(default_factory=InMemoryClipboard)
    last_search: tuple[str, bool] | None = None
    last_substitute: SubstituteSpec | None = None
    ex_history: list[str] = field(default_factory=list)
    search_history: list[str] = field(default_factory=list)
    last_change: list[ViKey] | None = None


class _ViBuffer(Buffer):
    """A Buffer whose undo/redo history also covers the final-newline flag.

    Same algorithm as prompt_toolkit's ``Buffer`` undo, with stack entries
    of ``(text, cursor_position, eol)`` instead of ``(text, cursor_position)``.
    """

    eol: bool = False

    def _state(self) -> tuple[str, int, bool]:
        return self.text, self.cursor_position, self.eol

    def save_to_undo_stack(self, clear_redo_stack: bool = True) -> None:
        undo: list = self._undo_stack
        if undo and undo[-1][0] == self.text and undo[-1][2] == self.eol:
            undo[-1] = self._state()
        else:
            undo.append(self._state())
        if clear_redo_stack:
            self._redo_stack = []

    def undo(self) -> None:
        undo: list = self._undo_stack
        redo: list = self._redo_stack
        while undo:
            text, pos, eol = undo.pop()
            if text != self.text or eol != self.eol:
                redo.append(self._state())
                self._restore(text, pos, eol)
                break

    def redo(self) -> None:
        redo: list = self._redo_stack
        if redo:
            self.save_to_undo_stack(clear_redo_stack=False)
            self._restore(*redo.pop())

    def _restore(self, text: str, pos: int, eol: bool) -> None:
        self.eol = eol
        self.document = Document(text, cursor_position=pos)


class ViEngine:
    """A headless vi editor over one text buffer."""

    def __init__(
        self,
        text: str,
        *,
        cursor: tuple[int, int] = (0, 0),
        session: ViSession | None = None,
        marks: MarkTable | None = None,
        viewport: Viewport | None = None,
        indent_unit: str = "    ",
        _extra_bindings: KeyBindings | None = None,
    ) -> None:
        self.session = session if session is not None else ViSession()
        self.marks = marks
        self.viewport = viewport
        self.indent_unit = indent_unit
        self._events: list[EngineEvent] = []
        self._pending_keys: list[ViKey] = []
        # Text of the ':' line while in the stub command mode, else None.
        self._cmdline: str | None = None

        body, eol = _split_eol(text)
        self.buffer = _ViBuffer(document=Document(body, 0), multiline=True)
        self.buffer.eol = eol
        kb = load_tdb_vi_bindings(self)
        if _extra_bindings is not None:
            kb = merge_key_bindings([kb, _extra_bindings])
        self.app: Application[None] = Application(
            layout=Layout(Window(BufferControl(self.buffer))),
            key_bindings=kb,
            editing_mode=EditingMode.VI,
            input=DummyInput(),
            output=DummyOutput(),
            full_screen=True,
        )
        self.app.timeoutlen = None
        self.app.clipboard = self.session.clipboard  # type: ignore[assignment]
        self.app.vi_state = self.session.vi_state  # type: ignore[assignment]
        vs = self.app.vi_state
        vs.input_mode = InputMode.NAVIGATION  # also clears operator state
        vs.waiting_for_digraph = False
        vs.temporary_navigation_mode = False
        vs.recording_register = None
        vs.current_recording = ""
        self._move_to(*cursor)

    # ------------------------------------------------------------------
    # Feeding keys

    def feed(self, key: ViKey) -> None:
        """Process one key; on an exception restore the pre-key state."""
        if self._cmdline is not None:
            self._feed_cmdline(key)
            return
        if key.name == ":" and self.mode == "normal" and not self._key_pending():
            self._cmdline = ""
            return
        buff = self.buffer
        before = (
            buff.document,
            buff.eol,
            list(buff._undo_stack),
            list(buff._redo_stack),
        )
        self._pending_keys.append(key)
        try:
            with set_app(self.app):
                self.app.key_processor.feed(key.to_key_press())
                self.app.key_processor.process_keys()
        except Exception as exc:  # a stray key must never crash tdb
            log.exception("vi engine error on key %r", key.name)
            self._recover(*before)
            self._events.append(Notify(f"vi: {key.notation} failed: {exc}", "warning"))
            return
        if not self._key_pending():
            self._pending_keys.clear()

    def feed_keys(self, notation: str) -> None:
        """Parse vim *notation* and feed each key."""
        for key in ViKey.parse(notation):
            self.feed(key)

    def _recover(self, document: Document, eol: bool, undo: list, redo: list) -> None:
        """Put back the pre-key buffer, undo/redo history and normal mode."""
        self.app.key_processor.reset()
        vs = self.app.vi_state
        vs.input_mode = InputMode.NAVIGATION
        vs.waiting_for_digraph = False
        vs.temporary_navigation_mode = False
        buff = self.buffer
        buff.selection_state = None
        buff.set_document(document, bypass_readonly=True)
        buff.eol = eol
        buff._undo_stack = undo
        buff._redo_stack = redo
        self._pending_keys.clear()

    # ------------------------------------------------------------------
    # ':' command line (minimal stub; Task 10 replaces it with real ex)

    def _feed_cmdline(self, key: ViKey) -> None:
        cmd = self._cmdline or ""
        if key.name == "escape" or (key.name == "backspace" and not cmd):
            self._cmdline = None
        elif key.name == "backspace":
            self._cmdline = cmd[:-1]
        elif key.name == "enter":
            self._cmdline = None
            self._run_cmdline(cmd.strip())
        elif len(key.name) == 1:
            self._cmdline = cmd + key.name

    def _run_cmdline(self, cmd: str) -> None:
        if cmd == "w":
            self._events.append(SaveRequest())
        elif cmd in ("q", "q!"):
            self._events.append(LeaveRequest(discard=cmd == "q!"))
        elif cmd in ("wq", "x"):
            self._events += [SaveRequest(), LeaveRequest()]
        elif cmd.isdigit():
            row = max(1, min(int(cmd), self.buffer.document.line_count)) - 1
            self._move_to(row, self.cursor[1])  # nvim nostartofline
        elif cmd:
            self._events.append(Notify(f"Unknown command: :{cmd}", "warning"))

    def drain_events(self) -> list[EngineEvent]:
        """Return and clear the events produced since the last drain."""
        events, self._events = self._events, []
        return events

    # ------------------------------------------------------------------
    # State

    @property
    def _eol(self) -> bool:
        """Whether the text ends with a final newline."""
        return self.buffer.eol

    @property
    def text(self) -> str:
        """Full text, with the original final-newline state."""
        return self.buffer.text + ("\n" if self._eol else "")

    @property
    def cursor(self) -> tuple[int, int]:
        """``(row, column)`` of the cursor, 0-based codepoint column."""
        doc = self.buffer.document
        return doc.cursor_position_row, doc.cursor_position_col

    @property
    def mode(self) -> str:
        """``normal insert replace visual visual-line command`` (more later)."""
        if self._cmdline is not None:
            return "command"
        input_mode = self.app.vi_state.input_mode
        if input_mode in (InputMode.INSERT, InputMode.INSERT_MULTIPLE):
            return "insert"
        if input_mode in (InputMode.REPLACE, InputMode.REPLACE_SINGLE):
            return "replace"
        sel = self.buffer.selection_state
        if sel is not None:
            return "visual-line" if sel.type == SelectionType.LINES else "visual"
        return "normal"

    @property
    def selection(self) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Ordered inclusive ``(start, end)`` of the visual selection, or None."""
        if self.buffer.selection_state is None:
            return None
        doc = self.buffer.document
        start, end = doc.selection_range()
        return doc.translate_index_to_position(start), doc.translate_index_to_position(
            end
        )

    @property
    def has_pending(self) -> bool:
        """True while a command is incomplete or the mode is not normal."""
        return self._key_pending() or self.mode != "normal"

    @property
    def status(self) -> str:
        """``:cmd`` line, pending keys (``"a2d``), ``recording @a``, or ``""``."""
        if self._cmdline is not None:
            return ":" + self._cmdline
        if self._pending_keys and self._key_pending():
            return "".join(k.notation for k in self._pending_keys)
        reg = self.app.vi_state.recording_register
        if reg:
            return f"recording @{reg}"
        return ""

    def _key_pending(self) -> bool:
        """Unprocessed keys, a register prefix, a count or an operator."""
        kp = self.app.key_processor
        return bool(
            kp.key_buffer
            or kp.input_queue
            or kp.arg is not None
            or self.app.vi_state.operator_func is not None
        )

    # ------------------------------------------------------------------
    # Programmatic changes

    def set_cursor(self, row: int, col: int) -> None:
        """Move the cursor (clamped), leaving visual mode."""
        self.buffer.exit_selection()
        self._move_to(row, col)

    def select(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        """Select characterwise from *start* to *end* (inclusive); enters visual."""
        self.buffer.exit_selection()
        self.app.vi_state.input_mode = InputMode.NAVIGATION
        self._move_to(*start)
        self.buffer.start_selection(SelectionType.CHARACTERS)
        self._move_to(*end)

    def insert_text(self, text: str) -> None:
        """Insert *text* at the cursor as one undo step; mode is unchanged."""
        self.buffer.save_to_undo_stack()
        self.buffer.insert_text(text)
        if self.mode == "normal":
            self._move_to(*self.cursor)  # keep the cursor on a character

    def reset_text(self, text: str) -> None:
        """Replace the whole buffer (undoable), keeping the cursor row/col."""
        row, col = self.cursor
        self.buffer.exit_selection()
        self.buffer.save_to_undo_stack()
        body, self.buffer.eol = _split_eol(text)
        self.buffer.set_document(Document(body, 0), bypass_readonly=True)
        self._move_to(row, col)

    def _move_to(self, row: int, col: int) -> None:
        """Set the cursor, clamped to the text (and to a character in normal mode)."""
        doc = self.buffer.document
        row = max(0, min(row, doc.line_count - 1))
        line = doc.lines[row]
        last = (
            len(line) if self.mode in ("insert", "replace") else max(len(line) - 1, 0)
        )
        col = max(0, min(col, last))
        self.buffer.cursor_position = doc.translate_row_col_to_index(row, col)


def _split_eol(text: str) -> tuple[str, bool]:
    """Split *text* into (body without final newline, had final newline)."""
    if text.endswith("\n"):
        return text[:-1], True
    return text, False
