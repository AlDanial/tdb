"""Shared assertions for trace tests (function tracing, Tasks 7, 8 and 11)."""

from __future__ import annotations

from collections import defaultdict


def assert_nested(trace: dict) -> None:
    """Every ``(pid, tid)`` lane's ``X`` events must nest properly.

    Sorted by ``(ts, -dur)``, each slice must either start at or after the
    end of the enclosing one, or end within it: no partial overlap, which
    Perfetto drops.  Times are compared in integer nanoseconds (``ts`` and
    ``dur`` are microseconds with three decimals), so float sums such as
    ``0.1 + 0.2`` never fake an overlap at a shared end.
    """
    lanes: dict = defaultdict(list)
    for ev in trace["traceEvents"]:
        if ev.get("ph") != "X":
            continue
        start = round(ev["ts"] * 1000)
        dur = round(ev["dur"] * 1000)
        assert dur >= 0, f"negative dur: {ev}"
        lanes[(ev["pid"], ev["tid"])].append((start, -dur, ev))
    for (pid, tid), items in lanes.items():
        items.sort(key=lambda item: item[:2])
        stack: list = []  # (end, event) of the enclosing slices
        for start, neg_dur, ev in items:
            end = start - neg_dur
            while stack and stack[-1][0] <= start:
                stack.pop()
            if stack and end > stack[-1][0]:
                outer_end, outer = stack[-1]
                raise AssertionError(
                    f"pid {pid} tid {tid}: {ev['name']!r} [{start}, {end}] ns "
                    f"partially overlaps {outer['name']!r} ending at {outer_end} ns"
                )
            stack.append((end, ev))
