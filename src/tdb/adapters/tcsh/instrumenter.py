"""Deterministic tcsh source instrumentation and source-map generation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from string import ascii_letters, digits
from types import MappingProxyType

from tdb.adapters.tcsh.models import SourceSpan, UnitKind
from tdb.adapters.tcsh.scanner import scan_script

_SAFE_TCSH_WORD_CHARACTERS = frozenset(ascii_letters + digits + "_./:-")


@dataclass(frozen=True, slots=True)
class Probe:
    """A generated stop location tied to an original source span."""

    id: int
    span: SourceSpan
    generated_path: Path
    source_depth: int


@dataclass(frozen=True, slots=True)
class SourceMap:
    """Immutable indexes for generated probes."""

    probes: tuple[Probe, ...]

    def probe(self, probe_id: int) -> Probe:
        """Return the probe with *probe_id*."""

        for candidate in self.probes:
            if candidate.id == probe_id:
                return candidate
        raise KeyError(probe_id)

    def for_path(self, path: Path) -> tuple[Probe, ...]:
        """Return probes for a canonicalized original source path."""

        canonical = path.resolve(strict=False)
        return tuple(probe for probe in self.probes if probe.span.path == canonical)


@dataclass(frozen=True, slots=True)
class InstrumentationResult:
    """The generated root and its immutable mapping to original sources."""

    root: Path
    source_map: SourceMap
    generated_by_original: Mapping[Path, Path]


class Instrumenter:
    """Generate source-isolated tcsh scripts for a single debug session."""

    def __init__(
        self,
        workspace: Path,
        cwd: Path,
        original_argv0: str,
        probe_renderer: Callable[[int], str],
        source_event_renderer: Callable[[str, int], str],
    ) -> None:
        self.workspace = workspace
        self.cwd = cwd.resolve(strict=False)
        self.original_argv0 = original_argv0
        self.probe_renderer = probe_renderer
        self.source_event_renderer = source_event_renderer

    def instrument(self, program: Path) -> InstrumentationResult:
        """Recursively instrument *program* and return its generated root."""

        self._probes: list[Probe] = []
        self._generated_by_original: dict[Path, Path] = {}
        self._visiting: set[Path] = set()
        self._next_probe_id = 1
        self._sources_dir = self.workspace / "sources"
        self._probes_dir = self.workspace / "probes"
        self._sources_dir.mkdir(parents=True, exist_ok=True)
        self._probes_dir.mkdir(parents=True, exist_ok=True)

        root = self._instrument_path(program.resolve(strict=False), source_depth=0, is_root=True)
        return InstrumentationResult(
            root=root,
            source_map=SourceMap(tuple(self._probes)),
            generated_by_original=MappingProxyType(dict(self._generated_by_original)),
        )

    def _instrument_path(self, original: Path, source_depth: int, is_root: bool = False) -> Path:
        existing = self._generated_by_original.get(original)
        if existing is not None:
            return existing

        generated = self._sources_dir / self._generated_basename(original)
        self._generated_by_original[original] = generated
        self._visiting.add(original)
        text = original.read_text()
        output: list[str] = []
        units = scan_script(original, text)
        first_unit = True

        for unit in units:
            rewritten = _rewrite_dollar_zero(unit.text)
            if first_unit and is_root:
                first_unit = False
                if rewritten.startswith("#!"):
                    output.extend(_original_argv0_setup(rewritten, self.original_argv0))
                    continue
                output.append(_argv0_setup(self.original_argv0))
            first_unit = False

            if unit.kind is UnitKind.LABEL:
                output.append(_line_terminated(rewritten))
                output.append(self._new_probe_source(unit.span, source_depth))
            elif unit.kind is UnitKind.PROBEABLE:
                output.append(self._new_probe_source(unit.span, source_depth))
                output.append(rewritten)
            elif unit.kind is UnitKind.LITERAL_SOURCE:
                output.append(self._new_probe_source(unit.span, source_depth))
                target = self._literal_target(unit.source_target)
                if target is None:
                    output.append(rewritten)
                else:
                    generated_target = self._instrument_path(target, source_depth + 1)
                    output.append(
                        _line_terminated(self.source_event_renderer("enter", source_depth + 1))
                    )
                    output.append(_line_terminated(_replace_source_operand(rewritten, generated_target)))
                    output.append(
                        _line_terminated(self.source_event_renderer("leave", source_depth + 1))
                    )
            else:
                output.append(rewritten)

        if is_root and not units:
            output.extend(_original_argv0_setup("", self.original_argv0))
        generated.write_text("".join(output))
        self._visiting.discard(original)
        return generated

    def _new_probe_source(self, span: SourceSpan, source_depth: int) -> str:
        probe_id = self._next_probe_id
        self._next_probe_id += 1
        generated_path = self._probes_dir / f"{probe_id}.csh"
        generated_path.write_text(self.probe_renderer(probe_id))
        self._probes.append(Probe(probe_id, span, generated_path, source_depth))
        return f"source {_quote_tcsh_word(str(generated_path))}\n"

    def _literal_target(self, target: str | None) -> Path | None:
        if target is None:
            return None
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        canonical = candidate.resolve(strict=False)
        if not canonical.is_file() or canonical in self._visiting:
            return None if canonical not in self._generated_by_original else canonical
        return canonical

    @staticmethod
    def _generated_basename(original: Path) -> str:
        digest = sha256(str(original).encode()).hexdigest()
        return f"{digest}-{original.name}"


def _original_argv0_setup(first_unit: str, original_argv0: str) -> list[str]:
    setup = _argv0_setup(original_argv0)
    if first_unit.startswith("#!"):
        first_line, separator, remainder = first_unit.partition("\n")
        if not separator:
            return [first_line + "\n", setup]
        return [first_line + "\n", setup, remainder]
    return [setup, first_unit]


def _argv0_setup(original_argv0: str) -> str:
    return f"set __tcsh_dap_original_0 = {_quote_tcsh_word(original_argv0)}\n"


def _line_terminated(text: str) -> str:
    if not text or text.endswith(("\n", "\r")):
        return text
    return text + "\n"


def _quote_tcsh_word(value: str) -> str:
    """Encode one literal tcsh word without expansion or word splitting."""

    if "\x00" in value:
        raise ValueError("tcsh words cannot contain NUL")
    if "\n" in value or "\r" in value:
        raise ValueError("tcsh words cannot contain a newline")
    if not value:
        return "''"
    return "".join(
        character if character in _SAFE_TCSH_WORD_CHARACTERS else "\\" + character
        for character in value
    )


def _replace_source_operand(text: str, generated_path: Path) -> str:
    """Replace the single scanner-approved source operand, preserving suffix text."""

    index = 0
    while index < len(text) and text[index].isspace():
        index += 1
    index += len("source")
    while index < len(text) and text[index].isspace():
        index += 1
    start = index
    if index < len(text) and text[index] in {"'", '"'}:
        quote = text[index]
        index += 1
        while index < len(text) and text[index] != quote:
            index += 1
        index += 1
    else:
        while index < len(text) and not text[index].isspace():
            index += 1
    return text[:start] + _quote_tcsh_word(str(generated_path)) + text[index:]


def _rewrite_dollar_zero(text: str) -> str:
    """Rewrite lexical, executable `$0` tokens while preserving heredoc bodies."""

    command, body = _split_heredoc_body(text)
    output: list[str] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        character = command[index]
        if escaped:
            output.append(character)
            escaped = False
        elif quote != "'" and character == "\\":
            output.append(character)
            escaped = True
        elif character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            output.append(character)
        elif quote is None and character == "#":
            output.append(command[index:])
            break
        elif (
            quote != "'"
            and command[index : index + 2] == "$0"
            and (index + 2 == len(command) or not _is_name_character(command[index + 2]))
        ):
            output.append("${__tcsh_dap_original_0}")
            index += 1
        else:
            output.append(character)
        index += 1
    return "".join(output) + body


def _split_heredoc_body(text: str) -> tuple[str, str]:
    header_end = _logical_header_end(text)
    if header_end == len(text):
        return text, ""
    header = text[:header_end]
    delimiter = _find_heredoc_delimiter(header)
    if delimiter is None:
        return text, ""
    offset = header_end
    body_start = offset
    while offset < len(text):
        next_end = text.find("\n", offset)
        if next_end == -1:
            next_end = len(text)
            line = text[offset:]
        else:
            next_end += 1
            line = text[offset:next_end]
        if line.rstrip("\r\n") == delimiter:
            return text[:body_start], text[body_start:]
        offset = next_end
    return text, ""


def _logical_header_end(text: str) -> int:
    offset = 0
    while offset < len(text):
        newline = text.find("\n", offset)
        end = len(text) if newline == -1 else newline + 1
        if not _line_is_continued(text[offset:end]):
            return end
        offset = end
    return len(text)


def _line_is_continued(line: str) -> bool:
    quote: str | None = None
    escaped = False
    for character in line.rstrip("\r\n"):
        if escaped:
            escaped = False
        elif quote != "'" and character == "\\":
            escaped = True
        elif character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif quote is None and character == "#":
            return False
    return quote != "'" and escaped


def _find_heredoc_delimiter(header: str) -> str | None:
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(header):
        character = header[index]
        if escaped:
            escaped = False
        elif quote != "'" and character == "\\":
            escaped = True
        elif character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif quote is None and character == "#":
            return None
        elif quote is None and header[index : index + 2] == "<<":
            return _heredoc_operand(header[index + 2 :])
        index += 1
    return None


def _heredoc_operand(text: str) -> str | None:
    operand = text.lstrip()
    if not operand:
        return None
    if operand[0] in {"'", '"'}:
        quote = operand[0]
        end = operand.find(quote, 1)
        return None if end == -1 else operand[1:end]
    return operand.split(maxsplit=1)[0]


def _is_name_character(character: str) -> bool:
    return character.isalnum() or character == "_"
