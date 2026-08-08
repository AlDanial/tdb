"""DAP type definitions as dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Source:
    path: str | None = None
    name: str | None = None
    source_reference: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        return cls(
            path=data.get("path"),
            name=data.get("name"),
            source_reference=data.get("sourceReference", 0),
        )


# A setBreakpoints response Breakpoint carries this exact message when an
# adapter has deferred real verification to a later point instead of
# reporting a genuine bind failure (perl's compile-phase breakpoint
# deferral -- see adapters/perl/server.py's _on_setBreakpoints -- is the
# only current producer). Breakpoints carrying it are always followed by
# a `breakpoint` "changed" event once resolved; controller.py's
# _warn_unbound_breakpoints treats its presence as "the adapter already
# explained itself" and skips its own missing-debug-info guess-warning.
DEFERRED_VERIFICATION_MESSAGE = "not yet verified: still compiling"


@dataclass
class Breakpoint:
    id: int = 0
    verified: bool = False
    line: int = 0
    source: Source | None = None
    message: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Breakpoint:
        source = Source.from_dict(data["source"]) if "source" in data else None
        return cls(
            id=data.get("id", 0),
            verified=data.get("verified", False),
            line=data.get("line", 0),
            source=source,
            message=data.get("message"),
        )


@dataclass
class SourceBreakpoint:
    line: int
    condition: str | None = None
    hit_condition: str | None = None
    log_message: str | None = None
    enabled: bool = True  # tdb-local flag; not part of DAP wire format
    # tdb-local flag; not part of DAP wire format. False for -t/--to-line
    # breakpoints, which are excluded from breakpoints.json on save.
    persist: bool = True
    # tdb-local flag; not part of DAP wire format. Mirrors the adapter's
    # last-reported verified state for this line -- True until told
    # otherwise so most (synchronously-verifying) adapters never touch
    # it. Kept current by controller.py's setBreakpoints response
    # handling and its `breakpoint` "changed" event handler (the latter
    # is how an adapter that defers verification, e.g. perl's
    # compile-phase breakpoint deferral, corrects it after the fact).
    verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"line": self.line}
        if self.condition is not None:
            d["condition"] = self.condition
        if self.hit_condition is not None:
            d["hitCondition"] = self.hit_condition
        if self.log_message is not None:
            d["logMessage"] = self.log_message
        return d


@dataclass
class Thread:
    id: int
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Thread:
        return cls(id=data["id"], name=data["name"])


@dataclass
class StackFrame:
    id: int
    name: str
    source: Source | None = None
    line: int = 0
    column: int = 0
    module_id: Any = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StackFrame:
        source = Source.from_dict(data["source"]) if "source" in data else None
        return cls(
            id=data["id"],
            name=data["name"],
            source=source,
            line=data.get("line", 0),
            column=data.get("column", 0),
            module_id=data.get("moduleId"),
        )


@dataclass
class Scope:
    name: str
    variables_reference: int
    named_variables: int = 0
    indexed_variables: int = 0
    expensive: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scope:
        return cls(
            name=data["name"],
            variables_reference=data["variablesReference"],
            named_variables=data.get("namedVariables", 0),
            indexed_variables=data.get("indexedVariables", 0),
            expensive=data.get("expensive", False),
        )


@dataclass
class Variable:
    name: str
    value: str
    type: str = ""
    variables_reference: int = 0
    named_variables: int = 0
    indexed_variables: int = 0
    evaluate_name: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Variable:
        return cls(
            name=data["name"],
            value=data["value"],
            type=data.get("type", ""),
            variables_reference=data.get("variablesReference", 0),
            named_variables=data.get("namedVariables", 0),
            indexed_variables=data.get("indexedVariables", 0),
            evaluate_name=data.get("evaluateName"),
        )


@dataclass
class Capabilities:
    supports_configuration_done_request: bool = False
    supports_evaluate_for_hovers: bool = False
    supports_set_variable: bool = False
    supports_completions_request: bool = False
    supports_conditional_breakpoints: bool = False
    supports_hit_conditional_breakpoints: bool = False
    supports_log_points: bool = False
    supports_breakpoint_locations_request: bool = False
    exception_breakpoint_filters: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Capabilities:
        return cls(
            supports_configuration_done_request=data.get(
                "supportsConfigurationDoneRequest", False
            ),
            supports_evaluate_for_hovers=data.get("supportsEvaluateForHovers", False),
            supports_set_variable=data.get("supportsSetVariable", False),
            supports_completions_request=data.get("supportsCompletionsRequest", False),
            supports_conditional_breakpoints=data.get(
                "supportsConditionalBreakpoints", False
            ),
            supports_hit_conditional_breakpoints=data.get(
                "supportsHitConditionalBreakpoints", False
            ),
            supports_log_points=data.get("supportsLogPoints", False),
            supports_breakpoint_locations_request=data.get(
                "supportsBreakpointLocationsRequest", False
            ),
            exception_breakpoint_filters=data.get("exceptionBreakpointFilters", []),
        )


@dataclass
class CompletionItem:
    label: str
    text: str | None = None
    type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompletionItem:
        return cls(
            label=data["label"],
            text=data.get("text"),
            type=data.get("type"),
        )
