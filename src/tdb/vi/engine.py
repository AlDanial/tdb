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
from collections import deque
from dataclasses import dataclass, field
from string import ascii_letters
from typing import TYPE_CHECKING, Literal, Protocol

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import Clipboard, ClipboardData, InMemoryClipboard
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

# Special ViKey name -> its code in a register's text, as vim stores it
# (K_SPECIAL byte 0x80 + two-character key code), so a recorded macro is
# plain text in its register and still round-trips.
_REGISTER_CODES: dict[str, str] = {
    "delete": "\x80kD",
    "up": "\x80ku",
    "down": "\x80kd",
    "left": "\x80kl",
    "right": "\x80kr",
    "home": "\x80kh",
    "end": "\x80@7",
    "pageup": "\x80kP",
    "pagedown": "\x80kN",
}
_REGISTER_CHARS: dict[str, str] = {
    "escape": "\x1b",
    "enter": "\r",
    "tab": "\t",
    "backspace": "\x7f",
}
_REGISTER_CODE_KEYS = {v: k for k, v in _REGISTER_CODES.items()}
# A line break in register text is <NL> (Ctrl+J), as in vim.
_REGISTER_CHAR_KEYS = {v: k for k, v in _REGISTER_CHARS.items()} | {"\n": "ctrl+j"}

# Keys replayed by `.` / macros in one feed before the replay is cut off
# (a recursive macro would otherwise never stop: vim ends it when a
# command fails, which prompt_toolkit does not report).
_REPLAY_LIMIT = 100_000

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


def keys_to_register_text(keys: list[ViKey]) -> str:
    """Encode *keys* as register text (how ``q`` stores a macro)."""
    out = []
    for key in keys:
        n = key.name
        if n in _REGISTER_CODES:
            out.append(_REGISTER_CODES[n])
        elif n in _REGISTER_CHARS:
            out.append(_REGISTER_CHARS[n])
        elif n.startswith("ctrl+"):
            out.append(chr(ord(n[5]) - ord("a") + 1))
        else:
            out.append(n)
    return "".join(out)


def register_text_to_keys(text: str) -> list[ViKey]:
    """Decode register *text* into keys (how ``@`` executes a register)."""
    codes = _REGISTER_CODE_KEYS
    chars = _REGISTER_CHAR_KEYS
    keys: list[ViKey] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\x80" and text[i : i + 3] in codes:
            keys.append(ViKey(codes[text[i : i + 3]]))
            i += 3
            continue
        if ch in chars:
            keys.append(ViKey(chars[ch]))
        elif "\x01" <= ch <= "\x1a":
            keys.append(ViKey("ctrl+" + chr(ord(ch) + ord("a") - 1)))
        elif ch.isprintable():
            keys.append(ViKey(ch))
        i += 1
    return keys


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
    # Indexes of the count digits in ``last_change`` (a count given to `.`
    # replaces all of them).
    last_change_counts: frozenset[int] = frozenset()
    # Register of the last executed macro, for ``@@``.
    last_macro: str | None = None


def _append_register(old: ClipboardData | None, new: ClipboardData) -> ClipboardData:
    """``"A``: *new* appended to *old*; linewise if either one is."""
    if old is None:
        return new
    if SelectionType.LINES in (old.type, new.type):
        return ClipboardData(old.text + "\n" + new.text, SelectionType.LINES)
    return ClipboardData(old.text + new.text, old.type)


class _Registers(Clipboard):
    """The engine's clipboard: routes reads and writes to the register prefix.

    Every prompt_toolkit and tdb binding goes through ``app.clipboard``, so
    ``"x`` works with all of them.  The unnamed register is the session's
    ``clipboard``; named ones and ``0`` live in ``vi_state.named_registers``.
    """

    def __init__(self, engine: ViEngine) -> None:
        self._engine = engine

    def set_data(self, data: ClipboardData) -> None:
        """A delete or change: the selected register, or the unnamed one."""
        self.store(data, yank=False)

    def yank(self, data: ClipboardData) -> None:
        """A yank: like ``set_data``, and also ``"0`` when no register is named."""
        self.store(data, yank=True)

    def store(self, data: ClipboardData, *, yank: bool) -> None:
        e = self._engine
        reg = e._register
        if reg == "_":
            return
        named = e.session.vi_state.named_registers  # type: ignore[attr-defined]
        if reg is not None and reg in ascii_letters:
            low = reg.lower()
            if reg.isupper():
                data = _append_register(named.get(low), data)
            named[low] = data
        elif reg == "0" or yank:
            named["0"] = data
        e.session.clipboard.set_data(data)  # type: ignore[attr-defined]

    def get_data(self) -> ClipboardData:
        e = self._engine
        reg = e._register
        if reg is None or reg == '"':
            return e.session.clipboard.get_data()  # type: ignore[attr-defined]
        if reg == "_":
            return ClipboardData()
        named = e.session.vi_state.named_registers  # type: ignore[attr-defined]
        return named.get(reg.lower()) or ClipboardData()

    def rotate(self) -> None:
        e = self._engine
        e.session.clipboard.rotate()  # type: ignore[attr-defined]


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
                # Undo and redo both put the cursor where the change began.
                redo.append((self.text, pos, self.eol))
                self._restore(text, pos, eol)
                break

    def redo(self) -> None:
        redo: list = self._redo_stack
        if redo:
            self.save_to_undo_stack(clear_redo_stack=False)
            self._restore(*redo.pop())

    def _restore(self, text: str, pos: int, eol: bool) -> None:
        """Put back *text* with the cursor at *pos*, on a character."""
        doc = Document(text, 0)
        row, col = doc.translate_index_to_position(min(pos, len(text)))
        col = max(0, min(col, len(doc.lines[row]) - 1))
        self.eol = eol
        self.document = Document(text, doc.translate_row_col_to_index(row, col))


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
        # True while keys from `.` or a macro are being processed.
        self.is_replaying = False
        self._queue: deque[tuple[ViKey, bool]] = deque()
        # Selected register ("x prefix) for the next command, or None.
        self._register: str | None = None
        # A key that waits for a register name: '"', "q" or "@".
        self._wait: str | None = None
        self._wait_count: int | None = None
        # Macro recording (q): register and keys so far.
        self._recording: str | None = None
        self._recorded: list[ViKey] = []
        # The change being recorded for `.`, and the text when it began.
        self._change: list[ViKey] | None = None
        self._change_from: tuple[str, bool] = ("", False)
        self._ran_macro = False
        # Indexes of count digits in ``_change``.
        self._change_counts: set[int] = set()
        # Undo-stack length when ``_change`` began, and the state then.
        self._undo_mark = 0
        self._change_cursor = 0
        # State before a command with an unsupported register, which is
        # rolled back when that command ends.
        self._rejected: tuple[Document, bool, list, list] | None = None
        # Text of the ':' line while in the stub command mode, else None.
        self._cmdline: str | None = None
        # The last Visual selection as (anchor, cursor, linewise), for `gv`.
        self.last_visual_raw: tuple[tuple[int, int], tuple[int, int], bool] | None = (
            None
        )
        # A `{count}i/a/I/A/o/O` insert waiting for its Esc: the command key,
        # the count, and the index in ``_change`` where the typed keys begin.
        self._insert_repeat: tuple[str, int, int | None] | None = None

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
        self.registers = _Registers(self)
        self.app.clipboard = self.registers
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
        if self._recording is not None:
            self._recorded.append(key)
        before = self._snapshot()
        self._pending_keys.append(key)
        self._queue.append((key, False))
        self._ran_macro = False
        try:
            self._run_queue()
            if self._ran_macro:  # vim: undo takes back a whole macro run
                self._group_undo(len(before[2]), before[0].text, before[1])
        except Exception as exc:  # a stray key must never crash tdb
            log.exception("vi engine error on key %r", key.name)
            self._recover(*before)
            self._events.append(Notify(f"vi: {key.notation} failed: {exc}", "warning"))
            return
        if not self._key_pending():
            self._pending_keys.clear()

    def _run_queue(self) -> None:
        """Process queued keys; `.` and ``@`` push their keys to the front."""
        replayed = 0
        try:
            while self._queue:
                key, replay = self._queue.popleft()
                if replay:
                    replayed += 1
                    if replayed > _REPLAY_LIMIT:
                        self._queue.clear()
                        self._events.append(
                            Notify(
                                f"vi: replay stopped after {_REPLAY_LIMIT} keys",
                                "warning",
                            )
                        )
                        break
                self.is_replaying = replay
                self._dispatch(key)
        finally:
            self.is_replaying = False

    def _replay(self, keys: list[ViKey]) -> None:
        """Run *keys* next, before anything already queued."""
        self._queue.extendleft((k, True) for k in reversed(keys))

    def _dispatch(self, key: ViKey) -> None:
        """One key: engine-level commands, else prompt_toolkit."""
        if self._cmdline is not None:
            self._feed_cmdline(key)
            return
        if self._wait is not None:
            self._finish_wait(key)
            return
        name = key.name
        idle = self.mode == "normal" and not self._ptk_busy()
        if idle and name == ":" and not self._key_pending():
            self._cmdline = ""
            return
        if idle and name in ("q", "@", "."):
            # Not part of a change; an abandoned count belongs to them, and
            # a register prefix is dropped (vim ignores it here).
            self._change = None
            self._register = self._rejected = None
            count = self._take_count()
            if name == ".":
                self._repeat_change(count)
            elif name == "q" and self._recording is not None:
                self._stop_recording()
            else:
                self._wait, self._wait_count = name, count
            return
        kp = self.app.key_processor
        if name == "escape" and self._insert_repeat is not None:
            self._repeat_insert()
        count_insert = self._count_insert_key(name)
        if self._change is None and self.mode == "normal" and not self._key_pending():
            self._change, self._change_counts = [], set()
            self._change_from = (self.buffer.text, self.buffer.eol)
            self._undo_mark = len(self.buffer._undo_stack)
        if self._change is not None:
            self._change.append(key)
        if (
            name == '"'
            and self.mode in ("normal", "visual", "visual-line")
            and not self._ptk_busy()
        ):
            self._wait = '"'
            return
        visual_before = self._visual_state()
        text_before = self.buffer.text
        self._feed_ptk(key)
        if visual_before is not None:
            if self.buffer.selection_state is None:
                self.last_visual_raw = visual_before  # the selection just ended
            if self.buffer.text != text_before:
                self._undo_to_visual_start(visual_before, text_before)
        self._track_insert_repeat(count_insert)
        if (
            self._change is not None
            and len(name) == 1
            and name.isdigit()
            and kp.arg is not None
            and kp.arg.endswith(name)
        ):
            # prompt_toolkit took the digit as (part of) a count, not as
            # the argument of r/f/t/m... or as inserted text.
            self._change_counts.add(len(self._change) - 1)
        if not self._ptk_pending() and self._rejected is None:
            self._register = None
        self._end_command()

    def _feed_ptk(self, key: ViKey) -> None:
        """Hand *key* to prompt_toolkit (not recorded for `.`)."""
        kp = self.app.key_processor
        with set_app(self.app):
            kp.feed(key.to_key_press())
            kp.process_keys()

    def _count_insert_key(self, name: str) -> int | None:
        """The count if *name* starts a repeated ``i a I A o O`` insert."""
        kp = self.app.key_processor
        if (
            name in ("i", "a", "I", "A", "o", "O")
            and self.mode == "normal"
            and not self._ptk_busy()
            and kp.arg is not None
            and kp.arg.isdigit()
            and int(kp.arg) > 1
        ):
            return int(kp.arg)
        return None

    def _track_insert_repeat(self, count: int | None) -> None:
        """After a key: note a counted insert starting, or one that ended."""
        if self.mode != "insert":
            self._insert_repeat = None
        elif count is not None and self._change is not None:
            self._insert_repeat = (self._change[-1].name, count, len(self._change))

    def _repeat_insert(self) -> None:
        """Esc ending a ``{count}i`` insert: type the text again first.

        The typed keys are replayed through prompt_toolkit (as vim replays
        its redo buffer), ``o``/``O`` opening a new line before each copy.
        """
        rep, self._insert_repeat = self._insert_repeat, None
        if rep is None or self.mode != "insert" or self._change is None:
            return
        cmd, count, start = rep
        typed = self._change[start:]
        if not typed:
            return
        for _ in range(count - 1):
            if cmd in ("o", "O"):
                self._feed_ptk(ViKey("enter"))
            for k in typed:
                self._feed_ptk(k)

    def _visual_state(self) -> tuple[tuple[int, int], tuple[int, int], bool] | None:
        """``(anchor, cursor, linewise)`` of the Visual selection, or None."""
        sel = self.buffer.selection_state
        if sel is None:
            return None
        doc = self.buffer.document
        return (
            doc.translate_index_to_position(sel.original_cursor_position),
            (doc.cursor_position_row, doc.cursor_position_col),
            sel.type == SelectionType.LINES,
        )

    def _undo_to_visual_start(
        self,
        visual: tuple[tuple[int, int], tuple[int, int], bool],
        text_before: str,
    ) -> None:
        """After a Visual command: ``u`` puts the cursor where the area began."""
        undo: list = self.buffer._undo_stack
        if not undo or undo[-1][0] != text_before:
            return
        anchor, cursor, linewise = visual
        row, col = min(anchor, cursor)
        if linewise and anchor <= cursor:
            col = 0
        doc = Document(text_before, 0)
        row = min(row, doc.line_count - 1)
        col = min(col, len(doc.lines[row]))
        undo[-1] = (text_before, doc.translate_row_col_to_index(row, col), undo[-1][2])

    @property
    def last_visual(self) -> tuple[tuple[int, int], tuple[int, int], bool] | None:
        """The last Visual area as ``(start, end, linewise)``, start <= end."""
        raw = self.last_visual_raw
        if raw is None:
            return None
        anchor, cursor, linewise = raw
        return min(anchor, cursor), max(anchor, cursor), linewise

    def _finish_wait(self, key: ViKey) -> None:
        """The register name after ``"``, ``q`` or ``@``."""
        kind, self._wait = self._wait, None
        name = key.name
        if kind == '"':
            if self._change is not None:
                self._change.append(key)
            self._select_register(name)
            if name == "escape":
                self._end_command()
        elif kind == "q":
            if len(name) == 1 and name in ascii_letters:
                self._recording, self._recorded = name, []
        elif kind == "@":
            self._run_macro(name, self._wait_count or 1)

    def _select_register(self, name: str) -> None:
        if len(name) == 1 and (name in ascii_letters or name in '"0_'):
            self._register = name
        elif name == "escape":
            self.app.key_processor.arg = None  # cancels the whole command
        else:
            if name in ("+", "*"):
                text = "Clipboard registers are not supported"
            else:
                text = f"Register {ViKey(name).notation} is not supported"
            self._events.append(Notify(text, "warning"))
            # The command still runs, then its changes are rolled back.
            self._register = "_"
            self._rejected = self._snapshot()

    def _take_count(self) -> int | None:
        """The typed count (and clear it), or None."""
        kp = self.app.key_processor
        arg, kp.arg = kp.arg, None
        return int(arg) if arg and arg.isdigit() else None

    def _end_command(self) -> None:
        """Once back in normal mode: undo a rejected command, keep the `.` change."""
        if (
            self._cmdline is not None
            or self.mode != "normal"
            or self._ptk_pending()
            or self._wait is not None
            or (self._register is not None and self._rejected is None)
        ):
            return
        if self._rejected is not None:
            self._restore(*self._rejected)
            self._rejected = self._register = None
        if self._change is None:
            return
        keys, self._change = self._change, None
        if (self.buffer.text, self.buffer.eol) == self._change_from:
            return
        counts = frozenset(self._change_counts)
        command = _command_key(keys, counts)
        if command is not None and command.name in ("u", "ctrl+r"):
            return
        self._group_undo(self._undo_mark, *self._change_from)
        self.session.last_change = keys
        self.session.last_change_counts = counts

    def _group_undo(self, mark: int, text: str, eol: bool) -> None:
        """Make everything pushed on the undo stack since *mark* one step.

        The single entry left is the state before the change, so ``u``
        takes the whole change back (an insert session, a `.`, a macro).
        """
        undo: list = self.buffer._undo_stack
        if len(undo) < mark:
            return
        pre = undo[mark] if len(undo) > mark else None
        del undo[mark:]
        if undo and undo[-1][0] == text and undo[-1][2] == eol:
            return  # the pre-change state was merged into the entry below
        if pre is None or pre[0] != text or pre[2] != eol:
            pre = (text, 0, eol)
        undo.append(pre)

    def _repeat_change(self, count: int | None) -> None:
        """``.``: replay the last change, a *count* replacing its own."""
        keys = self.session.last_change
        if not keys:
            return
        if count is not None:
            counts = self.session.last_change_counts
            keys = [k for i, k in enumerate(keys) if i not in counts]
            at = 2 if len(keys) > 1 and keys[0].name == '"' else 0
            keys = keys[:at] + ViKey.parse(str(count)) + keys[at:]
        self._replay(keys)

    def _run_macro(self, name: str, count: int) -> None:
        """``{count}@x``: replay register *x* (``@@``: the last one run)."""
        if name == "@":
            if self.session.last_macro is None:
                return
            name = self.session.last_macro
        if len(name) != 1 or not (name in ascii_letters or name in '"0'):
            return
        name = name.lower()
        if name == '"':
            data = self.session.clipboard.get_data()  # type: ignore[attr-defined]
        else:
            data = self.session.vi_state.named_registers.get(name)  # type: ignore[attr-defined]
        if data is None:
            return
        self.session.last_macro = name
        self._ran_macro = True
        text = data.text + ("\n" if data.type == SelectionType.LINES else "")
        self._replay(register_text_to_keys(text) * count)

    def _stop_recording(self) -> None:
        """``q`` while recording: store the keys (without this ``q``)."""
        reg, self._recording = self._recording, None
        keys = self._recorded if self.is_replaying else self._recorded[:-1]
        self._recorded = []
        assert reg is not None
        named = self.session.vi_state.named_registers  # type: ignore[attr-defined]
        text = keys_to_register_text(keys)
        if reg.isupper() and reg.lower() in named:
            text = named[reg.lower()].text + text
        named[reg.lower()] = ClipboardData(text)

    def feed_keys(self, notation: str) -> None:
        """Parse vim *notation* and feed each key."""
        for key in ViKey.parse(notation):
            self.feed(key)

    def _snapshot(self) -> tuple[Document, bool, list, list]:
        """Buffer, final-newline flag and undo/redo history, for ``_restore``."""
        buff = self.buffer
        return buff.document, buff.eol, list(buff._undo_stack), list(buff._redo_stack)

    def _restore(self, document: Document, eol: bool, undo: list, redo: list) -> None:
        """Put back a ``_snapshot`` and normal mode."""
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

    def _recover(self, *snapshot) -> None:
        """After an error: the pre-key state, nothing pending or queued."""
        self._restore(*snapshot)
        self._pending_keys.clear()
        self._queue.clear()
        self._wait = self._register = self._change = self._rejected = None

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
        if self._recording:
            return f"recording @{self._recording}"
        return ""

    def _key_pending(self) -> bool:
        """Unprocessed keys, a register prefix, a count or an operator."""
        return (
            self._ptk_pending() or self._wait is not None or self._register is not None
        )

    def _ptk_pending(self) -> bool:
        """prompt_toolkit holds keys, a count or an operator."""
        return self._ptk_busy() or self.app.key_processor.arg is not None

    def _ptk_busy(self) -> bool:
        """prompt_toolkit holds keys or an operator (a count alone is not busy)."""
        kp = self.app.key_processor
        return bool(
            kp.key_buffer
            or kp.input_queue
            or self.app.vi_state.operator_func is not None
        )

    # ------------------------------------------------------------------
    # Programmatic changes

    def set_cursor(self, row: int, col: int) -> None:
        """Move the cursor (clamped), leaving visual mode."""
        if self.buffer.selection_state is not None:
            self.last_visual_raw = self._visual_state()
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


def _command_key(keys: list[ViKey], counts: frozenset[int]) -> ViKey | None:
    """The first key of *keys* that is not a count digit or a ``"x`` prefix."""
    i = 0
    while i < len(keys):
        if i in counts:
            i += 1
        elif keys[i].name == '"':
            i += 2
        else:
            return keys[i]
    return None


def _split_eol(text: str) -> tuple[str, bool]:
    """Split *text* into (body without final newline, had final newline)."""
    if text.endswith("\n"):
        return text[:-1], True
    return text, False
