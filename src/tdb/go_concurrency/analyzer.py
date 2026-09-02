"""Bipartite wait graph (goroutine -> resource) and conservative findings.

Never goroutine->goroutine edges, never mutex owners. Confidence rules:
CONFIRMED only when the whole picture was seen (uncollected == 0) and
no non-runtime goroutine is still running; otherwise PROBABLE.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from tdb.go_concurrency.models import (
    Confidence,
    GoFinding,
    GoFindingKind,
    GoResource,
    GoroutineInfo,
    GoroutineSnapshot,
    GoroutineState,
    GoWaitEdge,
)

_LEAK_CLUSTER = 4  # same-channel waiter count that suggests a leak
_CONVOY_SIZE = 3  # same-semaphore waiter count worth flagging


def analyze(
    goroutines: Iterable[GoroutineInfo],
    uncollected: int,
    warnings: tuple[str, ...],
) -> GoroutineSnapshot:
    gs = tuple(goroutines)
    edges: list[GoWaitEdge] = []
    waiters: dict[str, list[GoroutineInfo]] = defaultdict(list)
    for g in gs:
        if g.resource_id is not None and g.operation is not None:
            edges.append(GoWaitEdge(g.thread_id, g.resource_id, g.operation))
            waiters[g.resource_id].append(g)

    resources = tuple(
        GoResource(
            rid,
            "channel" if rid.startswith("chan:") else "semaphore",
            f"{'chan' if rid.startswith('chan:') else 'sem'} {rid.split(':', 1)[1]}",
        )
        for rid in sorted(waiters)
    )

    someone_running = any(
        g.state is GoroutineState.RUNNING and not g.is_runtime for g in gs
    )
    full_picture = uncollected == 0 and not someone_running
    confidence = Confidence.CONFIRMED if full_picture else Confidence.PROBABLE

    findings: list[GoFinding] = []
    for resource in resources:
        group = waiters[resource.resource_id]
        ops = {g.operation for g in group}
        tids = tuple(sorted(g.thread_id for g in group))
        if resource.kind == "channel":
            if ops == {"recv"} or ops == {"send"}:
                side = "receiving" if ops == {"recv"} else "sending"
                other = "sender" if ops == {"recv"} else "receiver"
                findings.append(
                    GoFinding(
                        GoFindingKind.STUCK_CHANNEL,
                        tids,
                        f"{len(group)} goroutine(s) {side} on {resource.label} "
                        f"with no {other} observed",
                        confidence,
                    )
                )
                if len(group) >= _LEAK_CLUSTER:
                    findings.append(
                        GoFinding(
                            GoFindingKind.LIKELY_LEAK,
                            tids,
                            f"{len(group)} goroutines parked on {resource.label} "
                            f"— possible goroutine leak",
                            Confidence.PROBABLE,
                        )
                    )
        elif len(group) >= _CONVOY_SIZE:
            findings.append(
                GoFinding(
                    GoFindingKind.MUTEX_CONVOY,
                    tids,
                    f"{len(group)} goroutines queued on {resource.label}",
                    Confidence.PROBABLE,
                )
            )

    return GoroutineSnapshot(
        goroutines=gs,
        resources=resources,
        edges=tuple(edges),
        findings=tuple(findings),
        uncollected=uncollected,
        warnings=warnings,
    )
