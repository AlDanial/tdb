"""Assemble trace part files into the user-facing capture file.

The tracer agent writes one JSON-lines part file per traced process into a
parts directory.  ``assemble`` reads that directory and produces a
:class:`Capture` (header, one ``process`` record per part, and events sorted
by epoch timestamp) plus a :class:`CaptureSummary`.  ``write_capture`` and
``load_capture`` store and read the self-contained ``BASE.jsonl`` form, and
``load_input`` accepts either that file or a parts directory (for merging).

Ids (call, task, fn, thread) are meaningful only within one part, so
definitions are resolved per part.  When a pid has several parts (a forked
process that then exec'd another traced program), call and task ids are
renumbered across that pid's parts so ``call_id`` stays unique per pid.

Each part's ``ended`` comes from its own last records: ``exec`` when its
last record is an ``exec`` record (``{"k": "exec", "t": ...}``, which the
agent writes just before ``os.execv``/``os.execve``), ``clean`` when it has
an ``exit`` record, else ``abrupt``; the main process's last part takes
``signal:NAME`` when the program died by a signal.

This module runs inside tdb, not the traced program; it must work on
Python 3.11.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PART_FILE = re.compile(r"^\d+(-\d+)?\.jsonl$")
_MARKER_GLOBS = (("skipped-*.json", "skipped"), ("agent-error-*.json", "agent_errors"))
_HEADER_KEYS = ("name", "tdb", "command", "cwd", "spec", "host")

_LANE_TYPES = {"susp": "suspend", "res": "resume", "raise": "raise", "ret": "return"}


@dataclass
class Capture:
    header: dict
    processes: list[dict]
    events: list[dict]


@dataclass
class CaptureSummary:
    processes: int = 0
    threads: int = 0
    tasks: int = 0
    calls: int = 0
    skipped: list[dict] = field(default_factory=list)
    agent_errors: list[dict] = field(default_factory=list)
    abrupt: list[int] = field(default_factory=list)
    # (name, pid, signal name): the main process, killed by a signal before
    # it wrote its exit record
    killed: list[tuple[str, int, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    called: set[tuple[str, str]] = field(default_factory=set)


def _part_key(path: Path) -> tuple[int, int]:
    stem = path.name[: -len(".jsonl")]
    pid, _, seq = stem.partition("-")
    return int(pid), int(seq) if seq else 1


def _records(path: Path, bad: list[int]):
    """Yield parsed JSON objects of a part; count unparsable lines in ``bad[0]``."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                rec = None
            if isinstance(rec, dict):
                yield rec
            else:
                bad[0] += 1


def _read_markers(parts_dir: Path, summary: CaptureSummary) -> None:
    for pattern, attr in _MARKER_GLOBS:
        for path in sorted(parts_dir.glob(pattern)):
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                summary.notes.append(f"{path.name}: unreadable marker; skipped")
                continue
            if isinstance(data, dict):
                getattr(summary, attr).append(data)


class _PidState:
    """Per-pid running maxima used to renumber ids across that pid's parts,
    and the pid's parts: (process record, how the part ended)."""

    def __init__(self) -> None:
        self.call_max = 0
        self.task_max = 0
        self.parts: list[tuple[dict, str | None]] = []


def _read_part(
    path: Path,
    state_for,
    summary: CaptureSummary,
    events: list[dict],
    threads: set,
    tasks: set,
) -> tuple[dict, str | None] | None:
    """Read one part; append its events.  Returns (process record, how it
    ended): "exec" if its last record is an exec record, else "exit" if it
    has an exit record, else None."""
    bad = [0]
    it = _records(path, bad)
    hdr = next(it, None)
    if hdr is None or hdr.get("k") != "hdr":
        summary.notes.append(f"{path.name}: no header; skipped")
        return None
    pid = hdr.get("pid", _part_key(path)[0])
    wall, pc = hdr.get("wall", 0), hdr.get("pc", 0)
    base = wall - pc
    state = state_for(pid)
    call_off, task_off = state.call_max, state.task_max
    call_hi = task_hi = 0
    proc_name = hdr.get("proc")
    fns: dict = {}
    th_names: dict = {}
    task_names: dict = {}
    first = len(events)
    pending: list[tuple[dict, object]] = []  # calls whose fn record came later
    last_t = None
    saw_exit = False
    exit_t = None
    last_k = None

    for rec in it:
        k = last_k = rec.get("k")
        if k == "fn":
            fns[rec.get("id")] = rec
            continue
        if k == "th":
            th_names[rec.get("tid")] = rec.get("name")
            continue
        if k == "task":
            task_names[rec.get("id")] = rec.get("name")
            if isinstance(rec.get("id"), int):
                task_hi = max(task_hi, rec["id"])
            continue
        if k == "proc":
            proc_name = rec.get("name", proc_name)
        t = rec.get("t")
        if t is not None:
            last_t = t
        if k == "exit":
            saw_exit = True
            exit_t = t
            continue
        if k == "count" or k == "proc" or k == "exec" or t is None:
            continue

        tid = rec.get("tid")
        task = rec.get("task")
        ev = {
            "type": None,
            "ts": base + t,
            "pid": pid,
            "process": None,
            "tid": tid,
            "thread": th_names.get(tid),
            "task_id": None if task is None else task + task_off,
            "task": None if task is None else task_names.get(task),
        }
        if tid is not None:
            threads.add((pid, tid))
        if task is not None:
            tasks.add((pid, task + task_off))
            task_hi = max(task_hi, task)
        if k == "call":
            c = rec.get("c", 0)
            call_hi = max(call_hi, c)
            fn = fns.get(rec.get("fn"))
            ev["type"] = "call"
            ev.update(file=None, function=None, line=None, kind=None)
            ev["call_id"] = c + call_off
            ev["values"] = rec.get("v")
            if fn is None:
                pending.append((ev, rec.get("fn")))
            else:
                _fill_fn(ev, fn, summary)
        elif k in _LANE_TYPES:
            c = rec.get("c", 0)
            call_hi = max(call_hi, c)
            ev["type"] = _LANE_TYPES[k]
            ev["call_id"] = c + call_off
            if k == "ret":
                ev["value"] = rec.get("v")
            elif k == "raise":
                ev["exception"] = rec.get("exc")
        elif k == "crash":
            ev["type"] = "crash"
            ev["exception"] = rec.get("exc")
            ev["where"] = rec.get("where")
            ev["traceback"] = rec.get("tb")
        elif k == "note":
            ev["type"] = "note"
            ev["message"] = rec.get("msg")
            summary.notes.append(f"pid {pid}: {rec.get('msg')}")
        else:
            continue
        events.append(ev)

    for ev, fn_id in pending:
        fn = fns.get(fn_id)
        if fn is not None:
            _fill_fn(ev, fn, summary)
    for ev in events[first:]:
        ev["process"] = proc_name
    for th in th_names:
        threads.add((pid, th))
    for tk in task_names:
        tasks.add((pid, tk + task_off))
    state.call_max = call_off + call_hi
    state.task_max = task_off + task_hi
    if bad[0]:
        summary.notes.append(f"{bad[0]} unreadable line(s) in {path.name}")
    end_t = exit_t if saw_exit and exit_t is not None else last_t
    proc = {
        "type": "process",
        "pid": pid,
        "ppid": hdr.get("ppid"),
        "name": proc_name,
        "argv": hdr.get("argv"),
        "python": hdr.get("py"),
        "start_ns": wall,
        "end_ns": wall if end_t is None else base + end_t,
        "ended": None,
    }
    return proc, "exec" if last_k == "exec" else "exit" if saw_exit else None


def _fill_fn(ev: dict, fn: dict, summary: CaptureSummary) -> None:
    ev["file"] = fn.get("file")
    ev["function"] = fn.get("qual")
    ev["line"] = fn.get("line")
    ev["kind"] = fn.get("kind")
    summary.called.add((ev["file"], ev["function"]))


def assemble(
    parts_dir: Path,
    *,
    header: dict,
    main_pid: int | None = None,
    main_signal: str | None = None,
) -> tuple[Capture, CaptureSummary]:
    """Turn a parts directory into a capture and its summary."""
    parts_dir = Path(parts_dir)
    summary = CaptureSummary()
    events: list[dict] = []
    threads: set = set()
    tasks: set = set()
    states: dict[int, _PidState] = {}
    processes: list[dict] = []

    def state_for(pid: int) -> _PidState:
        return states.setdefault(pid, _PidState())

    files = sorted(
        (p for p in parts_dir.iterdir() if PART_FILE.match(p.name)), key=_part_key
    )
    for path in files:
        got = _read_part(path, state_for, summary, events, threads, tasks)
        if got is None:
            continue
        state_for(got[0]["pid"]).parts.append(got)
        processes.append(got[0])

    for pid, st in states.items():
        for i, (proc, how) in enumerate(st.parts):
            if pid == main_pid and main_signal and i == len(st.parts) - 1:
                proc["ended"] = f"signal:{main_signal}"
                if how is None:  # killed before it could write its last records
                    name = header.get("name") or proc["name"] or "the program"
                    summary.killed.append((name, pid, main_signal))
            elif how == "exec":
                proc["ended"] = "exec"
            elif how == "exit":
                proc["ended"] = "clean"
            else:
                proc["ended"] = "abrupt"
                summary.abrupt.append(pid)

    _read_markers(parts_dir, summary)
    events.sort(key=lambda e: e["ts"])  # stable: ties keep file order
    summary.processes = len(processes)
    summary.threads = len(threads)
    summary.tasks = len(tasks)
    summary.calls = sum(1 for e in events if e["type"] == "call")

    hdr = {"type": "capture", "version": 1}
    for key in _HEADER_KEYS:
        hdr[key] = header.get(key)
    starts = [p["start_ns"] for p in processes]
    ends = [p["end_ns"] for p in processes] + ([events[-1]["ts"]] if events else [])
    hdr["start_ns"] = min(starts) if starts else 0
    hdr["end_ns"] = max(ends) if ends else hdr["start_ns"]
    hdr["exit_code"] = header.get("exit_code")
    return Capture(hdr, processes, events), summary


def write_capture(capture: Capture, path: Path) -> None:
    """Write ``capture`` as JSON lines: header, processes, then events."""
    with open(path, "w", encoding="utf-8") as f:
        for rec in (capture.header, *capture.processes, *capture.events):
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def load_capture(path: Path) -> Capture:
    """Read a capture file written by ``write_capture``."""
    header: dict = {}
    processes: list[dict] = []
    events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec.get("type")
            if kind == "capture":
                header = rec
            elif kind == "process":
                processes.append(rec)
            else:
                events.append(rec)
    if not header:
        raise ValueError(f"{path}: not a tdb capture (no capture header)")
    return Capture(header, processes, events)


def load_input(path: Path) -> Capture:
    """Load a ``.jsonl`` capture or assemble a ``.parts`` directory."""
    path = Path(path)
    if path.is_dir():
        try:
            run = json.loads((path / "run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            run = {}
        if not isinstance(run, dict):
            run = {}
        return assemble(path, header={**run, "exit_code": None})[0]
    return load_capture(path)
