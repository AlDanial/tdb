"""Debugger-neutral classification for Rust standard-library waits."""

from __future__ import annotations

from dataclasses import dataclass
import re

from tdb.rust_concurrency.models import (
    Confidence,
    Evidence,
    Primitive,
    PrimitiveKind,
    RawFrame,
    RawSnapshot,
    RawThread,
    ThreadAnalysis,
    ThreadState,
    WaitEdge,
)


@dataclass(frozen=True)
class _FrameRule:
    pattern: re.Pattern[str]
    operation: str
    kind: PrimitiveKind


# Rules remain exact after demangler spelling normalization.  High-level
# operations precede the implementation-level park fallback.
_FRAME_RULES = (
    _FrameRule(
        re.compile(r"^std::sync::mpsc::SyncSender::send$"),
        "mpsc-send",
        PrimitiveKind.CHANNEL,
    ),
    _FrameRule(
        re.compile(r"^std::sync::mpsc::Receiver::recv$"),
        "mpsc-recv",
        PrimitiveKind.CHANNEL,
    ),
    _FrameRule(
        re.compile(r"^std::sync::poison::rwlock::RwLock::write$"),
        "rwlock-write",
        PrimitiveKind.RWLOCK,
    ),
    _FrameRule(
        re.compile(r"^std::sync::poison::rwlock::RwLock::read$"),
        "rwlock-read",
        PrimitiveKind.RWLOCK,
    ),
    _FrameRule(
        re.compile(r"^std::sync::poison::mutex::Mutex::lock$"),
        "mutex-lock",
        PrimitiveKind.MUTEX,
    ),
    _FrameRule(
        re.compile(r"^std::sync::poison::condvar::Condvar::wait$"),
        "condvar-wait",
        PrimitiveKind.CONDVAR,
    ),
    _FrameRule(
        re.compile(
            r"^std::thread::(?:join_handle::)?JoinHandle::join$"
        ),
        "join",
        PrimitiveKind.THREAD,
    ),
    _FrameRule(
        re.compile(r"^std::thread::(?:functions::)?park$"),
        "park",
        PrimitiveKind.PARKER,
    ),
)

_HEX_TOKEN = re.compile(r"(?<![0-9A-Za-z_])0x[0-9a-fA-F]+(?![0-9A-Za-z_])")
_LEGACY_HASH_SUFFIX = re.compile(r"::h[0-9a-fA-F]+$")
_PLATFORM_WAIT_FRAMES = (
    re.compile(r"^futex_wait(?:v)?$"),
    re.compile(r"^__futex(?:_abstimed_wait_common(?:64)?|_wait)$"),
    re.compile(r"^pthread_(?:cond|mutex)_wait$"),
    re.compile(r"^__pthread_cond_wait_common$"),
    re.compile(r"^__ulock_wait$"),
    re.compile(r"^__psynch_cvwait$"),
)


@dataclass(frozen=True)
class _Wait:
    frame_name: str
    operation: str
    kind: PrimitiveKind
    address: str | None
    evidence: tuple[Evidence, ...]


def _strip_generics(name: str) -> str:
    """Remove legacy or v0 demangler generic components from a frame name."""
    result: list[str] = []
    depth = 0
    for character in name.replace("$LT$", "<").replace("$GT$", ">"):
        if character == "<":
            if depth == 0 and result[-2:] == [":", ":"]:
                del result[-2:]
            depth += 1
        elif character == ">" and depth:
            depth -= 1
        elif not depth:
            result.append(character)
    return "".join(result)


def _normalize_frame_name(name: str) -> str:
    return _LEGACY_HASH_SUFFIX.sub("", _strip_generics(name))


def _address_from(frame: RawFrame) -> tuple[str | None, str | None]:
    """Extract only a complete hexadecimal address from a typed variable."""
    for variable in frame.variables:
        if not variable.type_name.strip():
            continue
        match = _HEX_TOKEN.search(variable.value)
        if match is not None:
            return match.group(0), variable.name
    return None, None


def _is_platform_wait_frame(frame: RawFrame) -> bool:
    """Recognize only known native wait symbols, never application lookalikes."""
    name = _normalize_frame_name(frame.name)
    return any(pattern.fullmatch(name) is not None for pattern in _PLATFORM_WAIT_FRAMES)


def _classify_wait(thread: RawThread) -> _Wait | None:
    platform_wait = next(
        (
            frame
            for frame in thread.frames
            if _is_platform_wait_frame(frame)
        ),
        None,
    )
    park_match: tuple[RawFrame, _FrameRule] | None = None
    for frame in thread.frames:
        normalized = _normalize_frame_name(frame.name)
        for rule in _FRAME_RULES:
            if rule.pattern.fullmatch(normalized) is None:
                continue
            if rule.kind is PrimitiveKind.PARKER:
                park_match = park_match or (frame, rule)
                break
            return _wait_from_match(frame, rule, platform_wait)
    if park_match is None:
        return None
    return _wait_from_match(*park_match, platform_wait)


def _wait_from_match(
    frame: RawFrame, rule: _FrameRule, platform_wait: RawFrame | None
) -> _Wait:
    address, variable_name = _address_from(frame)
    if address is not None:
        evidence = (
            Evidence(
                Confidence.PROBABLE,
                "frame-variable",
                f"{variable_name}={address}",
            ),
        )
    else:
        observations = [Evidence(Confidence.UNKNOWN, "stack", frame.name)]
        if platform_wait is not None:
            observations.append(
                Evidence(
                    Confidence.PROBABLE,
                    "platform-wait",
                    platform_wait.name,
                )
            )
        evidence = tuple(observations)
    return _Wait(frame.name, rule.operation, rule.kind, address, evidence)


def _primitive_id(kind: PrimitiveKind, address: str | None, thread_id: int) -> str:
    if address is not None:
        return f"{kind.value}:{address}"
    return f"unknown:{kind.value}:{thread_id}"


def _label(kind: PrimitiveKind, address: str | None) -> str:
    names = {
        PrimitiveKind.THREAD: "Thread",
        PrimitiveKind.MUTEX: "Mutex",
        PrimitiveKind.RWLOCK: "RwLock",
        PrimitiveKind.CONDVAR: "Condvar",
        PrimitiveKind.CHANNEL: "MPSC channel",
        PrimitiveKind.PARKER: "Parker",
    }
    name = names[kind]
    return f"{name} at {address}" if address is not None else f"{name} (address unavailable)"


def _analyze_thread(thread: RawThread) -> tuple[ThreadAnalysis, _Wait | None]:
    wait = _classify_wait(thread)
    if wait is None:
        return (
            ThreadAnalysis(thread.thread_id, thread.name, ThreadState.UNKNOWN, None),
            None,
        )

    edge = WaitEdge(
        waiter_thread_id=thread.thread_id,
        primitive_id=_primitive_id(wait.kind, wait.address, thread.thread_id),
        owner_thread_id=None,
        operation=wait.operation,
        evidence=wait.evidence,
    )
    return (
        ThreadAnalysis(thread.thread_id, thread.name, ThreadState.BLOCKED, edge),
        wait,
    )


def classify_thread(thread: RawThread) -> ThreadAnalysis:
    """Classify a single thread without debugger-specific assumptions."""
    return _analyze_thread(thread)[0]


def classify_snapshot(
    raw: RawSnapshot,
) -> tuple[tuple[ThreadAnalysis, ...], tuple[Primitive, ...], tuple[WaitEdge, ...]]:
    """Classify all threads and deduplicate their observed primitives."""
    classified = tuple(_analyze_thread(thread) for thread in raw.threads)
    primitives: dict[str, Primitive] = {}
    edges: list[WaitEdge] = []

    for analysis, wait in classified:
        if analysis.wait is None or wait is None:
            continue
        primitive_id = analysis.wait.primitive_id
        primitives.setdefault(
            primitive_id,
            Primitive(
                primitive_id=primitive_id,
                kind=wait.kind,
                address=wait.address,
                label=_label(wait.kind, wait.address),
                evidence=wait.evidence,
            ),
        )
        edges.append(analysis.wait)

    return (
        tuple(analysis for analysis, _ in classified),
        tuple(primitives.values()),
        tuple(edges),
    )
