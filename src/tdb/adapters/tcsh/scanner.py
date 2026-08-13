"""A deliberately conservative lexical scanner for C-shell source."""

from __future__ import annotations

import re
from pathlib import Path

from tdb.adapters.tcsh.models import LogicalUnit, SourceSpan, UnitKind

_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*:")
_CASE = re.compile(r"case\s+.+:", re.DOTALL)
_IF_HEADER = re.compile(r"if\s*\(.+\)\s+then", re.DOTALL)
_WHILE_HEADER = re.compile(r"while\s*\(.+\)", re.DOTALL)
_FOREACH_HEADER = re.compile(r"foreach\s+[A-Za-z_][A-Za-z0-9_]*\s*\(.+\)", re.DOTALL)
_SWITCH_HEADER = re.compile(r"switch\s*\(.+\)", re.DOTALL)
_SOURCE = re.compile(r"source\s+(.+)", re.DOTALL)


def scan_script(path: Path, text: str) -> tuple[LogicalUnit, ...]:
    """Split *text* into contiguous, source-preserving logical units.

    The scanner recognizes only syntax which remains safe to instrument.  All
    other uncertain cases are retained as opaque units rather than guessed at.
    """

    lines = text.splitlines(keepends=True)
    units: list[LogicalUnit] = []
    index = 0

    while index < len(lines):
        start_index = index
        parts = [lines[index]]
        index += 1

        while _is_continued(parts[-1]) and index < len(lines):
            parts.append(lines[index])
            index += 1

        command_text = "".join(parts)
        heredoc_delimiter, unsafe_heredoc = _heredoc_delimiter(command_text)
        heredoc_complete = True
        if unsafe_heredoc:
            parts.extend(lines[index:])
            index = len(lines)
            heredoc_complete = False
        elif heredoc_delimiter is not None:
            heredoc_complete = False
            while index < len(lines):
                line = lines[index]
                parts.append(line)
                index += 1
                if line.rstrip("\r\n") == heredoc_delimiter:
                    heredoc_complete = True
                    break

        unit_text = "".join(parts)
        kind, source_target = _classify(command_text, heredoc_complete)
        units.append(
            LogicalUnit(
                SourceSpan(path, start_index + 1, index),
                unit_text,
                kind,
                source_target,
            )
        )

    return tuple(units)


def _classify(command_text: str, heredoc_complete: bool) -> tuple[UnitKind, str | None]:
    code, balanced, has_separator = _code_without_comment(command_text)
    stripped = code.strip()
    if not heredoc_complete or not balanced or not stripped or has_separator:
        return UnitKind.OPAQUE, None

    if stripped in {"else", "endif", "end", "endsw", "breaksw", "default:"}:
        return UnitKind.STRUCTURAL, None
    if _CASE.fullmatch(stripped) or any(
        pattern.fullmatch(stripped)
        for pattern in (_IF_HEADER, _WHILE_HEADER, _FOREACH_HEADER, _SWITCH_HEADER)
    ):
        return UnitKind.STRUCTURAL, None
    if _LABEL.fullmatch(stripped):
        return UnitKind.LABEL, None

    source_target = _literal_source_target(stripped)
    if source_target is not None:
        return UnitKind.LITERAL_SOURCE, source_target
    if stripped.startswith("source"):
        return UnitKind.OPAQUE, None
    if stripped.startswith(("if", "while", "foreach", "switch")):
        return UnitKind.OPAQUE, None
    return UnitKind.PROBEABLE, None


def _code_without_comment(text: str) -> tuple[str, bool, bool]:
    """Return code before a shell comment and lexical safety markers."""

    code: list[str] = []
    quote: str | None = None
    escaped = False
    separators = False
    for character in text:
        if escaped:
            code.append(character)
            escaped = False
            continue
        if quote != "'" and character == "\\":
            code.append(character)
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            code.append(character)
            continue
        if quote is None and character == "#":
            break
        if quote is None and character == ";":
            separators = True
        code.append(character)
    return "".join(code), quote is None, separators


def _is_continued(line: str) -> bool:
    """Whether a physical line ends with an unescaped backslash outside quotes."""

    body = line.rstrip("\r\n")
    quote: str | None = None
    escaped = False
    for character in body:
        if escaped:
            escaped = False
            continue
        if quote != "'" and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        if quote is None and character == "#":
            break
    return quote != "'" and escaped


def _heredoc_delimiter(command_text: str) -> tuple[str | None, bool]:
    """Return a literal delimiter or flag an unsafe dynamic heredoc opener."""

    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command_text):
        character = command_text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if quote != "'" and character == "\\":
            escaped = True
            index += 1
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            index += 1
            continue
        if quote is None and character == "#":
            return None, False
        if quote is None and command_text[index : index + 2] == "<<":
            return _parse_heredoc_operand(command_text[index + 2 :])
        index += 1
    return None, False


def _parse_heredoc_operand(text: str) -> tuple[str | None, bool]:
    operand = text.lstrip()
    if not operand:
        return None, True
    quoted = operand[0] in {"'", '"'}
    if quoted:
        quote = operand[0]
        end = 1
        while end < len(operand):
            if operand[end] == quote:
                return operand[1:end], False
            end += 1
        return None, True

    delimiter = operand.split(maxsplit=1)[0]
    if any(character in delimiter for character in "$`*?[]\\"):
        return None, True
    return delimiter, False


def _literal_source_target(code: str) -> str | None:
    match = _SOURCE.fullmatch(code)
    if match is None:
        return None
    operand = match.group(1).strip()
    if not operand:
        return None

    quoted = operand[0] in {"'", '"'}
    if quoted:
        if len(operand) < 2 or operand[-1] != operand[0]:
            return None
        target = operand[1:-1]
        if operand[0] in target:
            return None
    else:
        if any(character.isspace() for character in operand):
            return None
        target = operand

    if not target or any(character in target for character in "$`*?[]\\;|&<>"):
        return None
    if not quoted and any(character in target for character in "~{}="):
        return None
    return target
