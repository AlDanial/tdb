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
from bisect import bisect_right
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import accumulate
from string import ascii_letters
from typing import TYPE_CHECKING, Any, Literal, Protocol

import prompt_toolkit.document as ptk_document
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

from tdb.vi.bindings import SEARCH_KEY, load_tdb_vi_bindings
from tdb.vi.ex import ExError, LineRecord, LineWatch, run_ex, run_substitute
from tdb.vi.marks import JUMP, MAXCOL, MarkTable, adjust_pos, edit_rows
from tdb.vi.search import ViPatternError, compile_pattern, find_nth, place
from tdb.vi.substitute import (
    Rebuilt,
    SubstituteSpec,
    count_message,
    iter_matches,
    rebuild,
)
from tdb.vi.textedit import diff_text, remember_insert

if TYPE_CHECKING:
    from prompt_toolkit.document import _DocumentCache

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

# ``:normal`` commands nested deeper than this fail (vim's E169), long
# before Python's recursion limit.
_NORMAL_DEPTH_LIMIT = 20

# Keys that end the repeat of a counted insert (vim: the insert is broken).
_CURSOR_KEYS = frozenset(
    {"up", "down", "left", "right", "home", "end", "pageup", "pagedown"}
)

# Motions that are jumps (they set the previous-jump mark), as the keys
# after any count and operator; ' and ` take any mark name.
_JUMPS = frozenset(
    {("G",), ("g", "g"), ("%",), ("(",), (")",), ("{",), ("}",), ("H",), ("M",)}
    | {("L",), ("n",), ("N",), ("*",), ("#",)}
)
# prompt_toolkit's own motions, which fail (vim's beep) when they cannot
# move the cursor at all: at a line or buffer edge, or no bracket for ``%``.
_MUST_MOVE = frozenset(
    {(k,) for k in ("h", "l", "left", "right", "backspace", " ", "w", "W", "b")}
    | {(k,) for k in ("B", "e", "E", "+", "-", "enter", "up", "down", "ctrl+n")}
    | {("g", "e"), ("g", "E"), ("%",)}
)
_OPERATORS = frozenset("dcy<>=!")
_G_OPERATORS = frozenset("~uU?q")

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
    # ``:nohlsearch`` hid the last search's highlighting; the next search
    # (``/ ? n N * #``, ``:s``, ``:g``) shows it again.
    hl_hidden: bool = False
    last_substitute: SubstituteSpec | None = None
    ex_history: list[str] = field(default_factory=list)
    search_history: list[str] = field(default_factory=list)
    last_change: list[ViKey] | None = None
    # Indexes of the count digits in ``last_change`` (a count given to `.`
    # replaces all of them).
    last_change_counts: frozenset[int] = frozenset()
    # Register of the last executed macro, for ``@@``.
    last_macro: str | None = None


# What an undo group needs to make its changes one step: the undo-stack
# length and entries, text and final-newline flag, and redo stack when
# it began.
_UndoGroup = tuple[int, list, str, bool, list]


@dataclass
class _Confirm:
    """A ``:s///c`` session waiting for y/n/a/q/l."""

    spec: SubstituteSpec
    first: int
    last: int
    lines: list[str]  # the text when the session began
    matches: list[tuple[int, int, int]]  # (row, start, end) in ``lines``
    undo: _UndoGroup
    # Marks and the ``gv`` area when it began; each step maps them anew.
    marks: dict[str, tuple[int, int]]
    visual: tuple[tuple[int, int], tuple[int, int], bool] | None
    start: tuple[int, int]  # the cursor, for the previous-jump mark
    index: int = 0
    accepted: set[tuple[int, int, int]] = field(default_factory=set)
    done: Rebuilt | None = None


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


class _LineStarts:
    """A text's line start offsets, split at the row of its last edit:
    rows ``0 .. hlen - 1`` absolute (``head``), the rows after them
    relative to the end of the text (``tail[toff:]``: start minus *size*,
    the text's length).

    An edit leaves the tail's values alone, so the next text's table
    shares this one's lists: an edit at or after the split row appends to
    ``head`` (when no other table has, the entries past ``hlen`` being no
    one else's) and moves ``toff`` on.  Typing and ``:%normal`` cost O(1)
    instead of a scan of every line (see ``_derive_cache``).  Neither list
    is ever changed below a table's ``hlen`` / at or after its ``toff``.
    """

    __slots__ = ("head", "hlen", "size", "tail", "toff")

    def __init__(
        self, head: list[int], hlen: int, tail: list[int], toff: int, size: int
    ) -> None:
        self.head, self.hlen, self.tail, self.toff, self.size = (
            head,
            hlen,
            tail,
            toff,
            size,
        )

    def __len__(self) -> int:
        return self.hlen + len(self.tail) - self.toff

    def __getitem__(self, row: int) -> int:
        """Start of *row* (negative rows count from the end, as a list)."""
        if row < 0:
            row += len(self)
            if row < 0:
                raise IndexError(row)
        if row < self.hlen:
            return self.head[row]
        return self.tail[self.toff + row - self.hlen] + self.size

    def find(self, index: int) -> tuple[int, int]:
        """``(row, start)`` of the line holding text offset *index*."""
        tail, toff, size = self.tail, self.toff, self.size
        if toff < len(tail) and index >= tail[toff] + size:
            k = bisect_right(tail, index - size, toff) - 1
            return self.hlen + k - toff, tail[k] + size
        pos = bisect_right(self.head, index, 0, self.hlen) - 1
        return pos, self.head[pos]


def _line_start_indexes(self: Document) -> list[int] | _LineStarts:
    """``Document._line_start_indexes``: without a Python loop, or the
    split table ``_derive_cache`` left.

    prompt_toolkit builds this table (the offset each line starts at) with
    a Python ``for`` loop for every new text, so each edit cost a loop over
    every line, and ``:%normal`` on 10,000 lines spent most of its seconds
    here.  ``accumulate`` gives the same list at C speed.
    """
    cache = self._cache
    if cache.line_indexes is None:
        indexes = list(accumulate(map((1).__add__, map(len, self.lines)), initial=0))
        if len(indexes) > 1:
            indexes.pop()  # the last one is not the start of a line
        cache.line_indexes = indexes
    return cache.line_indexes


def _find_line_start_index(self: Document, index: int) -> tuple[int, int]:
    """``Document._find_line_start_index`` over either kind of table."""
    starts = self._line_start_indexes
    if type(starts) is list:
        pos = bisect_right(starts, index) - 1
        return pos, starts[pos]
    return starts.find(index)


def _translate_row_col_to_index(self: Document, row: int, col: int) -> int:
    """``Document.translate_row_col_to_index`` (same code) over either kind
    of table."""
    starts = self._line_start_indexes
    lines = self.lines
    try:
        result = starts[row]
        line = lines[row]
    except IndexError:
        if row < 0:
            result = starts[0]
            line = lines[0]
        else:
            result = starts[-1]
            line = lines[-1]
    result += max(0, min(col, len(line)))
    return max(0, min(result, len(self.text)))


@dataclass(frozen=True)
class _DocumentInternals:
    """The private prompt_toolkit names the fast line tables rely on."""

    cache_type: type  # ``_DocumentCache``
    line_list: type  # ``_ImmutableLineList``
    caches: Any  # ``_text_to_document_cache``: text -> its ``_DocumentCache``


# The three Document attributes the patch replaces.
_PATCHED = (
    "_line_start_indexes",
    "_find_line_start_index",
    "translate_row_col_to_index",
)


def _document_internals() -> _DocumentInternals | None:
    """prompt_toolkit's ``Document`` internals, or None when they are not
    what the patch expects (a prompt_toolkit that renamed them): then
    ``Document`` is left alone and prompt_toolkit's own, slower, line
    tables are used."""
    try:
        internals = _DocumentInternals(
            ptk_document._DocumentCache,
            ptk_document._ImmutableLineList,
            ptk_document._text_to_document_cache,
        )
        probe = internals.cache_type()
        needed = [(probe, "lines"), (probe, "line_indexes")]
        needed += [(internals.caches, "get"), (Document, "__slots__")]
        needed += [(Document, name) for name in _PATCHED]
        for owner, name in needed:
            if not hasattr(owner, name):
                raise AttributeError(name)
        if "_cache" not in Document.__slots__:
            raise AttributeError("Document._cache")
    except (AttributeError, TypeError) as error:
        log.debug("prompt_toolkit Document internals changed (%s): slow path", error)
        return None
    return internals


# Document's own attributes, before the patch (for tests of the slow path).
_DOCUMENT_ORIGINALS = {
    name: Document.__dict__[name] for name in _PATCHED if name in Document.__dict__
}
# None: the internals changed, and every user below takes the slow path.
_PTK = _document_internals()
if _PTK is not None:
    # prompt_toolkit is pinned below 3.1; these are the only readers of the
    # line-start table (``Document._line_start_indexes``).
    Document._line_start_indexes = property(_line_start_indexes)  # type: ignore[method-assign,assignment]
    Document._find_line_start_index = _find_line_start_index  # type: ignore[method-assign]
    Document.translate_row_col_to_index = _translate_row_col_to_index  # type: ignore[method-assign]


def _derive_cache(
    ptk: _DocumentInternals,
    old: str,
    known: _DocumentCache,
    new: str,
    cache: _DocumentCache,
) -> None:
    """Fill *cache* (lines, line starts of *new*) from *known* (those of
    *old*) by the one edit between the texts: a list copy and slices in C,
    or less (see ``_LineStarts``), instead of prompt_toolkit's split and
    scan of the whole new text."""
    lines, starts = known.lines, known.line_indexes
    edit = diff_text(old, new)
    if lines is None or starts is None or edit is None:
        return
    if type(starts) is list:
        # The old text's own table becomes this view of its list, so the
        # rows a later table appends to the list are not the old text's.
        starts = known.line_indexes = _LineStarts(  # type: ignore[assignment]
            starts, len(starts), [], 0, len(old)
        )
    first, first_start = starts.find(edit.start)
    last, last_start = starts.find(edit.old_end)
    mid = (
        lines[first][: edit.start - first_start]
        + edit.new_text
        + lines[last][edit.old_end - last_start :]
    ).split("\n")
    new_lines = ptk.line_list(lines)
    list.__setitem__(new_lines, slice(first, last + 1), mid)
    head, hlen, tail, toff, size = (
        starts.head,
        starts.hlen,
        starts.tail,
        starts.toff,
        starts.size,
    )
    # Rows 0 .. first, absolute.
    if first + 1 == hlen and len(head) == hlen:
        new_head = head  # extended in place: no table uses what is past hlen
    elif first < hlen:
        new_head = head[: first + 1]
    else:
        new_head = head if len(head) == hlen else head[:hlen]
        new_head += [t + size for t in tail[toff : toff + first + 1 - hlen]]
    pos = new_head[first]
    for line in mid[:-1]:
        pos += len(line) + 1
        new_head.append(pos)
    # The rows after the edit keep their place relative to the end.
    if last + 1 >= hlen:
        new_tail, new_toff = tail, toff + last + 1 - hlen
    else:
        new_tail = [x - size for x in head[last + 1 : hlen]] + tail[toff:]
        new_toff = 0
    cache.lines = new_lines
    cache.line_indexes = _LineStarts(  # type: ignore[assignment]
        new_head, first + len(mid), new_tail, new_toff, len(new)
    )


class _ViBuffer(Buffer):
    """A Buffer whose undo/redo history also covers the final-newline flag.

    Same algorithm as prompt_toolkit's ``Buffer`` undo, with stack entries
    of ``(text, cursor_position, eol)`` instead of ``(text, cursor_position)``.
    """

    eol: bool = False
    # While a ``:normal`` inside ``:g`` runs: the line changes of the key
    # (see ``LineRecord``), or None for one nobody recorded.
    line_record: list[LineRecord | None] | None = None
    # The undo-stack length when the outermost open undo group began (a
    # `.` change or insert session, a macro run, an ex command), or None.
    undo_floor: Callable[[], int | None] = staticmethod(lambda: None)
    # True while prompt_toolkit handles a key: its save before every key
    # must not clear the redo history (the engine clears it on a change).
    keep_redo = False
    # The text the last undo or redo put back.
    restored: str | None = None
    # Inside a group: a change since the group's entry was saved.
    _grouped_change = False
    # The text and its shared Document cache (None on the slow path) as of
    # the last change.
    _known: tuple[str, _DocumentCache | None] | None = None
    # While insert_text runs: (text before, cursor offset, inserted text).
    _inserting: tuple[str, int, str] | None = None

    def insert_text(
        self,
        data: str,
        overwrite: bool = False,
        move_cursor: bool = True,
        fire_event: bool = True,
    ) -> None:
        """prompt_toolkit's, noting the insert so the change need not be
        found by comparing the texts (typing, ``:%normal A...``)."""
        if not overwrite:
            self._inserting = self.text, self.cursor_position, data
        try:
            super().insert_text(data, overwrite, move_cursor, fire_event)
        finally:
            self._inserting = None

    def _text_changed(self) -> None:
        """Before prompt_toolkit's handling: the new text's lines and line
        starts come from the previous text's (see ``_derive_cache``)."""
        new = self.text
        known = self._known
        inserting = self._inserting
        if inserting is not None and known is not None and inserting[0] is known[0]:
            remember_insert(inserting[0], inserting[1], inserting[2], new)
        ptk, cache = _PTK, None
        if ptk is not None:
            cache = ptk.caches.get(new)
            if cache is None:
                cache = ptk.caches[new] = ptk.cache_type()
            if cache.lines is None and known is not None and known[1] is not None:
                _derive_cache(ptk, known[0], known[1], new, cache)
        self._known = new, cache
        super()._text_changed()

    def record_rows(self, first: int, deleted: int, inserted: int) -> None:
        """Note a linewise change: at row *first*, *deleted* lines went
        and *inserted* new ones came."""
        if self.line_record is not None:
            self.line_record.append(("rows", first, deleted, inserted))

    def insert_line_above(self, copy_margin: bool = True) -> None:
        row = self.document.cursor_position_row
        super().insert_line_above(copy_margin)
        self.record_rows(row, 0, 1)

    def insert_line_below(self, copy_margin: bool = True) -> None:
        row = self.document.cursor_position_row
        super().insert_line_below(copy_margin)
        self.record_rows(row + 1, 0, 1)

    def cut_selection(self) -> ClipboardData:
        sel = self.selection_state
        doc = self.document
        count = doc.line_count
        first = doc.cursor_position_row
        if sel is not None:
            other = doc.translate_index_to_position(sel.original_cursor_position)[0]
            first = min(first, other)
        data = super().cut_selection()
        if sel is not None and sel.type == SelectionType.LINES:
            if not self.text:
                self.record_rows(0, count, 1)
            else:
                self.record_rows(first, count - self.document.line_count, 0)
        return data

    def _state(self) -> tuple[str, int, bool]:
        return self.text, self.cursor_position, self.eol

    def save_to_undo_stack(self, clear_redo_stack: bool = True) -> None:
        """Save the state before a change (prompt_toolkit: before every key).

        Inside an undo group whose state before is saved nothing more is
        added: the group becomes that one step anyway, and as in vim a
        ``u`` inside it (``:normal xxu``) undoes the block so far.  Without
        this every key of a ``:%normal`` kept a copy of the whole text.
        """
        undo: list = self._undo_stack
        floor = self.undo_floor()
        grouped = floor is not None and len(undo) > floor
        if grouped and self._grouped_change:
            pass
        elif undo and undo[-1][0] == self.text and undo[-1][2] == self.eol:
            undo[-1] = self._state()
        elif grouped:
            self._grouped_change = True
        else:
            undo.append(self._state())
            self._grouped_change = False
        if clear_redo_stack and not self.keep_redo:
            self._redo_stack = []

    def undo(self) -> bool:  # type: ignore[override]
        """Take back one change; False when there is none."""
        undo: list = self._undo_stack
        redo: list = self._redo_stack
        while undo:
            text, pos, eol = undo.pop()
            if text != self.text or eol != self.eol:
                # Undo and redo both put the cursor where the change began.
                redo.append((self.text, pos, self.eol))
                self._restore(text, pos, eol)
                return True
        return False

    def redo(self) -> None:
        """Each redo is an undo step of its own, even inside a group."""
        undo: list = self._undo_stack
        redo: list = self._redo_stack
        if redo:
            if undo and undo[-1][0] == self.text and undo[-1][2] == self.eol:
                undo[-1] = self._state()
            else:
                undo.append(self._state())
            self._restore(*redo.pop())

    def _restore(self, text: str, pos: int, eol: bool) -> None:
        """Put back *text* with the cursor at *pos*, on a character."""
        doc = Document(text, 0)
        row, col = doc.translate_index_to_position(min(pos, len(text)))
        col = max(0, min(col, len(doc.lines[row]) - 1))
        self.eol = eol
        self.document = Document(text, doc.translate_row_col_to_index(row, col))
        self.restored = self.text  # (not document.text: a cache by value)


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
        tabstop: int = 8,
        _extra_bindings: KeyBindings | None = None,
    ) -> None:
        self.session = session if session is not None else ViSession()
        self.marks = marks if marks is not None else MarkTable()
        if self.marks.get(JUMP) is None:
            self.marks.set(JUMP, (0, 0))  # vim: the file was entered at its top
        self.viewport = viewport
        self.indent_unit = indent_unit
        # Screen columns a tab reaches to the next multiple of (``N|``);
        # the editor passes the width its TextArea draws tabs with.
        self.tabstop = max(tabstop, 1)
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
        # Text of the ':', '/' or '?' line (``_line_kind``) while it is
        # open, else None.
        self._cmdline: str | None = None
        self._line_kind = ":"
        # The last ':' '/' '?' line closed, as shown (``":w"``; ``""``
        # when Esc abandoned it), and how many lines have closed: a
        # status line shows it until a mode message replaces it.
        self.last_line = ""
        self.lines_closed = 0
        # Browsing the line's history: index of the entry shown (None when
        # not browsing) and the text typed before browsing began.
        self._history_pos: int | None = None
        self._history_prefix = ""
        # Cursor offset when the '/' or '?' line was opened, and the count
        # typed before it (prompt_toolkit's raw ``arg``).
        self._search_from = 0
        self._search_arg: str | None = None
        # Index in ``_change`` where the search line's keys begin.
        self._search_change_at: int | None = None
        # The last Visual selection as (anchor, cursor, linewise), for `gv`.
        self._last_visual_raw: tuple[tuple[int, int], tuple[int, int], bool] | None = (
            None
        )
        # Keys of the normal/Visual command being given to prompt_toolkit,
        # and the cursor when it began (the previous-jump mark if it jumps).
        self._cmd_keys: list[str] = []
        self._cmd_start: tuple[int, int] = (0, 0)
        # A `{count}i/a/I/A/o/O` insert waiting for its Esc: the command key,
        # the count, and the index in ``_change`` where the typed keys begin.
        self._insert_repeat: tuple[str, int, int | None] | None = None
        # Set by an ex command, whose changes already moved the marks.
        self._marks_adjusted = False
        # Told of each key's text change (``:normal`` inside ``:g``).
        # (watch, queue depth of the keys it watches)
        self._line_watch: tuple[LineWatch, int] | None = None
        self._queue_depth = 0
        # How many ``:normal`` commands are running inside each other.
        self._normal_depth = 0
        # The ``:s///c`` session being answered, or None.
        self._confirm: _Confirm | None = None
        # Set by ``fail()`` while a key is processed.
        self._failed = False
        # Open undo groups (ex commands), innermost last; the undo-stack
        # length when the key being fed began; and that length while it
        # runs a macro (the run is one undo step).
        self._open_groups: list[_UndoGroup] = []
        self._feed_undo_len = 0
        self._macro_floor: int | None = None

        body, eol = _split_eol(text)
        self.buffer = _ViBuffer(document=Document(body, 0), multiline=True)
        self.buffer.eol = eol
        self.buffer.undo_floor = self._undo_floor  # type: ignore[method-assign]
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
        marks_before = self.marks.snapshot(), self._last_visual_raw
        self._pending_keys.append(key)
        self._queue.append((key, False))
        self._ran_macro = False
        self._feed_undo_len = len(before[2])
        try:
            self._run_queue()
            self._macro_floor = None
            if self._ran_macro:  # vim: undo takes back a whole macro run
                doc, eol, undo, redo = before
                if self._confirm is not None:
                    # The macro left a :s///c session open: it and the
                    # macro's other changes become one step when it ends.
                    self._confirm.undo = (len(undo), undo, doc.text, eol, redo)
                else:
                    self._group_undo(len(undo), doc.text, eol)
        except Exception as exc:  # a stray key must never crash tdb
            log.exception("vi engine error on key %r", key.name)
            self._macro_floor = None
            self._recover(*before)
            self.marks.restore(marks_before[0])
            self._last_visual_raw = marks_before[1]
            self._events.append(Notify(f"vi: {key.notation} failed: {exc}", "warning"))
            return
        if not self._key_pending():
            self._pending_keys.clear()

    def _run_queue(self) -> None:
        """Process queued keys; `.` and ``@`` push their keys to the front."""
        replayed = 0
        self._queue_depth += 1
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
                text = self.buffer.text
                typing = self.mode in ("insert", "replace")
                at = self.buffer.cursor_position if typing else None
                self._marks_adjusted = False
                row = self.cursor[0]
                watch = self._watch_key()
                self._failed = False
                self._dispatch(key)
                if not self._marks_adjusted:
                    self._adjust_marks(text, at)
                self._marks_adjusted = False
                self._watched_key(watch, text, row)
                if self._failed:
                    self._failed = False
                    # vim's beep_flush: the rest of the macro, `.` or
                    # :normal is dropped.  Typed keys are never queued.
                    self._queue = deque(item for item in self._queue if not item[1])
        finally:
            self.is_replaying = False
            self._queue_depth -= 1

    def _watch_key(
        self,
    ) -> tuple[LineWatch | None, list[LineRecord | None], int, int] | None:
        """Before a key: start its record of line changes, if watched.

        Only keys of the watched ``:normal`` are reported; keys they run
        in turn (a ``:normal`` inside) add their records to theirs.
        """
        buff = self.buffer
        if self._line_watch is not None and self._line_watch[1] == self._queue_depth:
            buff.line_record = []
            return self._line_watch[0], buff.line_record, 0, buff.document.line_count
        record = buff.line_record
        if record is not None:
            return None, record, len(record), buff.document.line_count
        return None

    def _watched_key(self, watch, text: str, row: int) -> None:
        """After a key: a line count change no binding recorded spoils the
        record; a watched key reports its change."""
        if watch is None:
            return
        report, record, size, count = watch
        if self.buffer.document.line_count != count and len(record) == size:
            record.append(None)
        if report is None:
            return
        self.buffer.line_record = None
        if self.buffer.text != text:
            cache = None if _PTK is None else _PTK.caches.get(text)
            lines = cache.lines if cache is not None else None  # known lines
            report(
                text.split("\n") if lines is None else lines,
                row,
                self.cursor[0],
                record,
            )

    def fail(self) -> None:
        """The command being run failed (vim's beep): an unmatched motion or
        object, an edge of the buffer, an error.  The remaining keys of a
        macro, `.`, count run or ``:normal`` are dropped once the key ends;
        ``:normal`` keeps its own keys, so ``:g`` goes on to the next line."""
        self._failed = True

    def unsupported(self, keys: str) -> None:
        """An unknown or unsupported command *keys*: notify, and fail."""
        self._events.append(Notify(f"Not supported: {keys}", "warning"))
        self.fail()

    def command_notation(self) -> str:
        """The keys of the normal/Visual command being run, without its
        count, in vim notation (``g?``, ``dQ``)."""
        keys = self._cmd_keys
        return "".join(ViKey(k).notation for k in keys[_skip_count(keys, 0) :])

    def _replay(self, keys: list[ViKey]) -> None:
        """Run *keys* next, before anything already queued."""
        self._queue.extendleft((k, True) for k in reversed(keys))

    def _dispatch(self, key: ViKey) -> None:
        """One key: engine-level commands, else prompt_toolkit."""
        if self._confirm is not None:
            self._feed_confirm(key)
            self._marks_adjusted = True  # each step already mapped them
            return
        if self._cmdline is not None:
            self._feed_cmdline(key)
            return
        if self._wait is not None:
            self._finish_wait(key)
            return
        name = key.name
        idle = self.mode == "normal" and not self._ptk_busy()
        if name == ":" and self._open_ex_line():
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
        self._begin_motion(key)
        self._feed_ptk(key)
        self._end_motion(text_before)
        if visual_before is not None:
            if self.buffer.selection_state is None:
                self.last_visual_raw = visual_before  # the selection just ended
            if self.buffer.text != text_before:
                self._undo_to_visual_start(visual_before, text_before)
        self._track_insert_repeat(count_insert, name)
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

    def _begin_motion(self, key: ViKey) -> None:
        """Before a normal/Visual key goes to prompt_toolkit: note the command."""
        if self.mode not in ("normal", "visual", "visual-line"):
            self._cmd_keys = []
            return
        if not self._ptk_pending():
            self._cmd_keys, self._cmd_start = [], self.cursor
        self._cmd_keys.append(key.name)

    def count_typed(self) -> bool:
        """Whether the normal/Visual command being run was given a count,
        before or after its operator (``d1G``, ``1dG``)."""
        return any(len(k) == 1 and k.isdigit() for k in self._cmd_keys)

    def _end_motion(self, text_before: str) -> None:
        """After the key: a finished jump sets the previous-jump mark.

        As vim's ``checkpcmark``, a jump that did not move the cursor (or
        change the text) leaves the mark alone.
        """
        if not self._cmd_keys or self._ptk_pending():
            return
        keys, self._cmd_keys = self._cmd_keys, []
        moved = self.cursor != self._cmd_start or self.buffer.text != text_before
        if _is_jump(keys) and moved:
            self.marks.set(JUMP, self._cmd_start)
        start = _skip_count(keys, 0)
        motion = tuple(keys[start:])
        if not moved and motion in _MUST_MOVE and not (motion == ("%",) and start):
            self.fail()  # (``N%`` goes to a line: never a failure)

    def _adjust_marks(self, old: str, typed_at: int | None = None) -> None:
        """After a text change from *old*: move the marks and the ``gv`` area.

        *typed_at* is the cursor offset before a key typed in insert mode.
        """
        new = self.buffer.text
        if new == old:
            return
        edit = diff_text(old, new)
        assert edit is not None
        cache = None if _PTK is None else _PTK.caches.get(old)
        starts = cache.line_indexes if cache is not None else None
        row = None
        if type(starts) is _LineStarts:  # the old text's rows are known
            row = starts.find(edit.start)[0]
        first, old_last, new_last, cols = edit_rows(edit, old, typed_at, row)
        self.marks.adjust(first, old_last, new_last, last_cols=cols)
        raw = self._last_visual_raw
        if raw is not None:
            anchor, cursor, linewise = raw
            anchor, cursor = (
                adjust_pos(p, first, old_last, new_last, keep=True, last_cols=cols) or p
                for p in (anchor, cursor)
            )
            self._last_visual_raw = anchor, cursor, linewise

    def _feed_ptk(self, key: ViKey) -> None:
        """Hand *key* to prompt_toolkit (not recorded for `.`)."""
        self._process(key.to_key_press())

    def _process(self, press: KeyPress) -> None:
        """prompt_toolkit handles *press*.

        Its handlers save the undo state before every key, which also
        cleared the redo history, even for a motion or a count (``uj<C-r>``,
        ``3<C-r>``).  Here only a change that is not an undo or redo clears it.
        """
        buff, kp = self.buffer, self.app.key_processor
        before = buff.text, buff.eol
        buff.keep_redo = True
        try:
            with set_app(self.app):
                kp.feed(press)
                kp.process_keys()
        finally:
            buff.keep_redo = False
        if buff.text is not buff.restored and (buff.text, buff.eol) != before:
            buff._redo_stack = []

    def _undo_floor(self) -> int | None:
        """The undo-stack length when the outermost open undo group began:
        a `.` change (and insert session), a macro run or an ex command."""
        marks = [group[0] for group in self._open_groups]
        if self._change is not None:
            marks.append(self._undo_mark)
        if self._macro_floor is not None:
            marks.append(self._macro_floor)
        if self._confirm is not None:
            marks.append(self._confirm.undo[0])
        return min(marks) if marks else None

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

    def _track_insert_repeat(self, count: int | None, key_name: str = "") -> None:
        """After a key: note a counted insert starting, or one that ended."""
        if self.mode != "insert" or key_name in _CURSOR_KEYS:
            self._insert_repeat = None  # leaving, or moving the cursor, ends it
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
    def last_visual_raw(
        self,
    ) -> tuple[tuple[int, int], tuple[int, int], bool] | None:
        """The last Visual area as ``(anchor, cursor, linewise)``, for ``gv``."""
        return self._last_visual_raw

    @last_visual_raw.setter
    def last_visual_raw(
        self, raw: tuple[tuple[int, int], tuple[int, int], bool] | None
    ) -> None:
        """Remember a Visual area that ended; also sets ``'<`` and ``'>``."""
        self._last_visual_raw = raw
        if raw is None:
            return
        anchor, cursor, linewise = raw
        start, end = min(anchor, cursor), max(anchor, cursor)
        if linewise:  # vim: the whole lines, from column 0 to MAXCOL
            start, end = (start[0], 0), (end[0], MAXCOL)
        self.marks.set("<", start)
        self.marks.set(">", end)

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
            elif name == "escape":
                self.fail()
            else:
                self.unsupported("q" + key.notation)
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
        floor = self._undo_floor()
        if pre is None and floor is not None and floor < mark:
            return  # inside a group that saved the state before it: one step
        if pre is None or pre[0] != text or pre[2] != eol:
            pre = (text, 0, eol)
        undo.append(pre)

    def _repeat_change(self, count: int | None) -> None:
        """``.``: replay the last change, a *count* replacing its own."""
        keys = self.session.last_change
        if not keys:
            self.fail()
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
                self.fail()
                return
            name = self.session.last_macro
        if len(name) != 1 or not (name in ascii_letters or name in '"0'):
            if name == "escape":
                self.fail()
            else:
                self.unsupported("@" + ViKey(name).notation)
            return
        name = name.lower()
        if name == '"':
            data = self.session.clipboard.get_data()  # type: ignore[attr-defined]
        else:
            data = self.session.vi_state.named_registers.get(name)  # type: ignore[attr-defined]
        if data is None:
            self.fail()
            return
        self.session.last_macro = name
        self._ran_macro = True
        if self._macro_floor is None:
            self._macro_floor = self._feed_undo_len
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
        buff._grouped_change = False

    def _recover(self, *snapshot) -> None:
        """After an error: the pre-key state, nothing pending or queued."""
        self._restore(*snapshot)
        if self._confirm is not None:
            confirm, self._confirm = self._confirm, None
            self._close_undo_group(confirm.undo)
        self._pending_keys.clear()
        self._queue.clear()
        self._wait = self._register = self._change = self._rejected = None
        self._cmdline = None
        self._open_groups.clear()

    # ------------------------------------------------------------------
    # The ':' command line (ex commands, see tdb.vi.ex) and the '/' '?'
    # search line

    def _open_line(self, kind: str, arg: str | None = None) -> None:
        """Open the ``:``, ``/`` or ``?`` line; *arg* is a search's count.

        ``/`` and ``?`` come from their prompt_toolkit binding, so they
        work after an operator (``d/x<CR>``) and in Visual mode.
        """
        self._line_kind, self._cmdline = kind, ""
        self._history_pos = None
        self._search_from = self.buffer.cursor_position
        self._search_arg = arg
        change = self._change
        self._search_change_at = (
            len(change) if kind != ":" and change is not None else None
        )

    def _open_ex_line(self) -> bool:
        """``:`` in normal mode (``3:`` gives ``.,.+2``) or Visual mode
        (``'<,'>``); False when ``:`` is not a command here."""
        mode = self.mode
        if (
            mode not in ("normal", "visual", "visual-line")
            or self._ptk_busy()
            or self._wait is not None
            or self._register is not None
        ):
            return False
        # Not part of a `.` change (a count or a Visual area may have begun one).
        self._change = None
        if mode == "normal":
            count = self._take_count()
            if count is None:
                prefill = ""
            else:
                prefill = "." if count == 1 else f".,.+{count - 1}"
        else:
            self.app.key_processor.arg = None
            visual = self._visual_state()
            assert visual is not None
            self.last_visual_raw = visual
            self.buffer.exit_selection()
            # As vim, the cursor goes to the start of the area (a linewise
            # one ending below the cursor: column 0 of its first line).
            anchor, cursor, linewise = visual
            if linewise:
                if anchor < cursor:
                    self._move_to(anchor[0], 0)
            else:
                self._move_to(*min(anchor, cursor))
            prefill = "'<,'>"
        self._open_line(":")
        self._cmdline = prefill
        return True

    def open_filter_line(self, first: int, last: int) -> None:
        """``!{motion}``: the ``:`` line holding the motion's lines, as vim
        (``!j`` gives ``:.,.+1!``); ex refuses the filter."""
        self._change = None
        self._move_to(first, self.cursor[1])
        self._open_line(":")
        self._cmdline = ("." if last == first else f".,.+{last - first}") + "!"

    def _history(self) -> list[str]:
        if self._line_kind == ":":
            return self.session.ex_history
        return self.session.search_history

    @staticmethod
    def _remember(history: list[str], entry: str) -> None:
        """Add *entry* to *history*, moving an earlier copy to the end."""
        if entry:
            if entry in history:
                history.remove(entry)
            history.append(entry)

    def _remember_search(self, pattern: str) -> None:
        self._remember(self.session.search_history, pattern)

    def _feed_cmdline(self, key: ViKey) -> None:
        searching = self._line_kind != ":"
        if searching and self._change is not None:
            self._change.append(key)  # `d/x<CR>` is repeated by `.`
        line = self._cmdline or ""
        name = key.name
        if name == "escape" or (name == "backspace" and not line):
            self.last_line = ""
            self.lines_closed += 1
            self._close_line()
            if searching:
                self._cancel_operator()
                self._end_search()
        elif name == "backspace":
            self._edit_line(line[:-1])
        elif name in ("up", "down"):
            self._recall(older=name == "up")
        elif name == "enter":
            at = self._search_change_at
            if searching and self._change is not None and at is not None:
                # `.` repeats the pattern as accepted, not the editing keys
                # (<Up>, <BS>...) that produced it; a macro keeps those.
                self._change[at:] = [ViKey(c) for c in line] + [key]
            self._remember(self._history(), line)
            self.last_line = self._line_kind + line
            self.lines_closed += 1
            self._close_line()
            if searching:
                self._run_search(line)
                self._end_search()
            else:
                self._run_cmdline(line)
        elif len(name) == 1:
            self._edit_line(line + name)

    def _edit_line(self, line: str) -> None:
        self._cmdline = line
        self._history_pos = None
        self._incsearch()

    def _recall(self, *, older: bool) -> None:
        """Up / Down: the previous / next history entry starting as typed."""
        history = self._history()
        if self._history_pos is None:
            if not older:
                return
            self._history_prefix = self._cmdline or ""
            pos = len(history)
        else:
            pos = self._history_pos
        prefix = self._history_prefix
        step = -1 if older else 1
        pos += step
        while 0 <= pos < len(history) and not history[pos].startswith(prefix):
            pos += step
        if 0 <= pos < len(history):
            self._history_pos, self._cmdline = pos, history[pos]
        elif not older:
            self._history_pos, self._cmdline = None, prefix
        self._incsearch()

    def _close_line(self) -> None:
        """Close the line, putting back the cursor an incremental search moved."""
        if self._cmdline is not None and self._line_kind != ":":
            self.buffer.cursor_position = self._search_from
        self._cmdline = None
        self._history_pos = None
        self._cmd_keys = []

    def _search_count(self) -> int:
        """The count typed before ``/`` (times an operator's count)."""
        arg = self._search_arg
        op_arg = self.app.vi_state.operator_arg
        count = int(arg) if arg and arg.isdigit() else 1
        return count * (op_arg or 1)

    def _incsearch(self) -> None:
        """While typing a search: the cursor on the match it would go to."""
        if self._line_kind == ":":
            return
        pos = self._search_from
        if self._cmdline:
            try:
                rx = compile_pattern(self._cmdline)
            except ViPatternError:
                rx = None
            text = self.buffer.text
            found = rx and find_nth(
                text, rx, pos, self._line_kind == "?", self._search_count()
            )
            if found:
                pos = place(text, found[0])
        self.buffer.cursor_position = pos

    def _cancel_operator(self) -> None:
        """Drop a pending operator and count (a search that did not happen)."""
        vs = self.app.vi_state
        vs.operator_func = None
        vs.operator_arg = None
        self.app.key_processor.arg = None

    def _run_search(self, line: str) -> None:
        """``<CR>`` on the search line: the search, as a motion."""
        backward = self._line_kind == "?"
        pattern = line
        if not pattern and self.session.last_search is not None:
            pattern = self.session.last_search[0]
        error = None
        if not pattern:
            error = "No previous regular expression"
        else:
            try:
                compile_pattern(pattern)
            except ViPatternError as exc:
                error = f"Invalid pattern: {exc}"
        if error is not None:
            self._events.append(Notify(error, "error"))
            self._cancel_operator()
            self.fail()
            return
        self.session.last_search = (pattern, backward)
        self.session.hl_hidden = False
        start, text = self.cursor, self.buffer.text
        self.app.key_processor.arg = self._search_arg
        self._process(KeyPress(SEARCH_KEY, ""))  # SEARCH_KEY's binding: `n`
        if self.cursor != start or self.buffer.text != text:
            self.marks.set(JUMP, start)

    def _end_search(self) -> None:
        """After the search line closed: the command it was part of ends."""
        if not self._ptk_pending() and self._rejected is None:
            self._register = None
        self._end_command()

    def _run_cmdline(self, line: str) -> None:
        """``<CR>`` on the ``:`` line: run it as an ex command."""
        try:
            run_ex(line, _ExHost(self))
        except ExError as exc:
            self._events.append(Notify(str(exc), "error"))
            self.fail()
        finally:
            # The marks already followed each change the command made.
            self._marks_adjusted = True

    def _goto_row(self, row: int) -> None:
        """``:N``: go to *row*, keeping the column (nvim nostartofline)."""
        start = self.cursor
        self._move_to(row, self.cursor[1])
        if self.cursor != start:
            self.marks.set(JUMP, start)

    def _run_keys(self, keys: list[ViKey]) -> None:
        """Run *keys* now, as replayed keys (``:normal``), then normal mode.

        As vim's ``:normal``, an unfinished command is ended as by Esc.
        """
        if self._normal_depth >= _NORMAL_DEPTH_LIMIT:
            raise ExError("Command too recursive")
        queue, replaying = self._queue, self.is_replaying
        self._queue = deque((k, True) for k in keys)
        self._normal_depth += 1
        try:
            self._run_queue()
            if self._cmdline is not None:
                self._close_line()
                if self._line_kind != ":":
                    self._cancel_operator()
                    self._end_search()
            self._wait = None
            if self._key_pending() or self.mode != "normal":
                self._queue.append((ViKey("escape"), True))
                self._run_queue()
            self._register = None
        finally:
            self._normal_depth -= 1
            self._queue, self.is_replaying = queue, replaying

    # ------------------------------------------------------------------
    # Undo groups and the ``:s///c`` confirm session

    def _open_undo_group(self) -> _UndoGroup:
        buff = self.buffer
        undo = list(buff._undo_stack)
        return len(undo), undo, buff.text, buff.eol, list(buff._redo_stack)

    def _close_undo_group(self, group: _UndoGroup) -> None:
        """Make the changes since ``_open_undo_group`` one undo step.

        A change undone by keys inside (``:normal u``) is left as those
        undos made it, so ``<C-r>`` still redoes it; no net change leaves
        no step and keeps the redo.
        """
        mark, before, text, eol, redo = group
        buff = self.buffer
        undo = buff._undo_stack
        if len(undo) >= mark and undo[:mark] == before:
            if (buff.text, buff.eol) != (text, eol):
                self._group_undo(mark, text, eol)
                buff._redo_stack = []
            else:
                del undo[mark:]
                buff._redo_stack = redo

    def _start_confirm(self, spec: SubstituteSpec, first: int, last: int) -> None:
        """Begin a ``:s///c`` session at its first match."""
        lines = list(self.buffer.document.lines)
        self._confirm = _Confirm(
            spec=spec,
            first=first,
            last=last,
            lines=lines,
            matches=list(iter_matches(lines, first, last, spec)),
            undo=self._open_undo_group(),
            marks=self.marks.snapshot(),
            visual=self._last_visual_raw,
            start=self.cursor,
        )
        self._confirm_goto()

    def _confirm_position(self, index: int) -> tuple[int, int]:
        """Where match *index* of the session is in the current text."""
        c = self._confirm
        assert c is not None
        match = c.matches[index]
        if c.done is None:
            return match[0], match[1]
        return c.done.positions[match]

    def _confirm_goto(self) -> None:
        assert self._confirm is not None
        self._move_to(*self._confirm_position(self._confirm.index))

    def _feed_confirm(self, key: ViKey) -> None:
        """One answer: y n a q l (Esc / Ctrl+C: q); other keys are ignored."""
        c = self._confirm
        assert c is not None
        name = key.name
        match = c.matches[c.index]
        if name in ("q", "escape", "ctrl+c"):
            self._end_confirm(self._confirm_position(c.index))
        elif name in ("y", "l"):
            c.accepted.add(match)
            self._confirm_apply()
            if name == "l" or c.index == len(c.matches) - 1:
                assert c.done is not None
                self._end_confirm((c.done.end_row(match[0]), 0))
            else:
                c.index += 1
                self._confirm_goto()
        elif name == "n":
            if c.index == len(c.matches) - 1:
                self._end_confirm(self._confirm_position(c.index))
            else:
                c.index += 1
                self._confirm_goto()
        elif name == "a":
            c.accepted.update(c.matches[c.index :])
            self._confirm_apply()
            assert c.done is not None
            row = c.done.last_row
            line = c.done.lines[row]
            self._end_confirm((row, len(line) - len(line.lstrip(" \t"))))

    def _confirm_apply(self) -> None:
        """Put the accepted replacements into the text (marks follow)."""
        c = self._confirm
        assert c is not None
        c.done = done = rebuild(c.lines, c.first, c.last, c.spec, c.accepted)
        if "\n".join(done.lines) == self.buffer.text:
            return
        marks = dict(c.marks)
        marks[JUMP] = c.start  # vim: the jump mark is where :s began
        self.marks.restore(marks)
        self._last_visual_raw = c.visual
        first = min(r for r, _, _ in c.accepted)
        _ExHost(self).replace_lines(
            done.lines, self.cursor, row_map=done.row_map, undo_cursor=(first, 0)
        )

    def _end_confirm(self, cursor: tuple[int, int]) -> None:
        """End the session: one undo step, the cursor, the count report."""
        c = self._confirm
        assert c is not None
        self._confirm = None
        self._close_undo_group(c.undo)
        self._move_to(*cursor)
        if len(c.accepted) > 2:
            rows = {r for r, _, _ in c.accepted}
            self._events.append(Notify(count_message(len(c.accepted), len(rows))))

    def _end_confirm_outside(self) -> None:
        """A change from outside the keys (revert, paste, click) ends an
        open ``:s///c`` session first, keeping what it replaced so far."""
        if self._confirm is not None:
            self._end_confirm(self.cursor)

    def substitute_again(self, count: int | None, *, every_line: bool) -> None:
        """Normal-mode ``&`` (the last ``:s`` on *count* lines, without its
        flags) and ``g&`` (it on every line with the last search pattern,
        keeping its flags).  Neither is a change ``.`` repeats."""
        self._change = None
        if not every_line:
            self._run_cmdline("s" if count in (None, 1) else f".,.+{count - 1}s")
            return
        host = _ExHost(self)
        last, search = self.session.last_substitute, host.last_search()
        try:
            if last is None or not search:
                raise ExError("No previous regular expression")
            spec = SubstituteSpec(search, last.replacement, last.flags)
            with host.undo_group():
                run_substitute(spec, 0, len(host.lines()) - 1, host)
        except ExError as exc:
            self._events.append(Notify(str(exc), "error"))
            self.fail()
        finally:
            self._marks_adjusted = True

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
        """``normal insert replace visual visual-line command search confirm``."""
        if self._confirm is not None:
            return "confirm"
        if self._cmdline is not None:
            return "command" if self._line_kind == ":" else "search"
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
    def highlight_pattern(self) -> str | None:
        """The pattern whose matches are highlighted (vim's ``'hlsearch'``):
        the one being typed on the ``/`` ``?`` line, else the last search
        unless ``:nohlsearch`` hid it.  None: highlight nothing."""
        if self._cmdline and self._line_kind != ":":
            return self._cmdline
        last = self.session.last_search
        if last is None or self.session.hl_hidden:
            return None
        return last[0]

    @property
    def has_pending(self) -> bool:
        """True while a command is incomplete or the mode is not normal."""
        return self._key_pending() or self.mode != "normal"

    @property
    def status(self) -> str:
        """``:cmd`` / ``/pat`` line, the ``:s///c`` prompt, pending keys
        (``"a2d``), ``recording @a``, or ``""``."""
        if self._confirm is not None:
            return f"replace with {self._confirm.spec.replacement} (y/n/a/q/l)?"
        if self._cmdline is not None:
            return self._line_kind + self._cmdline
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
        self._end_confirm_outside()
        if self.buffer.selection_state is not None:
            self.last_visual_raw = self._visual_state()
        self.buffer.exit_selection()
        self._move_to(row, col)

    def select(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        """Select characterwise from *start* to *end* (inclusive); enters visual."""
        self._end_confirm_outside()
        self.buffer.exit_selection()
        self.app.vi_state.input_mode = InputMode.NAVIGATION
        self._move_to(*start)
        self.buffer.start_selection(SelectionType.CHARACTERS)
        self._move_to(*end)

    def insert_text(self, text: str) -> None:
        """Insert *text* (a paste) at the cursor as one undo step; mode is
        unchanged.

        With the ``:``, ``/`` or ``?`` line open, its first line goes into
        that line instead (vim's paste into the command line).  A pending
        command (count, operator, register, ``r``) is cancelled first.
        """
        if self._cmdline is not None:
            self._edit_line(self._cmdline + text.split("\n", 1)[0])
            return
        self._end_confirm_outside()
        self._cancel_pending()
        old, at = self.buffer.text, self.buffer.cursor_position
        self.buffer.save_to_undo_stack()
        self.buffer.insert_text(text)
        if self.mode == "normal":
            self._move_to(*self.cursor)  # keep the cursor on a character
        self._adjust_marks(old, at)  # a paste is typed text

    def _cancel_pending(self) -> None:
        """Drop an unfinished normal/Visual command, as Esc would."""
        vs = self.app.vi_state
        if vs.input_mode == InputMode.REPLACE_SINGLE:
            vs.input_mode = InputMode.NAVIGATION  # a pending r
        if self.mode in ("insert", "replace") or not self._key_pending():
            return
        self.app.key_processor.reset()
        vs.operator_func = None
        vs.operator_arg = None
        self._wait = self._register = self._rejected = self._change = None
        self._pending_keys.clear()
        self._cmd_keys = []

    def reset_text(self, text: str) -> None:
        """Replace the whole buffer (undoable), keeping the cursor row/col."""
        self._end_confirm_outside()
        row, col = self.cursor
        old = self.buffer.text
        self.buffer.exit_selection()
        self.buffer.save_to_undo_stack()
        body, self.buffer.eol = _split_eol(text)
        self.buffer.set_document(Document(body, 0), bypass_readonly=True)
        self._move_to(row, col)
        self._adjust_marks(old)

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


class _ExHost:
    """``ExHost`` over a ``ViEngine``: what ``:`` commands act on."""

    def __init__(self, engine: ViEngine) -> None:
        self._e = engine

    def lines(self) -> list[str]:
        # prompt_toolkit's own list, which cannot be changed in place (no
        # command changes it): a copy per line made :%normal quadratic.
        return self._e.buffer.document.lines

    def cursor_row(self) -> int:
        return self._e.cursor[0]

    def cursor_col(self) -> int:
        return self._e.cursor[1]

    def set_cursor(self, row: int, col: int) -> None:
        self._e._move_to(row, col)

    def mark(self, name: str) -> tuple[int, int] | None:
        return self._e.marks.get(name)

    def set_mark(self, name: str, pos: tuple[int, int]) -> None:
        self._e.marks.set(name, pos)

    def replace_lines(
        self,
        lines: list[str],
        cursor: tuple[int, int],
        *,
        row_map: Sequence[int] | None = None,
        undo_cursor: tuple[int, int] | None = None,
    ) -> None:
        e = self._e
        buff = e.buffer
        old = buff.text
        was_empty = not old and not buff.eol
        new = "\n".join(lines)
        if new == old:
            e._move_to(*cursor)  # e.g. a move to where the lines are
            return
        if undo_cursor is None:
            row = _first_changed_row(buff.document.lines, lines, row_map)
            undo_cursor = row, e.cursor[1]
        # The undo entry holds the cursor ``u`` puts back.
        buff.cursor_position = buff.document.translate_row_col_to_index(*undo_cursor)
        buff.save_to_undo_stack()
        if was_empty and new:
            buff.eol = True  # the empty buffer got lines
        if buff.line_record is not None:
            if row_map is None:
                buff.line_record.append(None)
            else:
                buff.line_record.append(("map", list(row_map), max(len(lines), 1)))
        buff.set_document(Document(new, 0), bypass_readonly=True)
        e._move_to(*cursor)
        if row_map is None:
            e._adjust_marks(old)
            return
        to = _rows_after(row_map, len(lines))
        marks = {}
        for name, (r, c) in e.marks.snapshot().items():
            if 0 <= r < len(to):
                if row_map[r] < 0 and name not in (JUMP, "<", ">"):
                    continue  # its line was deleted
                r = to[r]
            marks[name] = r, c
        e.marks.restore(marks)
        raw = e._last_visual_raw
        if raw is not None:
            (ar, ac), (cr, cc), linewise = raw
            last = len(to) - 1
            e._last_visual_raw = (
                (to[min(ar, last)], ac),
                (to[min(cr, last)], cc),
                linewise,
            )

    def set_register(
        self, name: str | None, text: str, linewise: bool, *, yank: bool = False
    ) -> None:
        e = self._e
        kind = SelectionType.LINES if linewise else SelectionType.CHARACTERS
        saved, e._register = e._register, name
        try:
            e.registers.store(ClipboardData(text, kind), yank=yank)
        finally:
            e._register = saved

    def run_normal(
        self, row: int | None, keys: str, *, watch: LineWatch | None = None
    ) -> None:
        e = self._e
        if row is not None:
            e._move_to(row, 0)
        saved = e._line_watch
        if watch is not None:
            e._line_watch = watch, e._queue_depth + 1
        try:
            e._run_keys(ViKey.parse(keys))
        finally:
            e._line_watch = saved

    def last_search(self) -> str | None:
        last = self._e.session.last_search
        return None if last is None else last[0]

    def set_last_search(self, pattern: str) -> None:
        session = self._e.session
        backward = session.last_search[1] if session.last_search else False
        session.last_search = (pattern, backward)
        session.hl_hidden = False

    def hide_search_highlight(self) -> None:
        self._e.session.hl_hidden = True

    def last_substitute(self) -> SubstituteSpec | None:
        return self._e.session.last_substitute

    def set_last_substitute(self, spec: SubstituteSpec) -> None:
        self._e.session.last_substitute = spec

    def confirm_substitute(self, spec: SubstituteSpec, first: int, last: int) -> None:
        self._e._start_confirm(spec, first, last)

    def substitute_tally(self) -> None:
        return None

    def save(self) -> None:
        self._e._events.append(SaveRequest())

    def leave(self, discard: bool) -> None:
        self._e._events.append(LeaveRequest(discard=discard))

    def notify(self, text: str, severity: str = "information") -> None:
        self._e._events.append(Notify(text, severity))  # type: ignore[arg-type]

    def fail(self) -> None:
        self._e.fail()

    def goto_line(self, row: int) -> None:
        self._e._goto_row(row)

    @contextmanager
    def undo_group(self) -> Iterator[None]:
        """Everything the command changes is one undo step (redo cleared)."""
        e = self._e
        group = e._open_undo_group()
        e._open_groups.append(group)
        try:
            yield
        finally:
            e._open_groups.pop()  # groups nest
            e._close_undo_group(group)


def _rows_after(row_map: Sequence[int], count: int) -> list[int]:
    """Each old row's new row; a deleted one's (``-1``) is where it was:
    the row after the new row of the last line above it that is kept."""
    out: list[int] = []
    prev = -1
    for to in row_map:
        if to >= 0:
            prev = to
        out.append(to if to >= 0 else max(0, min(prev + 1, count - 1)))
    return out


def _first_changed_row(
    old: list[str], new: list[str], row_map: Sequence[int] | None
) -> int:
    """The first row of *old* a ``replace_lines`` changes (or the last row)."""
    if row_map is not None:
        moved = (r for r, to in enumerate(row_map) if r != to)
    else:
        moved = (r for r, (a, b) in enumerate(zip(old, new)) if a != b)
    return min(next(moved, len(new)), len(old) - 1)


def _skip_count(keys: list[str], i: int) -> int:
    """The index after the count (if any) that starts at ``keys[i]``."""
    if i < len(keys) and keys[i] in "123456789":
        i += 1
        while i < len(keys) and keys[i].isdigit():
            i += 1
    return i


def _is_jump(keys: list[str]) -> bool:
    """Whether the command *keys* (count, operator, count, motion) is a jump."""
    i = _skip_count(keys, 0)
    if i < len(keys) - 1 and keys[i] in _OPERATORS:
        i += 1
    elif i < len(keys) - 2 and keys[i] == "g" and keys[i + 1] in _G_OPERATORS:
        i += 2
    rest = tuple(keys[_skip_count(keys, i) :])
    return rest in _JUMPS or (len(rest) == 2 and rest[0] in ("'", "`"))


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
