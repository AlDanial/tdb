"""Strict parsing and version gating for debugger probe envelopes."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from tdb.rust_concurrency.models import (
    Confidence,
    Evidence,
    ProbePrimitiveState,
    ProbeResult,
    ProbeThread,
)


MARKER = "TDB_RUST_JSON:"
_PRIMITIVE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:0x[0-9a-fA-F]+$")


class _InvalidEnvelope(ValueError):
    pass


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _InvalidEnvelope(f"{field} must be an object")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(_is_string(item) for item in value):
        raise _InvalidEnvelope(f"{field} must be an array of strings")
    return tuple(value)


def _parse_evidence(value: object) -> tuple[Evidence, ...]:
    if not isinstance(value, list):
        raise _InvalidEnvelope("evidence must be an array")
    parsed: list[Evidence] = []
    for item in value:
        evidence = _mapping(item, "evidence entry")
        confidence = evidence.get("confidence")
        source = evidence.get("source")
        detail = evidence.get("detail")
        if not all(_is_string(part) for part in (confidence, source, detail)):
            raise _InvalidEnvelope("evidence entries require string fields")
        try:
            parsed.append(Evidence(Confidence(confidence), source, detail))
        except ValueError as exc:
            raise _InvalidEnvelope("evidence confidence is invalid") from exc
    return tuple(parsed)


def _parse_threads(value: object) -> tuple[ProbeThread, ...]:
    if not isinstance(value, list):
        raise _InvalidEnvelope("threads must be an array")
    parsed: list[ProbeThread] = []
    for item in value:
        thread = _mapping(item, "thread entry")
        hint = thread.get("dap_thread_hint")
        os_thread_id = thread.get("os_thread_id")
        if not _is_string(hint) or not _is_string(os_thread_id):
            raise _InvalidEnvelope("thread entries require string identifiers")
        parsed.append(ProbeThread(hint, os_thread_id))
    return tuple(parsed)


def _parse_primitive_states(value: object) -> tuple[ProbePrimitiveState, ...]:
    if not isinstance(value, list):
        raise _InvalidEnvelope("primitive_states must be an array")
    parsed: list[ProbePrimitiveState] = []
    primitive_ids: set[str] = set()
    for item in value:
        state = _mapping(item, "primitive state")
        primitive_id = state.get("primitive_id")
        raw_state = state.get("raw_state")
        if not _is_string(primitive_id) or not _PRIMITIVE_ID.fullmatch(primitive_id):
            raise _InvalidEnvelope("primitive_id must contain a full hexadecimal address")
        if primitive_id in primitive_ids:
            raise _InvalidEnvelope(f"duplicate primitive_id {primitive_id}")
        primitive_ids.add(primitive_id)
        if not _is_string(raw_state):
            raise _InvalidEnvelope("raw_state must be a string")
        parsed.append(
            ProbePrimitiveState(
                primitive_id=primitive_id,
                owner_os_thread_ids=_string_list(
                    state.get("owner_os_thread_ids"), "owner_os_thread_ids"
                ),
                raw_state=raw_state,
                evidence=_parse_evidence(state.get("evidence")),
            )
        )
    return tuple(parsed)


def _marker_payload(output: str) -> str:
    matches = [line[len(MARKER) :] for line in output.splitlines() if line.startswith(MARKER)]
    if len(matches) != 1:
        raise _InvalidEnvelope("expected exactly one TDB_RUST_JSON marker")
    return matches[0]


def parse_probe_output(output: str) -> ProbeResult:
    """Parse one strict marker envelope, degrading malformed evidence safely."""
    try:
        payload = json.loads(_marker_payload(output))
        envelope = _mapping(payload, "probe envelope")
        version = envelope.get("rust_version")
        if version is not None and not _is_string(version):
            raise _InvalidEnvelope("rust_version must be a string or null")
        warnings = _string_list(envelope.get("warnings", []), "warnings")
        return ProbeResult(
            rust_version=version,
            threads=_parse_threads(envelope.get("threads", [])),
            primitive_states=_parse_primitive_states(
                envelope.get("primitive_states", [])
            ),
            warnings=warnings,
        )
    except (TypeError, json.JSONDecodeError, _InvalidEnvelope) as exc:
        return ProbeResult(
            rust_version=None,
            warnings=(f"invalid Rust probe envelope: {exc}",),
        )


def gate_supported_layout(result: ProbeResult, *, supported: str) -> ProbeResult:
    """Keep layout-dependent evidence only for the supported stable compiler."""
    if result.rust_version == supported:
        return result
    version = result.rust_version or "unknown"
    return replace(
        result,
        primitive_states=(),
        warnings=(f"unsupported Rust {version}; layout evidence disabled",)
        + result.warnings,
    )
