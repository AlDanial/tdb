"""``:s`` substitute: parsing ``/pat/rep/flags`` and replacing (pure).

Patterns are Python regular expressions (see ``tdb.vi.search``), applied
to each line on its own, so ``^`` and ``$`` match at the line's ends and
a match never spans lines.  The replacement is a ``re.sub`` template:
``\\1`` and ``\\g<name>`` insert groups, ``\\\\`` is one backslash and
``&`` is literal (not vim's whole match).  ``\\n`` (and, as in vim,
``\\r``) typed in the replacement splits the line.

Flags: ``g`` all matches in a line (``gg`` turns it off again), ``i`` /
``I`` ignore / match case, ``c`` confirm each match, ``e`` no error when
nothing matches, and a leading ``&`` keeps the previous flags.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field

from tdb.vi.search import compile_pattern

_FLAGS = frozenset("gciIe")


class SubstituteError(ValueError):
    """A bad ``:s`` command; the message is shown as an error."""


@dataclass
class SubstituteSpec:
    """One ``:s``: the pattern, the replacement template and the flags."""

    pattern: str
    replacement: str
    flags: str


def parse_substitute(
    args: str,
    *,
    last: SubstituteSpec | None = None,
    last_search: str | None = None,
) -> SubstituteSpec:
    """Parse the argument of ``:s`` (``/pat/rep/flags``).

    The delimiter is any character but a letter, digit, space, ``\\``,
    ``"`` or ``|``; a missing trailing delimiter is fine.  An empty
    pattern is *last_search*.  Without a pattern (``""``, or flags only:
    ``g``, ``&``) it is *last* again, with only the flags given (``&``
    first keeps *last*'s).
    """
    if not args.strip() or args[0].isalnum() or args[0] == "&":
        if last is None:
            raise SubstituteError("No previous regular expression")
        flags = args.strip()
        if flags.startswith("&"):
            flags = last.flags + flags[1:]
        return SubstituteSpec(last.pattern, last.replacement, _check_flags(flags))
    delim = args[0]
    if delim in ' \t\\"|':
        raise SubstituteError("Regular expressions can't be delimited by letters")
    pattern, i = _field(args, 1, delim, keep_escape=True)
    replacement, i = _field(args, i, delim, keep_escape=False)
    if not pattern:
        if not last_search:
            raise SubstituteError("No previous regular expression")
        pattern = last_search
    return SubstituteSpec(pattern, replacement, _check_flags(args[i:].strip()))


def _field(args: str, i: int, delim: str, *, keep_escape: bool) -> tuple[str, int]:
    """The text from *i* to the next unescaped *delim*, and the index after it.

    ``\\<delim>`` is the delimiter itself: kept escaped in a pattern (where
    ``re`` takes it literally), plain in a replacement.
    """
    out: list[str] = []
    while i < len(args):
        ch = args[i]
        if ch == delim:
            return "".join(out), i + 1
        if ch == "\\" and i + 1 < len(args):
            nxt = args[i + 1]
            out.append(ch + nxt if keep_escape or nxt != delim else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out), i


def _check_flags(flags: str) -> str:
    bad = flags.lstrip("&")
    for j, ch in enumerate(bad):
        if ch not in _FLAGS:
            raise SubstituteError(f"Trailing characters: {bad[j:]}")
    return flags


def _has(spec: SubstituteSpec, flag: str) -> bool:
    """Whether *flag* is on (``g`` and ``c`` toggle, as in vim)."""
    n = spec.flags.count(flag)
    return n % 2 == 1 if flag in "gc" else n > 0


def global_flag(spec: SubstituteSpec) -> bool:
    return _has(spec, "g")


def confirm_flag(spec: SubstituteSpec) -> bool:
    return _has(spec, "c")


def quiet_flag(spec: SubstituteSpec) -> bool:
    """``e``: no error when the pattern is not found."""
    return _has(spec, "e")


def compile_spec(spec: SubstituteSpec) -> re.Pattern:
    """The compiled pattern (``i``: ignore case unless a later ``I``)."""
    rx = compile_pattern(spec.pattern)  # raises ViPatternError
    flags = spec.flags
    if "i" in flags and flags.rfind("i") > flags.rfind("I"):
        rx = re.compile(spec.pattern, re.MULTILINE | re.IGNORECASE)
    return rx


def _template(replacement: str) -> str:
    """The ``re`` template for *replacement* (``\\r`` splits like ``\\n``)."""
    out: list[str] = []
    i = 0
    while i < len(replacement):
        ch = replacement[i]
        if ch == "\\" and i + 1 < len(replacement):
            pair = replacement[i : i + 2]
            out.append("\\n" if pair == "\\r" else pair)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _prepare(spec: SubstituteSpec) -> tuple[re.Pattern, str]:
    rx = compile_spec(spec)
    template = _template(spec.replacement)
    try:
        rx.sub(template, "")  # checks the template (group numbers...)
    except re.error as exc:
        raise SubstituteError(f"Invalid replacement: {exc}") from None
    return rx, template


def _line_matches(rx: re.Pattern, line: str, every: bool) -> list[re.Match]:
    if every:
        return list(rx.finditer(line))
    m = rx.search(line)
    return [] if m is None else [m]


def iter_matches(
    lines: list[str], first: int, last: int, spec: SubstituteSpec
) -> Iterator[tuple[int, int, int]]:
    """``(row, start, end)`` of each match the command replaces, in order."""
    rx, _ = _prepare(spec)
    every = global_flag(spec)
    for row in range(first, last + 1):
        for m in _line_matches(rx, lines[row], every):
            yield row, m.start(), m.end()


@dataclass
class Rebuilt:
    """The text after replacing some of a command's matches."""

    lines: list[str]
    # New row of each old row (its first row when it was split).
    row_map: list[int]
    substitutions: int
    lines_changed: int
    # New row of the last part of the last changed line (-1: no change).
    last_row: int
    # (old row, match start) -> where that match now begins.
    positions: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)

    def end_row(self, row: int) -> int:
        """The new row of the last part of old line *row*."""
        if row + 1 < len(self.row_map):
            return self.row_map[row + 1] - 1
        return len(self.lines) - 1


def rebuild(
    lines: list[str],
    first: int,
    last: int,
    spec: SubstituteSpec,
    accepted: Collection[tuple[int, int, int]] | None = None,
) -> Rebuilt:
    """*lines* with the matches in *accepted* (default: all) replaced.

    Raises ``ViPatternError`` / ``SubstituteError`` before changing
    anything; *lines* itself is never modified.
    """
    rx, template = _prepare(spec)
    every = global_flag(spec)
    out: list[str] = list(lines[:first])
    row_map = list(range(first))
    positions: dict[tuple[int, int], tuple[int, int]] = {}
    subs = changed = 0
    last_row = -1
    for row in range(first, len(lines)):
        line = lines[row]
        row_map.append(len(out))
        matches = _line_matches(rx, line, every) if row <= last else []
        if not matches:
            out.append(line)
            continue
        parts: list[str] = []
        at = 0
        hits = 0
        for m in matches:
            parts.append(line[at : m.start()])
            built = "".join(parts)
            positions[(row, m.start())] = (
                len(out) + built.count("\n"),
                len(built) - built.rfind("\n") - 1,
            )
            if accepted is None or (row, m.start(), m.end()) in accepted:
                parts.append(m.expand(template))
                hits += 1
            else:
                parts.append(m.group())
            at = m.end()
        parts.append(line[at:])
        out.extend("".join(parts).split("\n"))
        if hits:
            subs += hits
            changed += 1
            last_row = len(out) - 1
    return Rebuilt(out, row_map, subs, changed, last_row, positions)


def apply_substitute(
    lines: list[str], first: int, last: int, spec: SubstituteSpec
) -> tuple[list[str], int, int, int]:
    """Replace in rows *first*..*last*: ``(new_lines, n_substitutions,
    n_lines_changed, last_changed_row)`` (the last row of that line's
    new text; -1 when nothing matched)."""
    done = rebuild(lines, first, last, spec)
    return done.lines, done.substitutions, done.lines_changed, done.last_row


def count_message(substitutions: int, lines: int) -> str:
    """vim's ``3 substitutions on 2 lines``."""
    s = "" if substitutions == 1 else "s"
    l_ = "" if lines == 1 else "s"
    return f"{substitutions} substitution{s} on {lines} line{l_}"
