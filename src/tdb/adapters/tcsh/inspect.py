"""Parse and retain live tcsh state for one stopped generation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum


class InspectionError(Exception):
    """Live shell state could not be inspected safely."""


class UnknownFrameError(InspectionError):
    """A frame does not belong to the current stop."""


class UnknownReferenceError(InspectionError):
    """A variables reference was never issued by this store."""


class StaleReferenceError(InspectionError):
    """A variables reference belonged to an earlier stop."""


@dataclass(frozen=True, slots=True)
class Variable:
    """One DAP-compatible leaf variable."""

    name: str
    value: str
    variables_reference: int = 0


@dataclass(frozen=True, slots=True)
class Scope:
    """One fixed live-shell scope."""

    name: str
    variables_reference: int
    expensive: bool = False


class ScopeKind(str, Enum):
    """The adapter-owned command supplying a scope's values."""

    SHELL = "set"
    ENVIRONMENT = "env"
    ALIASES = "alias"
    ARGUMENTS = "arguments"


_SCOPE_DEFINITIONS = (
    ("Shell Variables", ScopeKind.SHELL),
    ("Environment", ScopeKind.ENVIRONMENT),
    ("Aliases", ScopeKind.ALIASES),
    ("Arguments", ScopeKind.ARGUMENTS),
)


def parse_set(output: str) -> tuple[Variable, ...]:
    """Parse the tab-delimited output of tcsh's ``set`` builtin."""

    return _parse_records(output, "set", lambda line: line.partition("\t"))


def parse_env(output: str) -> tuple[Variable, ...]:
    """Parse environment records, retaining equals signs after the first."""

    return _parse_records(output, "env", lambda line: line.partition("="))


def parse_alias(output: str) -> tuple[Variable, ...]:
    """Parse tab-delimited aliases without splitting their command text."""

    return _parse_records(output, "alias", lambda line: line.partition("\t"))


_ADAPTER_INTERNAL_PREFIX = "__tcsh_dap"


def _parse_records(
    output: str,
    command: str,
    split: Callable[[str], tuple[str, str, str]],
) -> tuple[Variable, ...]:
    records: list[Variable] = []
    for line_number, line in enumerate(output.splitlines(), 1):
        name, separator, value = split(line)
        if not name or not separator:
            raise InspectionError(f"malformed {command} output at line {line_number}")
        if name.startswith(_ADAPTER_INTERNAL_PREFIX):
            continue
        records.append(Variable(name, value))
    return tuple(sorted(records, key=lambda variable: variable.name))


class VariableStore:
    """Issue and expire frame/scope handles for the current stopped generation."""

    def __init__(self) -> None:
        self._next_reference = 1
        self._known_references: set[int] = set()
        self._scopes_by_frame: dict[int, tuple[Scope, ...]] = {}
        self._kind_by_reference: dict[int, ScopeKind] = {}
        self._values: dict[ScopeKind, tuple[Variable, ...]] = {}
        self._active = False

    def begin_stop(self, frame_ids: list[int] | tuple[int, ...]) -> None:
        """Start a generation and allocate four distinct handles per frame."""

        self.invalidate()
        self._active = True
        for frame_id in frame_ids:
            scopes: list[Scope] = []
            for name, kind in _SCOPE_DEFINITIONS:
                reference = self._next_reference
                self._next_reference += 1
                self._known_references.add(reference)
                self._kind_by_reference[reference] = kind
                scopes.append(Scope(name, reference))
            self._scopes_by_frame[frame_id] = tuple(scopes)

    def scopes(self, frame_id: int) -> tuple[Scope, ...]:
        """Return fixed scopes for a frame in the current generation."""

        try:
            return self._scopes_by_frame[frame_id]
        except KeyError:
            raise UnknownFrameError(f"unknown frame ID: {frame_id}") from None

    def variables(self, reference: int) -> tuple[Variable, ...]:
        """Return cached values for a current reference."""

        kind = self.scope_kind(reference)
        try:
            return self._values[kind]
        except KeyError:
            raise InspectionError(
                f"variables reference {reference} has not been loaded"
            ) from None

    def is_loaded(self, kind: ScopeKind) -> bool:
        """Return whether a scope kind has cached values for this stop."""

        return kind in self._values

    def scope_kind(self, reference: int) -> ScopeKind:
        """Resolve a current handle to its scope kind."""

        try:
            return self._kind_by_reference[reference]
        except KeyError:
            if reference in self._known_references:
                raise StaleReferenceError(
                    f"stale variables reference: {reference}"
                ) from None
            raise UnknownReferenceError(
                f"unknown variables reference: {reference}"
            ) from None

    def cache_set(self, variables: tuple[Variable, ...]) -> None:
        """Cache shell variables and derive the MVP Arguments view from argv."""

        self._require_active()
        self._values[ScopeKind.SHELL] = variables
        self._values[ScopeKind.ARGUMENTS] = tuple(
            variable for variable in variables if variable.name == "argv"
        )

    def cache_environment(self, variables: tuple[Variable, ...]) -> None:
        """Cache parsed environment values for this generation."""

        self._require_active()
        self._values[ScopeKind.ENVIRONMENT] = variables

    def cache_aliases(self, variables: tuple[Variable, ...]) -> None:
        """Cache parsed aliases for this generation."""

        self._require_active()
        self._values[ScopeKind.ALIASES] = variables

    def invalidate(self) -> None:
        """Expire all active handles and cached state."""

        self._active = False
        self._scopes_by_frame.clear()
        self._kind_by_reference.clear()
        self._values.clear()

    def _require_active(self) -> None:
        if not self._active:
            raise InspectionError("inspection data requires an active stop")
