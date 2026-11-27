"""Render trace captures as Chrome/Perfetto JSON trace events.

``build_trace`` turns one or more :class:`~tdb.trace.capture.Capture` objects
(one per service) into the Chrome JSON trace-event format, which
ui.perfetto.dev opens directly; ``write_trace`` streams the same trace to a
file.  All captures share one time base, the earliest event, so merged
services line up.

Drawing (a lane is one OS thread outside any asyncio task, or one task):

- an ordinary function is one ``X`` slice from call to return/raise;
- a coroutine whose call ran inside an asyncio task is one slice for its
  whole lifetime on that task's lane, with ``(suspended)`` children for its
  waits (only the innermost traced frame's wait is kept);
- generators, async generators and coroutines outside any task are one slice
  per run (start or resume to the next suspend or end), each on the lane
  where that run happened.  An async generator's run goes on across an
  await that suspends a traced coroutine it is awaiting: the agent records
  awaits and yields alike as ``suspend``, and ending the run there would cut
  through that coroutine's slice;
- a call that never ended is ``args["unfinished"] = True``: closed where its
  process ends (where its part ends, for a call open at an exec), or, for
  one suspended for good, its last run as it was.

Every event carries its own lane, and one call's events can span lanes, so
events pair by ``(pid, call_id)``.  Perfetto drops slices that do not nest,
so each lane is repaired after drawing, deterministically: a
``(suspended)`` slice that runs past the end of a slice enclosing its start
is trimmed to start where that slice ends (and dropped if that is past
the end of its own call); a slice that starts inside a ``(suspended)`` one
and outlasts it ends that suspension where it starts; any other slice that
runs past the end of its enclosing slice is cut to end there.  Repaired
slices carry ``args["clipped"] = True``.

This module runs inside tdb, not the traced program; it must work on
Python 3.11.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from heapq import heappop, heappush
from operator import itemgetter
from pathlib import Path

from tdb.trace.capture import Capture

_PER_RUN_KINDS = ("generator", "async_generator")
_SORT_STRIDE = 1000  # process_sort_index = service index * stride + order

# How a call is drawn.
_FN, _LIFETIME, _PER_RUN = range(3)

# A slice is a list [start_ns, end_ns, rank, seq, call, run, flags]: rank
# orders a call before a suspension with the same interval, seq (creation
# order) breaks the remaining ties, run is the 1-based run number (0 when
# not drawn per run).
_START, _END, _RANK, _SEQ, _CALL, _RUN, _FLAGS = range(7)
_CALL_RANK, _SUSP_RANK = 0, 1
_UNFINISHED, _CLIPPED = 1, 2

_ts_of = itemgetter("ts")


class _Proc:
    __slots__ = (
        "anchor",
        "end",
        "instants",
        "lane_map",
        "lanes",
        "last_ts",
        "name",
        "pid",
        "service",
        "si",
        "sort",
        "start",
        "syn",
    )

    def __init__(self, si: int, service: str, pid) -> None:
        self.si, self.service, self.pid = si, service, pid
        self.name = None
        self.start = self.end = self.last_ts = None
        self.lanes: list[_Lane] = []  # in order of their first event
        self.lane_map: dict = {}  # (tid, task_id) -> _Lane
        self.instants: list[dict] = []  # crash and note events
        self.syn = self.sort = self.anchor = 0


class _Lane:
    __slots__ = (
        "items",
        "open_lifetime",
        "proc",
        "raises",
        "susp",
        "syn",
        "task",
        "task_id",
        "thread",
        "tid",
    )

    def __init__(self, proc: _Proc, tid, task_id, syn: int) -> None:
        self.proc, self.tid, self.task_id, self.syn = proc, tid, task_id, syn
        self.thread = self.task = None
        self.items: list[list] = []  # call and run slices
        self.susp: list[list] = []  # (suspended) slices
        self.raises: list[tuple] = []  # (ts, exception)
        # Lifetime-drawn calls started on this lane and not ended, in call
        # order (so the last one started last).
        self.open_lifetime: list[_Call] = []

    def name(self) -> str:
        thread = self.thread if self.thread is not None else f"thread {self.tid}"
        if self.task_id is None:
            return thread
        task = self.task if self.task is not None else f"task {self.task_id}"
        return f"{task} · {thread}"


class _Call:
    __slots__ = (
        "awaiting",
        "end",
        "ev",
        "item",
        "lane",
        "mode",
        "run",
        "runs",
        "susp",
    )

    def __init__(self, ev: dict, mode: int, lane: _Lane) -> None:
        self.ev, self.mode, self.lane = ev, mode, lane
        self.item: list | None = None  # the call's slice, or its latest run
        self.susp = None  # start of an open suspension (lifetime-drawn)
        self.run: list | None = None  # the open run (per-run)
        self.runs = 0
        self.awaiting = False  # the open run is suspended in an await
        self.end: dict | None = None  # the return or raise event


class _Builder:
    def __init__(self) -> None:
        self.procs: list[_Proc] = []
        self.next_tid = 1
        self.seq = 0
        self.base = None

    # -- drawing -----------------------------------------------------------

    def add(self, si: int, service: str, capture: Capture) -> None:
        procs: dict = {}
        spans: dict = {}  # pid -> [(start_ns, end_ns)] of its parts, in order
        for rec in capture.processes:
            pid = rec.get("pid")
            p = procs.get(pid)
            if p is None:
                p = procs[pid] = self._new_proc(si, service, pid)
            spans.setdefault(pid, []).append((rec.get("start_ns"), rec.get("end_ns")))
            start = rec.get("start_ns")
            if start is not None and (p.start is None or start < p.start):
                p.start = start
            if rec.get("name") is not None:
                p.name = rec["name"]
            p.end = rec.get("end_ns")  # the last record of a pid wins

        events = sorted(capture.events, key=_ts_of)  # linear when sorted
        if events and (self.base is None or events[0]["ts"] < self.base):
            self.base = events[0]["ts"]
        calls: dict = {}  # (pid, call_id) -> _Call
        for e in events:
            ts = e["ts"]
            pid = e.get("pid")
            p = procs.get(pid)
            if p is None:
                p = procs[pid] = self._new_proc(si, service, pid)
                p.name, p.start = e.get("process"), ts
            p.last_ts = ts
            kind = e.get("type")
            if kind == "call":
                key = (pid, e.get("call_id"))
                old = calls.pop(key, None)
                if old is not None:  # a reused call id: end the old call here
                    self._unfinished(old, ts)
                calls[key] = self._call(p, e)
            elif kind == "return" or kind == "raise":
                c = calls.pop((pid, e.get("call_id")), None)
                lane = None if c is None else self._end(c, e)
                if kind == "raise":  # an orphan's marker goes where it ran
                    lane = lane or self._lane(p, e)
                    lane.raises.append((ts, e.get("exception")))
            elif kind == "suspend":
                c = calls.get((pid, e.get("call_id")))
                if c is not None:
                    self._suspend(c, ts)
            elif kind == "resume":
                c = calls.get((pid, e.get("call_id")))
                if c is not None:
                    self._resume(c, p, e)
            elif kind == "crash" or kind == "note":
                p.instants.append(e)

        for p in procs.values():
            if p.end is None:
                p.end = p.last_ts if p.last_ts is not None else p.start
        for c in calls.values():  # never ended: close at its part's end
            p = c.lane.proc
            self._unfinished(c, _part_end(spans.get(p.pid), c.ev["ts"], p.end))

    def _new_proc(self, si: int, service: str, pid) -> _Proc:
        p = _Proc(si, service, pid)
        self.procs.append(p)
        return p

    def _lane(self, p: _Proc, e: dict) -> _Lane:
        key = (e.get("tid"), e.get("task_id"))
        lane = p.lane_map.get(key)
        if lane is None:
            lane = p.lane_map[key] = _Lane(p, key[0], key[1], self.next_tid)
            self.next_tid += 1
            p.lanes.append(lane)
        if e.get("thread") is not None:
            lane.thread = e["thread"]
        if e.get("task") is not None:
            lane.task = e["task"]
        return lane

    def _slice(
        self, into: list, start, rank: int, call: _Call, run: int = 0, end=None
    ) -> list:
        item = [start, end, rank, self.seq, call, run, 0]
        self.seq += 1
        into.append(item)
        return item

    def _call(self, p: _Proc, e: dict) -> _Call:
        kind = e.get("kind")
        if kind in _PER_RUN_KINDS or (kind == "coroutine" and e.get("task_id") is None):
            mode = _PER_RUN
        elif kind == "coroutine":
            mode = _LIFETIME
        else:
            mode = _FN
        lane = self._lane(p, e)
        c = _Call(e, mode, lane)
        if mode == _PER_RUN:
            self._start_run(c, lane, e["ts"])
        else:
            c.item = self._slice(lane.items, e["ts"], _CALL_RANK, c)
            if mode == _LIFETIME:
                lane.open_lifetime.append(c)
        return c

    def _start_run(self, c: _Call, lane: _Lane, ts) -> None:
        c.runs += 1
        c.run = c.item = self._slice(lane.items, ts, _CALL_RANK, c, c.runs)
        c.lane = lane

    def _close_susp(self, c: _Call, ts) -> None:
        self._slice(c.lane.susp, c.susp, _SUSP_RANK, c, end=max(ts, c.susp))
        c.susp = None

    def _suspend(self, c: _Call, ts) -> None:
        if c.mode == _LIFETIME:
            if c.susp is None:
                c.susp = ts
        elif c.mode == _PER_RUN and c.run is not None and not c.awaiting:
            inner = c.lane.open_lifetime
            if (
                c.ev.get("kind") == "async_generator"
                and inner
                and inner[-1].item[_SEQ] > c.run[_SEQ]
            ):
                # A traced coroutine started in this run (creation order, not
                # time: ticks tie) is still open, so this suspension is an
                # await through it: the run goes on.  (A generator's suspend
                # is always a yield, whatever it stepped meanwhile.)
                c.awaiting = True
            else:
                c.run[_END] = ts
                c.run = None

    def _resume(self, c: _Call, p: _Proc, e: dict) -> None:
        if c.mode == _LIFETIME:
            if c.susp is not None:
                self._close_susp(c, e["ts"])
        elif c.mode == _PER_RUN:
            if c.awaiting:
                c.awaiting = False
            elif c.run is None:
                self._start_run(c, self._lane(p, e), e["ts"])

    def _end(self, c: _Call, e: dict) -> _Lane:
        """End ``c`` at ``e``; returns the lane of its last slice."""
        ts = e["ts"]
        c.end = e
        if c.mode == _PER_RUN:
            if c.run is not None:
                c.run[_END] = max(ts, c.run[_START])
                c.run = None
        else:
            if c.susp is not None:
                self._close_susp(c, ts)
            c.item[_END] = max(ts, c.item[_START])
            if c.mode == _LIFETIME:
                c.lane.open_lifetime.remove(c)
        return c.lane

    def _unfinished(self, c: _Call, end) -> None:
        if c.susp is not None:
            self._close_susp(c, end)
        if c.mode == _PER_RUN and c.run is None:
            # Suspended for good: its last run, as drawn, never ended.
            if c.item is not None:
                c.item[_FLAGS] |= _UNFINISHED
        else:
            item = c.run if c.mode == _PER_RUN else c.item
            if item is not None:
                item[_END] = max(end, item[_START])
                item[_FLAGS] |= _UNFINISHED
        c.run = None
        if c.mode == _LIFETIME:
            c.lane.open_lifetime.remove(c)

    # -- output ------------------------------------------------------------

    def events(self, base: int):
        """Yield the trace events: metadata, then each lane's slices."""
        procs = sorted(self.procs, key=lambda p: (p.si, p.pid))
        by_service: dict = {}
        for n, p in enumerate(procs, 1):
            p.syn = n
            by_service.setdefault(p.si, []).append(p)
        stride = _SORT_STRIDE
        while stride < max((len(ps) for ps in by_service.values()), default=0):
            stride *= 10  # keep each service's processes grouped
        for si, ps in by_service.items():
            ps.sort(key=lambda p: (p.start if p.start is not None else 0, p.pid))
            for order, p in enumerate(ps):
                p.sort = si * stride + order
        for p in procs:  # every event names a tid that belongs to its pid
            if p.lanes:
                p.anchor = p.lanes[0].syn
            else:
                p.anchor = self.next_tid
                self.next_tid += 1

        wall = _wall_clock()
        for p in procs:
            pid = p.syn
            name = f"{p.service} · {p.name or 'process'} ({p.pid})"
            yield _meta(pid, p.anchor, "process_name", "name", name)
            yield _meta(pid, p.anchor, "process_sort_index", "sort_index", p.sort)
            lanes = _lane_order(p.lanes)
            for index, lane in enumerate(lanes):
                yield _meta(pid, lane.syn, "thread_name", "name", lane.name())
                yield _meta(pid, lane.syn, "thread_sort_index", "sort_index", index)
            for lane in lanes:
                tid = lane.syn
                for item in _nest(lane.items + _innermost(lane.susp)):
                    yield _slice_event(pid, tid, item, base, wall)
                for ts, exc in lane.raises:
                    yield {
                        "ph": "i",
                        "s": "t",
                        "name": f"raise {_exc_type(exc)}",
                        "pid": pid,
                        "tid": tid,
                        "ts": _us(ts - base),
                        "args": {"exception": exc},
                    }
            for e in p.instants:
                yield _process_instant(pid, p.anchor, e, base)


def _part_end(spans: list | None, ts, default):
    """The end of the part a call at ``ts`` began in, when its pid has
    several (an exec: the calls open at it end with the part before it);
    else ``default``, the process's end."""
    if not spans or len(spans) < 2:
        return default
    end = None
    for start, part_end in spans:
        if start is not None and start <= ts:
            end = part_end
    return default if end is None else end


def _lane_order(lanes: list[_Lane]) -> list[_Lane]:
    """Threads by first event; each thread lane, then its task lanes by first event."""
    threads: dict = {}
    for lane in lanes:
        threads.setdefault(lane.tid, []).append(lane)
    out: list[_Lane] = []
    for group in threads.values():
        out += [lane for lane in group if lane.task_id is None]
        out += [lane for lane in group if lane.task_id is not None]
    return out


def _key(item: list) -> tuple:
    return (item[_START], -item[_END], item[_RANK], item[_SEQ])


def _innermost(susp: list[list]) -> list[list]:
    """Drop each suspension contained in another (keep the innermost frame's)."""
    susp.sort(key=_key)
    kept: list[list] = []
    reach = None  # the latest end of the suspensions so far
    for item in susp:
        if reach is not None and item[_END] <= reach:
            continue  # contained: an earlier one starts no later, ends no earlier
        kept.append(item)
        reach = item[_END]
    return kept


def _nest(items: list[list]) -> list[list]:
    """Return one lane's slices in nesting order, repaired to nest properly.

    A sweep by start time keeps the stack of slices enclosing the current
    start.  A slice that runs past the end of the top of that stack is a
    partial overlap, repaired as described in the module docstring.  The
    repairs are deterministic and every result nests: a trimmed suspension
    re-enters the sweep at its new start, and a slice cut short has no
    children left past its new end.
    """
    items.sort(key=_key)
    out: list[list] = []
    stack: list[list] = []
    later: list = []  # heap of (key, item): suspensions re-entering
    i, n = 0, len(items)
    while i < n or later:
        if later and (i == n or later[0][0] < _key(items[i])):
            item = heappop(later)[1]
        else:
            item = items[i]
            i += 1
        start = item[_START]
        while stack and stack[-1][_END] <= start:
            stack.pop()
        while stack and item[_END] > stack[-1][_END]:
            top = stack[-1]
            if item[_RANK] == _SUSP_RANK:  # trim the suspension's start
                item[_START] = top[_END]
                item[_FLAGS] |= _CLIPPED
                if item[_START] < item[_CALL].item[_END]:
                    heappush(later, (_key(item), item))
                # else it now starts where its own call (cut short) has
                # ended: outside that call, so it is dropped
                break
            if top[_RANK] == _SUSP_RANK:  # a slice starts: the wait is over
                top[_END] = start
                top[_FLAGS] |= _CLIPPED
                stack.pop()
            else:  # cut the slice at its enclosing slice's end
                item[_END] = top[_END]
                item[_FLAGS] |= _CLIPPED
        else:
            stack.append(item)
            out.append(item)
    return out


def _slice_event(pid: int, tid: int, item: list, base: int, wall) -> dict:
    start, end, c = item[_START], item[_END], item[_CALL]
    call = c.ev
    flags = item[_FLAGS]
    if item[_RANK] == _SUSP_RANK:
        name, cat = "(suspended)", "suspended"
        args = {"call_id": call.get("call_id")}
    else:
        name, cat = call.get("function") or "?", "call"
        args = {
            "file": f"{call.get('file')}:{call.get('line')}",
            "call_id": call.get("call_id"),
            "wall": wall(start),
        }
        run = item[_RUN]
        if run:
            args["run"] = run
        if run <= 1 and call.get("values") is not None:
            args["values"] = call["values"]
        if item is c.item and c.end is not None:
            if c.end.get("type") == "return":
                if c.end.get("value") is not None:
                    args["return"] = c.end["value"]
            else:
                args["exception"] = c.end.get("exception")
        if flags & _UNFINISHED:
            args["unfinished"] = True
    if flags & _CLIPPED:
        args["clipped"] = True
    return {
        "ph": "X",
        "name": name,
        "cat": cat,
        "pid": pid,
        "tid": tid,
        "ts": _us(start - base),
        "dur": _us(end - start),
        "args": args,
    }


def _process_instant(pid: int, tid: int, e: dict, base: int) -> dict:
    if e.get("type") == "note":
        name, args = "note", {"message": e.get("message")}
    else:
        exc = e.get("exception")
        kind = _exc_type(exc)
        name = "INTERRUPTED" if kind == "KeyboardInterrupt" else f"CRASH: {kind}"
        args = {
            "exception": exc,
            "where": e.get("where"),
            "traceback": e.get("traceback"),
        }
    return {
        "ph": "i",
        "s": "p",
        "name": name,
        "pid": pid,
        "tid": tid,
        "ts": _us(e["ts"] - base),
        "args": args,
    }


def _meta(pid: int, tid: int, name: str, key: str, value) -> dict:
    return {
        "ph": "M",
        "name": name,
        "pid": pid,
        "tid": tid,
        "ts": 0,
        "args": {key: value},
    }


def _exc_type(exc) -> str:
    """``"KeyError"`` from ``"KeyError: 'sku'"``."""
    return str(exc).partition(":")[0].strip() if exc else "?"


def _us(ns: int) -> float:
    """Nanoseconds to Perfetto microseconds, keeping every nanosecond."""
    return round(ns / 1000, 3)


def _wall_clock():
    """A ``ns -> "HH:MM:SS.ffffff"`` (local time) formatter, cached per second."""
    cache: dict = {}

    def wall(ns: int) -> str:
        sec, rest = divmod(ns, 1_000_000_000)
        hms = cache.get(sec)
        if hms is None:
            hms = cache[sec] = time.strftime("%H:%M:%S", time.localtime(sec))
        return f"{hms}.{rest // 1000:06d}"

    return wall


def _iso_local(ns: int) -> str:
    sec, rest = divmod(ns, 1_000_000_000)
    local = datetime.fromtimestamp(sec, UTC).astimezone()
    return local.replace(microsecond=rest // 1000).isoformat()


def _service_names(captures: list[Capture]) -> list[str]:
    names: list[str] = []
    used: set[str] = set()
    for capture in captures:
        name = str(capture.header.get("name") or "trace")
        unique, n = name, 1
        while unique in used:
            n += 1
            unique = f"{name}#{n}"
        used.add(unique)
        names.append(unique)
    return names


def _render(captures: list[Capture]):
    services = _service_names(captures)
    builder = _Builder()
    for si, capture in enumerate(captures):
        builder.add(si, services[si], capture)
    base = builder.base
    if base is None:  # no events at all
        starts = [c.header.get("start_ns") for c in captures]
        base = min((s for s in starts if s is not None), default=0)
    metadata = {
        "tdb_base_epoch_ns": base,
        "start": _iso_local(base),
        "services": services,
    }
    return metadata, builder.events(base)


def build_trace(captures: list[Capture]) -> dict:
    """Render ``captures`` (one per service) as a Chrome/Perfetto JSON trace."""
    metadata, events = _render(captures)
    return {"traceEvents": list(events), "displayTimeUnit": "ns", "metadata": metadata}


def write_trace(captures: list[Capture], path: Path) -> None:
    """Write the trace of ``captures`` to ``path``, one event per line.

    Events are encoded one at a time, so a capture of millions of events is
    never held as one JSON string.  Output is ASCII (``ensure_ascii``): a
    captured value can hold a lone surrogate, which UTF-8 cannot encode.
    """
    metadata, events = _render(captures)
    encode = json.JSONEncoder(separators=(",", ":")).encode
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"traceEvents":[')
        sep = "\n"
        for ev in events:
            f.write(sep)
            f.write(encode(ev))
            sep = ",\n"
        f.write('\n],"displayTimeUnit":"ns","metadata":')
        f.write(encode(metadata))
        f.write("}\n")
