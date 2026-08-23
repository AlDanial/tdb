"""Evidence-preserving wait-graph analysis for Rust concurrency snapshots."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
import re

from tdb.rust_concurrency.classifier import classify_snapshot
from tdb.rust_concurrency.models import (
    Confidence,
    ConcurrencySnapshot,
    Evidence,
    Finding,
    FindingKind,
    Primitive,
    ProbeResult,
    RawSnapshot,
    ThreadAnalysis,
    ThreadState,
    WaitEdge,
)


# Keep debugger-internal exclusions narrow and reviewable.  The key permits a
# future debugger release to extend this list without applying its names to
# every adapter or platform.
HOUSEKEEPING_THREAD_PATTERNS_V1: dict[tuple[str, str], tuple[re.Pattern[str], ...]] = {
    ("linux", "lldb-dap"): (
        re.compile(r"^lldb\.process\.internal-state-coordinator$"),
    ),
    ("darwin", "lldb-dap"): (
        re.compile(r"^lldb\.process\.internal-state-coordinator$"),
    ),
}

_CONFIDENCE_RANK = {
    Confidence.UNKNOWN: 0,
    Confidence.PROBABLE: 1,
    Confidence.CONFIRMED: 2,
}
MAX_WAIT_CYCLES = 256


def _cyclic_components(adjacency: dict[int, list[WaitEdge]]) -> tuple[frozenset[int], ...]:
    """Return deterministic strongly connected components that can contain cycles."""
    nodes = sorted(
        set(adjacency)
        | {
            edge.owner_thread_id
            for outgoing in adjacency.values()
            for edge in outgoing
            if edge.owner_thread_id is not None
        }
    )
    index = 0
    indexes: dict[int, int] = {}
    lowlinks: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components: list[frozenset[int]] = []

    def connect(node: int) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for edge in adjacency.get(node, ()):
            owner = edge.owner_thread_id
            assert owner is not None
            if owner not in indexes:
                connect(owner)
                lowlinks[node] = min(lowlinks[node], lowlinks[owner])
            elif owner in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[owner])
        if lowlinks[node] != indexes[node]:
            return
        component: set[int] = set()
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        has_self_loop = any(
            edge.owner_thread_id == node for edge in adjacency.get(node, ())
        )
        if len(component) > 1 or has_self_loop:
            components.append(frozenset(component))

    for node in nodes:
        if node not in indexes:
            connect(node)
    return tuple(sorted(components, key=lambda component: tuple(sorted(component))))


def _strongest_confidence(evidence: tuple[Evidence, ...]) -> Confidence:
    """Return the strongest direct observation without discarding evidence."""
    if not evidence:
        return Confidence.UNKNOWN
    return max((item.confidence for item in evidence), key=_CONFIDENCE_RANK.__getitem__)


def _is_confirmed(edge: WaitEdge) -> bool:
    return _strongest_confidence(edge.evidence) is Confidence.CONFIRMED


def _canonical_cycle(thread_ids: tuple[int, ...]) -> tuple[int, ...]:
    """Rotate a directed cycle so equivalent traversal starts compare equally."""
    start = min(range(len(thread_ids)), key=thread_ids.__getitem__)
    return thread_ids[start:] + thread_ids[:start]


def _find_cycles(
    edges: Iterable[WaitEdge],
) -> tuple[
    tuple[tuple[tuple[int, ...], tuple[WaitEdge, ...]], ...],
    bool,
]:
    """Find deterministic directed cycles using sorted depth-first traversal."""
    adjacency: dict[int, list[WaitEdge]] = {}
    for edge in edges:
        if edge.owner_thread_id is not None:
            adjacency.setdefault(edge.waiter_thread_id, []).append(edge)

    for outgoing in adjacency.values():
        outgoing.sort(
            key=lambda edge: (edge.owner_thread_id, edge.primitive_id, edge.operation)
        )

    component_by_node = {
        node: component
        for component in _cyclic_components(adjacency)
        for node in component
    }
    adjacency = {
        waiter: [
            edge
            for edge in outgoing
            if edge.owner_thread_id in component_by_node
            and component_by_node.get(waiter) == component_by_node[edge.owner_thread_id]
        ]
        for waiter, outgoing in adjacency.items()
        if waiter in component_by_node
    }

    active: list[int] = []
    active_edges: list[WaitEdge] = []
    active_index: dict[int, int] = {}
    found: dict[tuple[int, ...], tuple[WaitEdge, ...]] = {}
    truncated = False

    def visit(thread_id: int) -> bool:
        nonlocal truncated
        active_index[thread_id] = len(active)
        active.append(thread_id)
        for edge in adjacency.get(thread_id, ()):
            owner = edge.owner_thread_id
            assert owner is not None
            if owner in active_index:
                index = active_index[owner]
                cycle_ids = _canonical_cycle(tuple(active[index:]))
                cycle_edges = tuple(active_edges[index:] + [edge])
                found.setdefault(cycle_ids, cycle_edges)
                if len(found) > MAX_WAIT_CYCLES:
                    del found[cycle_ids]
                    truncated = True
                    active.pop()
                    del active_index[thread_id]
                    return False
            else:
                active_edges.append(edge)
                if not visit(owner):
                    active_edges.pop()
                    active.pop()
                    del active_index[thread_id]
                    return False
                active_edges.pop()
        active.pop()
        del active_index[thread_id]
        return True

    nodes = set(adjacency) | {
        edge.owner_thread_id
        for values in adjacency.values()
        for edge in values
        if edge.owner_thread_id is not None
    }
    for thread_id in sorted(nodes):
        if not visit(thread_id):
            break

    cycles = tuple((thread_ids, found[thread_ids]) for thread_ids in sorted(found))
    return cycles, truncated


def _deadlock_finding(thread_ids: tuple[int, ...]) -> Finding:
    joined = ", ".join(str(thread_id) for thread_id in thread_ids)
    return Finding(
        kind=FindingKind.CONFIRMED_DEADLOCK,
        thread_ids=thread_ids,
        summary=f"Confirmed deadlock involving threads {joined}.",
    )


def find_confirmed_cycles(edges: tuple[WaitEdge, ...]) -> tuple[Finding, ...]:
    """Return only cycles whose every traversed owner relationship is confirmed."""
    confirmed_edges = tuple(edge for edge in edges if _is_confirmed(edge))
    return tuple(
        _deadlock_finding(thread_ids)
        for thread_ids, _ in _find_cycles(confirmed_edges)[0]
    )


def _cycle_gaps(cycle_edges: tuple[WaitEdge, ...]) -> tuple[str, ...]:
    gaps = {
        (
            f"thread {edge.waiter_thread_id} -> {edge.primitive_id} has only "
            f"{_strongest_confidence(edge.evidence).value} evidence"
        )
        for edge in cycle_edges
        if not _is_confirmed(edge)
    }
    return tuple(sorted(gaps))


def _is_housekeeping(thread: ThreadAnalysis, *, platform: str, adapter: str) -> bool:
    return any(
        pattern.fullmatch(thread.name) is not None
        for pattern in HOUSEKEEPING_THREAD_PATTERNS_V1.get((platform, adapter), ())
    )


def _whole_program_stall(
    threads: tuple[ThreadAnalysis, ...], edges: tuple[WaitEdge, ...]
) -> Finding | None:
    """Flag an all-blocked application snapshot without inventing ownership."""
    blocked_by_id = {thread.thread_id: thread for thread in threads}
    if not blocked_by_id or any(thread.state is not ThreadState.BLOCKED for thread in threads):
        return None

    gaps: set[str] = set()
    for edge in edges:
        if edge.waiter_thread_id not in blocked_by_id:
            continue
        if edge.owner_thread_id is None:
            gaps.add(
                f"thread {edge.waiter_thread_id} -> {edge.primitive_id} has no observed owner"
            )
        elif not _is_confirmed(edge):
            gaps.add(
                f"thread {edge.waiter_thread_id} -> {edge.primitive_id} has only "
                f"{_strongest_confidence(edge.evidence).value} evidence"
            )
    return Finding(
        kind=FindingKind.WHOLE_PROGRAM_STALL,
        thread_ids=tuple(sorted(blocked_by_id)),
        summary="All observed application threads are blocked.",
        evidence_gaps=tuple(sorted(gaps)),
    )


def find_suspected_stalls(
    threads: tuple[ThreadAnalysis, ...],
    edges: tuple[WaitEdge, ...],
    confirmed: tuple[Finding, ...],
    *,
    platform: str | None = None,
    adapter: str | None = None,
) -> tuple[Finding, ...]:
    """Report evidence-incomplete cycles and all-blocked application snapshots."""
    confirmed_thread_sets = {finding.thread_ids for finding in confirmed}
    suspected: list[Finding] = []
    for thread_ids, cycle_edges in _find_cycles(edges)[0]:
        if thread_ids in confirmed_thread_sets:
            continue
        gaps = _cycle_gaps(cycle_edges)
        if not gaps:
            continue
        joined = ", ".join(str(thread_id) for thread_id in thread_ids)
        suspected.append(
            Finding(
                kind=FindingKind.SUSPECTED_CYCLE,
                thread_ids=thread_ids,
                summary=f"Suspected wait cycle involving threads {joined}.",
                evidence_gaps=gaps,
            )
        )

    app_threads = tuple(
        thread
        for thread in threads
        if not _is_housekeeping(
            thread,
            platform=platform or "",
            adapter=adapter or "",
        )
    )
    app_thread_ids = {thread.thread_id for thread in app_threads}
    confirmed_thread_ids = {
        thread_id for finding in confirmed for thread_id in finding.thread_ids
    }
    # A whole-program finding would duplicate a confirmed cycle when that
    # cycle already accounts for every application thread.  Keep it when a
    # separate blocked application thread remains outside the cycle.
    if not app_thread_ids.issubset(confirmed_thread_ids):
        whole_stall = _whole_program_stall(app_threads, edges)
        if whole_stall is not None:
            suspected.append(whole_stall)

    return tuple(
        sorted(
            suspected,
            key=lambda finding: (finding.kind.value, finding.thread_ids, finding.summary),
        )
    )


def _thread_ids_by_os_id(raw: RawSnapshot, probe: ProbeResult | None) -> dict[str, int]:
    """Join explicit DAP/probe thread identifiers without numeric guessing."""
    ids = {
        thread.os_thread_id: thread.thread_id
        for thread in raw.threads
        if thread.os_thread_id is not None
    }
    if probe is None:
        return ids

    candidates: dict[str, set[int]] = {}
    for thread in raw.threads:
        for hint in (thread.name, str(thread.thread_id), f"thread #{thread.thread_id}"):
            candidates.setdefault(hint, set()).add(thread.thread_id)
    by_hint = {
        hint: next(iter(thread_ids))
        for hint, thread_ids in candidates.items()
        if len(thread_ids) == 1
    }
    for probe_thread in probe.threads:
        thread_id = by_hint.get(probe_thread.dap_thread_hint)
        if thread_id is not None:
            ids[probe_thread.os_thread_id] = thread_id
    return ids


def _link_probe_owners(
    raw: RawSnapshot,
    threads: tuple[ThreadAnalysis, ...],
    primitives: tuple[Primitive, ...],
    edges: tuple[WaitEdge, ...],
    probe: ProbeResult | None,
) -> tuple[tuple[ThreadAnalysis, ...], tuple[Primitive, ...], tuple[WaitEdge, ...]]:
    """Merge explicit probe owner observations into immutable classifier output."""
    if probe is None or not probe.primitive_states:
        return threads, primitives, edges

    state_by_primitive = {
        state.primitive_id: state for state in probe.primitive_states
    }
    thread_ids_by_os_id = _thread_ids_by_os_id(raw, probe)
    linked: list[WaitEdge] = []
    display_waits: dict[int, WaitEdge] = {}
    for edge in edges:
        state = state_by_primitive.get(edge.primitive_id)
        if state is None:
            linked.append(edge)
            display_waits[edge.waiter_thread_id] = edge
            continue
        evidence = edge.evidence + state.evidence
        owners = tuple(
            sorted(
                {
                    thread_ids_by_os_id[owner_os_id]
                    for owner_os_id in state.owner_os_thread_ids
                    if owner_os_id in thread_ids_by_os_id
                }
            )
        )
        if not owners:
            linked_edge = replace(edge, evidence=evidence)
            linked.append(linked_edge)
            display_waits[edge.waiter_thread_id] = linked_edge
            continue
        owner_edges = tuple(
            replace(edge, owner_thread_id=owner, evidence=evidence) for owner in owners
        )
        linked.extend(owner_edges)
        display_waits[edge.waiter_thread_id] = owner_edges[0]

    enriched_primitives = tuple(
        replace(
            primitive,
            evidence=(
                primitive.evidence
                + state_by_primitive[primitive.primitive_id].evidence
                if primitive.primitive_id in state_by_primitive
                else primitive.evidence
            ),
        )
        for primitive in primitives
    )
    enriched_threads = tuple(
        replace(thread, wait=display_waits.get(thread.thread_id, thread.wait))
        for thread in threads
    )
    return enriched_threads, enriched_primitives, tuple(linked)


def analyze(raw: RawSnapshot, probe: ProbeResult | None = None) -> ConcurrencySnapshot:
    """Classify a stopped Rust snapshot and derive best-effort graph findings."""
    threads, primitives, edges = classify_snapshot(raw)
    threads, primitives, edges = _link_probe_owners(
        raw, threads, primitives, edges, probe
    )
    threads = tuple(sorted(threads, key=lambda thread: thread.thread_id))
    primitives = tuple(sorted(primitives, key=lambda primitive: primitive.primitive_id))
    edges = tuple(
        sorted(
            edges,
            key=lambda edge: (
                edge.waiter_thread_id,
                edge.primitive_id,
                edge.owner_thread_id is None,
                edge.owner_thread_id,
                edge.operation,
            ),
        )
    )
    confirmed = find_confirmed_cycles(edges)
    suspected = find_suspected_stalls(
        threads,
        edges,
        confirmed,
        platform=raw.platform,
        adapter=raw.adapter,
    )
    cycle_truncated = _find_cycles(edges)[1]
    warnings = raw.warnings + (probe.warnings if probe is not None else ())
    if cycle_truncated:
        warnings += (f"Wait-cycle analysis truncated after {MAX_WAIT_CYCLES} cycles.",)
    return ConcurrencySnapshot(
        rust_version=(probe.rust_version if probe and probe.rust_version else raw.rust_version),
        adapter=raw.adapter,
        platform=raw.platform,
        threads=threads,
        primitives=primitives,
        edges=edges,
        confirmed_deadlocks=confirmed,
        suspected_stalls=suspected,
        warnings=warnings,
    )
