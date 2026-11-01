"""The ``:`` (ex) command line: parsing, line ranges and the basic commands.

Pure Python: no prompt_toolkit or Textual imports.  The engine runs a
command through an ``ExHost``, which owns the text, cursor, marks and
registers.  Rows are 0-based; the line numbers typed in an address are
1-based, as in vim.

Supported here: ``:w :q :q! :wq :x :N :d :y :m :t :co :normal :s :&``
(``:s`` itself is in ``tdb.vi.substitute``) and ``:g :g! :v`` (in
``tdb.vi.global_cmd``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from tdb.vi.search import ViPatternError
from tdb.vi.substitute import (
    SubstituteError,
    SubstituteSpec,
    confirm_flag,
    count_message,
    parse_substitute,
    quiet_flag,
    rebuild,
)


class ExError(Exception):
    """A failed ex command; the message is shown as an error notification."""


@dataclass
class ExRange:
    """The raw address text of a range (``"'a"``, ``"$-1"``, ``"%"``).

    ``end`` is None for a single address; ``start`` is ``"%"`` for the
    whole file.
    """

    start: str | None
    end: str | None


@dataclass
class ExCommand:
    """One parsed command line; ``name`` is the full command name
    (``"delete"`` for ``d``), or ``""`` for a bare range (``:5``)."""

    range: ExRange | None
    name: str
    bang: bool
    args: str


@dataclass
class SubstituteTally:
    """The ``:s`` counts of a whole ``:g``, reported once when it ends."""

    substitutions: int = 0
    lines: int = 0


# What a key did to the lines, as the code that did it knows:
# ``("rows", first, deleted, inserted)``: at row *first*, *deleted* lines
# went and *inserted* new lines came; ``("map", row_map, count)``: each
# old row's new row (-1: deleted) and the new line count.
LineRecord = tuple

# ``watch(old_lines, row_before, row_after, record)``: a key changed the
# text from *old_lines*; the cursor row before and after it, and its
# records in order (``None`` in it: a change nothing recorded).
LineWatch = Callable[[list[str], int, int, list], None]


class ExHost(Protocol):
    """What ex commands need from the editor."""

    def lines(self) -> list[str]:
        """The buffer's lines (at least one; ``[""]`` when empty)."""
        ...

    def cursor_row(self) -> int: ...

    def cursor_col(self) -> int: ...

    def set_cursor(self, row: int, col: int) -> None:
        """Put the cursor at *row*, *col* (clamped), setting no mark."""
        ...

    def mark(self, name: str) -> tuple[int, int] | None:
        """Mark *name* (``a``-``z``, ``<``, ``>``, ``'``), or None if unset."""
        ...

    def set_mark(self, name: str, pos: tuple[int, int]) -> None: ...

    def replace_lines(
        self,
        lines: list[str],
        cursor: tuple[int, int],
        *,
        row_map: Sequence[int] | None = None,
        undo_cursor: tuple[int, int] | None = None,
    ) -> None:
        """Make *lines* the buffer (``[]`` leaves one empty line), cursor
        at *cursor* (clamped).  *row_map* gives every old row's new row
        (``-1``: the line was deleted) so marks follow their lines; a
        deleted line's marks are deleted, but ``'`` ``<`` ``>`` go to
        where it was.  Without it the marks follow a diff of the text.
        *undo_cursor* is where ``u`` puts the cursor (default: the first
        changed line, keeping the column)."""
        ...

    def set_register(
        self, name: str | None, text: str, linewise: bool, *, yank: bool = False
    ) -> None:
        """Store *text* as a delete (or with *yank* a yank) would."""
        ...

    def run_normal(
        self, row: int | None, keys: str, *, watch: LineWatch | None = None
    ) -> None:
        """Run normal-mode *keys* with the cursor at the start of *row*
        (None: where it is), then end any unfinished command.  *watch* is
        called after each key that changed the text."""
        ...

    def last_search(self) -> str | None: ...

    def set_last_search(self, pattern: str) -> None: ...

    def last_substitute(self) -> SubstituteSpec | None: ...

    def set_last_substitute(self, spec: SubstituteSpec) -> None: ...

    def confirm_substitute(self, spec: SubstituteSpec, first: int, last: int) -> None:
        """Start asking about each match of *spec* in rows *first*..*last*
        (there is at least one); the answers are keys typed later."""
        ...

    def substitute_tally(self) -> SubstituteTally | None:
        """Inside ``:g``: where ``:s`` adds its counts instead of reporting
        them (and finding nothing is no error); None elsewhere."""
        ...

    def save(self) -> None: ...

    def leave(self, discard: bool) -> None: ...

    def notify(self, text: str, severity: str = "information") -> None: ...

    def fail(self) -> None:
        """The command failed without an ``ExError`` (vim's error message
        shown as a warning): a running macro stops."""
        ...

    def goto_line(self, row: int) -> None:
        """Jump to *row* (sets the previous-jump mark), keeping the column."""
        ...

    def undo_group(self) -> AbstractContextManager[None]:
        """Changes made inside are one undo step."""
        ...


# Full command name -> shortest abbreviation accepted (vim's).
_NAMES: dict[str, str] = {
    "delete": "d",
    "yank": "y",
    "move": "m",
    "t": "t",
    "copy": "co",
    "normal": "norm",
    "write": "w",
    "quit": "q",
    "wq": "wq",
    "xit": "x",
    "substitute": "s",
    "global": "g",
    "vglobal": "v",
}
_ALIASES = {"t": "copy"}

# Commands whose argument keeps its trailing white space.
_KEEP_TRAILING = frozenset({"normal", "substitute", "global", "vglobal", "&"})

_ADDRESS = re.compile(r"(?:\d+|[.$]|'.)?(?:[+-]\d*)*")
_OFFSET = re.compile(r"([+-])(\d*)")
_NAME = re.compile(r"[A-Za-z]+|&")

# Registers ``:d`` / ``:y`` accept.
_REGISTERS = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"_')


def parse_ex(line: str) -> ExCommand:
    """Split a command line into range, command name, ``!`` and argument."""
    s = line.lstrip(" \t:")
    rng: ExRange | None = None
    i = 0
    if s.startswith("%"):
        rng, i = ExRange("%", None), 1
    else:
        m = _ADDRESS.match(s)
        start, i = m.group(0) or None, m.end()
        if s.startswith(",", i):
            m = _ADDRESS.match(s, i + 1)
            end, i = m.group(0) or None, m.end()
            rng = ExRange(start or ".", end or ".")  # vim: ",5" is ".,5"
        elif start is not None:
            rng = ExRange(start, None)
    while s.startswith((" ", "\t"), i):
        i += 1
    m = _NAME.match(s, i)
    name = ""
    if m is not None:
        name, i = _full_name(m.group(0)), m.end()
    bang = s.startswith("!", i)
    if bang:
        i += 1
    args = s[i:].lstrip(" \t")
    if name not in _KEEP_TRAILING:
        args = args.rstrip()
    return ExCommand(rng, name, bang, args)


def _full_name(typed: str) -> str:
    """The full name for an abbreviation such as ``co`` or ``norm``."""
    for full, shortest in _NAMES.items():
        if full.startswith(typed) and typed.startswith(shortest):
            return _ALIASES.get(full, full)
    return typed


def _line(address: str, host: ExHost) -> int:
    """The 1-based line number *address* names (0 and past-the-end allowed)."""
    m = re.fullmatch(r"(\d+|[.$]|'.)?((?:[+-]\d*)*)", address)
    if m is None:
        raise ExError(f"Invalid address: {address}")
    base, offsets = m.groups()
    if base is None or base == ".":
        n = host.cursor_row() + 1
    elif base == "$":
        n = len(host.lines())
    elif base.startswith("'"):
        pos = host.mark(base[1])
        if pos is None:
            raise ExError(f"Mark not set: {base[1]}")
        n = pos[0] + 1
    else:
        n = int(base)
    for sign, digits in _OFFSET.findall(offsets):
        step = int(digits) if digits else 1
        n += step if sign == "+" else -step
    return n


def resolve_range(
    rng: ExRange | None, host: ExHost, default: Literal["current", "all"]
) -> tuple[int, int]:
    """The 0-based inclusive rows of *rng* (*default* when there is none).

    Line 0 means line 1; a backwards range is swapped silently.
    """
    count = len(host.lines())
    if rng is None:
        if default == "all":
            return 0, count - 1
        row = host.cursor_row()
        return row, row
    if rng.start == "%":
        return 0, count - 1
    first = _line(rng.start or ".", host)
    last = first if rng.end is None else _line(rng.end, host)
    if not (0 <= first <= count and 0 <= last <= count):
        raise ExError("Invalid range")
    if first > last:
        first, last = last, first
    return max(first, 1) - 1, max(last, 1) - 1


def _destination(address: str, host: ExHost) -> int:
    """The line (1-based, 0 = above the first) ``:m`` / ``:t`` put lines after."""
    if not address:
        raise ExError("Invalid address")
    n = _line(address, host)
    if not 0 <= n <= len(host.lines()):
        raise ExError("Invalid range")
    return n


def _first_non_blank(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _register_and_count(
    cmd: ExCommand, first: int, last: int, count_lines: int
) -> tuple[str | None, int, int]:
    """``:d [x] [count]`` / ``:y [x] [count]``: the register and the rows."""
    args = cmd.args
    reg = None
    if args and not args[0].isdigit():
        reg, args = args[0], args[1:].lstrip()
        if reg in "+*":
            raise ExError("Clipboard registers are not supported")
        if reg not in _REGISTERS:
            raise ExError(f"Register {reg} is not supported")
    if args:
        if not args.isdigit() or int(args) == 0:
            raise ExError(f"Trailing characters: {args}")
        first = last
        last = min(first + int(args) - 1, count_lines - 1)
    return reg, first, last


def _delete(cmd: ExCommand, host: ExHost) -> None:
    lines = host.lines()
    first, last = resolve_range(cmd.range, host, "current")
    reg, first, last = _register_and_count(cmd, first, last, len(lines))
    host.set_register(reg, "\n".join(lines[first : last + 1]), True)
    rest = lines[:first] + lines[last + 1 :]
    row = min(first, max(len(rest) - 1, 0))
    col = _first_non_blank(rest[row]) if rest else 0
    n = last - first + 1
    # The row map says which lines went, also among identical ones.
    row_map = [*range(first), *[-1] * n, *range(first, len(lines) - n)]
    host.replace_lines(rest, (row, col), row_map=row_map)


def _yank(cmd: ExCommand, host: ExHost) -> None:
    lines = host.lines()
    first, last = resolve_range(cmd.range, host, "current")
    reg, first, last = _register_and_count(cmd, first, last, len(lines))
    host.set_register(reg, "\n".join(lines[first : last + 1]), True, yank=True)


def _undo_row(saves: list[tuple[int, int]], cursor_row: int) -> int:
    """The row vim's ``u`` puts the cursor on after a command.

    *saves* are the command's undo saves, oldest first, as ``(top, size)``:
    the 1-based line above the change and how many lines ``u`` puts back;
    *cursor_row* is the cursor when the first save was made.  As vim's
    ``u_undoredo``: the saves are undone newest first, and the first one
    whose area holds the cursor keeps it there, else the cursor goes to
    the first line put back.
    """
    lnum = cursor_row + 1
    new = None  # vim's newlnum: the line above the cursor
    for i, (top, size) in enumerate(reversed(saves)):
        if new is not None and top >= new:
            continue
        if top <= lnum <= top + size + 1:
            new = lnum - 1
        elif size > 0 or (new is None and i == len(saves) - 1):
            new = top
    return (lnum if new is None else new + 1) - 1


def _move(cmd: ExCommand, host: ExHost) -> None:
    lines = host.lines()
    first, last = resolve_range(cmd.range, host, "current")
    dest = _destination(cmd.args, host)  # lines go after line `dest`
    if first < dest <= last:
        raise ExError("Cannot move a range of lines into itself")
    row, col = host.cursor_row(), host.cursor_col()
    n = last - first + 1
    rows = list(range(len(lines)))
    if dest > last:
        order = rows[:first] + rows[last + 1 : dest] + rows[first : last + 1]
        order += rows[dest:]
        end_row = dest - 1
    else:
        order = rows[:dest] + rows[first : last + 1] + rows[dest:first]
        order += rows[last + 1 :]
        end_row = dest + n - 1
    row_map = [0] * len(lines)
    for new, old in enumerate(order):
        row_map[old] = new
    # vim moves by copying after `dest`, then deleting the original lines.
    shift = n if dest < first else 0
    saves = [(dest, 0), (first + shift, n)]
    host.replace_lines(
        [lines[r] for r in order],
        (end_row, col),
        row_map=row_map,
        undo_cursor=(_undo_row(saves, row), col),
    )


def _copy(cmd: ExCommand, host: ExHost) -> None:
    lines = host.lines()
    first, last = resolve_range(cmd.range, host, "current")
    dest = _destination(cmd.args, host)
    row, col = host.cursor_row(), host.cursor_col()
    block = lines[first : last + 1]
    n = len(block)
    row_map = [r if r < dest else r + n for r in range(len(lines))]
    host.replace_lines(
        lines[:dest] + block + lines[dest:],
        (dest + n - 1, col),
        row_map=row_map,
        undo_cursor=(_undo_row([(dest, 0)], row), col),
    )


def _normal(cmd: ExCommand, host: ExHost) -> None:
    """``:[range]normal {keys}``: *keys* at the start of each line of the
    range, or at the cursor without one.

    As vim, the lines are numbers counted up from the first, not marked
    lines: after ``dd`` the next run is on the line below the one that
    moved up, and a number past the end is clamped to the last line.
    """
    if not cmd.args:
        raise ExError("Argument required")
    if cmd.range is None:
        host.run_normal(None, cmd.args)
        return
    first, last = resolve_range(cmd.range, host, "current")
    for n in range(first, last + 1):
        host.run_normal(min(n, len(host.lines()) - 1), cmd.args)


def _goto(cmd: ExCommand, host: ExHost) -> None:
    """``:N`` / ``:'a``: go to the (last) line of the range."""
    assert cmd.range is not None
    if cmd.range.start == "%":
        n = len(host.lines())
    else:
        n = _line(cmd.range.end or cmd.range.start or ".", host)
    host.goto_line(max(1, min(n, len(host.lines()))) - 1)


def _no_args(cmd: ExCommand) -> None:
    if cmd.args:
        raise ExError(f"Trailing characters: {cmd.args}")


def _whole_buffer(cmd: ExCommand) -> None:
    """``:w`` and friends write the whole buffer; a range is refused."""
    if cmd.range is not None:
        if cmd.bang:
            raise ExError("Writing part of the buffer is not supported")
        raise ExError("Use ! to write partial buffer")


def _write(cmd: ExCommand, host: ExHost) -> None:
    _no_args(cmd)
    _whole_buffer(cmd)
    host.save()


def _quit(cmd: ExCommand, host: ExHost) -> None:
    _no_args(cmd)
    host.leave(cmd.bang)


def _write_quit(cmd: ExCommand, host: ExHost) -> None:
    _no_args(cmd)
    _whole_buffer(cmd)
    host.save()
    host.leave(False)


def _substitute(cmd: ExCommand, host: ExHost) -> None:
    """``:[range]s/pat/rep/[flags]`` (``:s [flags]`` repeats the last one)."""
    _substitute_with(cmd.args, cmd, host)


def _repeat_substitute(cmd: ExCommand, host: ExHost) -> None:
    """``:[range]&[&][flags]``: the last ``:s`` again."""
    args = cmd.args.strip()
    if args and not (args[0].isalnum() or args[0] == "&"):
        raise ExError(f"Trailing characters: {args}")
    _substitute_with(args, cmd, host)


def _substitute_with(args: str, cmd: ExCommand, host: ExHost) -> None:
    try:
        spec = parse_substitute(
            args, last=host.last_substitute(), last_search=host.last_search()
        )
    except SubstituteError as exc:
        raise ExError(str(exc)) from None
    first, last = resolve_range(cmd.range, host, "current")
    run_substitute(spec, first, last, host)


def run_substitute(spec: SubstituteSpec, first: int, last: int, host: ExHost) -> None:
    """Substitute *spec* in rows *first*..*last* (or start confirming).

    As vim: the pattern becomes the last search, the cursor goes to the
    first non-blank of the last changed line, the previous-jump mark to
    where the cursor was, and the count is reported when it is more than
    vim's default ``'report'`` (2).
    """
    lines = host.lines()
    try:
        done = rebuild(lines, first, last, spec)
    except ViPatternError as exc:
        raise ExError(f"Invalid pattern: {exc}") from None
    except SubstituteError as exc:
        raise ExError(str(exc)) from None
    host.set_last_substitute(spec)
    host.set_last_search(spec.pattern)
    tally = host.substitute_tally()
    if not done.substitutions:
        if not quiet_flag(spec) and tally is None:
            host.notify(f"Pattern not found: {spec.pattern}", "warning")
            host.fail()  # vim's E486
        return
    if confirm_flag(spec):
        host.confirm_substitute(spec, first, last)
        return
    host.set_mark("'", (host.cursor_row(), host.cursor_col()))
    row = done.last_row
    first_changed = min(r for r, _, _ in done.positions)
    host.replace_lines(
        done.lines,
        (row, _first_non_blank(done.lines[row])),
        row_map=done.row_map,
        undo_cursor=(first_changed, 0),
    )
    if tally is not None:
        tally.substitutions += done.substitutions
        tally.lines += done.lines_changed
    elif done.substitutions > 2:
        host.notify(count_message(done.substitutions, done.lines_changed))


def _global(cmd: ExCommand, host: ExHost) -> None:
    """``:[range]g[!]/pat/cmd``; ``!`` is ``:v``."""
    from tdb.vi.global_cmd import run_global  # it imports this module

    run_global(cmd, host, invert=cmd.bang)


def _vglobal(cmd: ExCommand, host: ExHost) -> None:
    """``:[range]v/pat/cmd``: *cmd* on the lines that do not match."""
    from tdb.vi.global_cmd import run_global

    if cmd.bang:
        raise ExError("No ! allowed")
    run_global(cmd, host, invert=True)


# Commands that change the text (run as one undo step).
COMMANDS: dict[str, Callable[[ExCommand, ExHost], None]] = {
    "delete": _delete,
    "move": _move,
    "copy": _copy,
    "normal": _normal,
    "substitute": _substitute,
    "&": _repeat_substitute,
    "global": _global,
    "vglobal": _vglobal,
}
# Commands that do not.
_OTHER_COMMANDS: dict[str, Callable[[ExCommand, ExHost], None]] = {
    "yank": _yank,
    "write": _write,
    "quit": _quit,
    "wq": _write_quit,
    "xit": _write_quit,
}


def run_ex(line: str, host: ExHost) -> None:
    """Run one command line; raises ``ExError`` when it fails."""
    cmd = parse_ex(line)
    typed = line.strip(" \t:")
    unknown = ExError(f"Not an editor command: {typed}")
    if not cmd.name:
        if cmd.bang:
            raise ExError("Shell commands and filters (:!) are not supported")
        if cmd.args:
            raise unknown
        if cmd.range is not None:
            _goto(cmd, host)
        return
    if cmd.name in _OTHER_COMMANDS:
        _OTHER_COMMANDS[cmd.name](cmd, host)
        return
    handler = COMMANDS.get(cmd.name)
    if handler is None:
        raise unknown
    with host.undo_group():
        handler(cmd, host)
