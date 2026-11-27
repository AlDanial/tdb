"""Opt-in: Perfetto's own importer reads tdb's traces as tdb drew them.

Runs only where Perfetto's ``trace_processor_shell`` is found: at the path
in ``TDB_TRACE_PROCESSOR``, else on ``PATH``; nothing is downloaded.  Its
directory is put first on ``LD_LIBRARY_PATH`` for it.  Each trace is
loaded with ``trace_processor_shell -q SQLFILE TRACE``, and then:

- Perfetto's import stats show no error and no data loss;
- on every lane (a thread track, or a process's own track for
  process-scoped instants) Perfetto's slices are exactly the trace's ``X``
  and instant events: same ``ts`` and ``dur`` in nanoseconds, same name;
- each ``X`` slice is at the depth its containment gives (ties nest in
  file order), so none was dropped or re-nested for overlapping;
- the ``args.clipped`` and ``args.unfinished`` flags all arrive.

The traces: the perfetto unit tests' mixed and random captures, the docs'
two-service walkthrough merged, and a capture with a real exec.
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from tdb.trace.capture import load_capture
from tdb.trace.perfetto import write_trace
from tests.unit.test_trace_perfetto import mixed_capture, random_capture

TP_ENV = "TDB_TRACE_PROCESSOR"


def _trace_processor() -> str | None:
    path = os.environ.get(TP_ENV) or shutil.which("trace_processor_shell")
    return path if path and os.path.isfile(path) else None


TP = _trace_processor()
pytestmark = pytest.mark.skipif(
    TP is None,
    reason=f"no trace_processor_shell (set {TP_ENV}, or put it on PATH)",
)
traced = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring")
posix = pytest.mark.skipif(os.name == "nt", reason="exec keeps the pid")

# Each row starts with its kind; trace_processor_shell prints every
# statement's rows as CSV, each set after a header row of its own.
SQL = """\
select 'STAT' as k, name, severity, value
from stats
where value != 0 and severity in ('error', 'data_losses');

select 'SL' as k, s.ts, s.dur, s.depth, s.name,
  coalesce(t.tid, -1) as tid, coalesce(p.pid, p2.pid, -1) as pid,
  case when tt.id is not null then 't' when pt.id is not null then 'p' else 'o' end
    as scope
from slice s
  left join thread_track tt on s.track_id = tt.id
  left join thread t using (utid)
  left join process p on t.upid = p.upid
  left join process_track pt on s.track_id = pt.id
  left join process p2 on pt.upid = p2.upid
order by s.id;

select 'ARG' as k, s.name, a.key
from slice s join args a using (arg_set_id)
where a.key in ('args.clipped', 'args.unfinished');
"""


def _ns(us: float) -> int:
    """A trace time (microseconds, three decimals) in nanoseconds."""
    return round(us * 1000)


def _lane(pid, tid, scope: str):
    """A thread track, or (pid, "process") for a process-scoped instant."""
    return (pid, tid) if scope == "t" else (pid, "process")


def _depths(slices: list[tuple]) -> list[int]:
    """Each slice's depth by containment of [ts, ts + dur), slices in file
    order: sorted by (start, longest first), ties staying in file order;
    a zero-length slice at a zero-length slice's time nests in it."""
    depth = [0] * len(slices)
    stack: list[tuple[int, int]] = []  # (start, end) of the enclosing slices
    for i in sorted(range(len(slices)), key=lambda i: (slices[i][0], -slices[i][1])):
        ts, dur, _ = slices[i]
        while stack and stack[-1][1] <= ts and not (dur == 0 and stack[-1] == (ts, ts)):
            stack.pop()
        depth[i] = len(stack)
        stack.append((ts, ts + dur))
    return depth


def _import(trace: Path, tmp_path: Path) -> list[list[str]]:
    """Load ``trace`` in trace_processor_shell; the rows of SQL."""
    sql = tmp_path / "import-check.sql"
    sql.write_text(SQL, encoding="utf-8")
    env = dict(os.environ)
    libs = env.get("LD_LIBRARY_PATH")
    env["LD_LIBRARY_PATH"] = str(Path(TP).parent) + (f":{libs}" if libs else "")
    done = subprocess.run(
        [TP, "-q", str(sql), str(trace)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, f"{trace.name}: {done.stderr[-2000:]}"
    rows = csv.reader(io.StringIO(done.stdout))
    return [row for row in rows if row and row[0] in ("STAT", "SL", "ARG")]


def import_problems(trace: Path, tmp_path: Path) -> list[str]:
    """What Perfetto's import of ``trace`` changed or lost (empty: none)."""
    events = json.loads(trace.read_text(encoding="utf-8"))["traceEvents"]
    expected: dict = defaultdict(Counter)  # lane -> {(ts, dur, name): count}
    xs: dict = defaultdict(list)  # lane -> its X slices, in file order
    instants: dict = defaultdict(set)  # lane -> its instants
    flags = 0
    for e in events:
        if e["ph"] == "X":
            lane, sl = (e["pid"], e["tid"]), (_ns(e["ts"]), _ns(e["dur"]), e["name"])
            xs[lane].append(sl)
            flags += sum(k in e.get("args", {}) for k in ("clipped", "unfinished"))
        elif e["ph"] == "i":
            lane = _lane(e["pid"], e["tid"], "t" if e.get("s") == "t" else "p")
            sl = (_ns(e["ts"]), 0, e["name"])
            instants[lane].add(sl)
        else:
            continue
        expected[lane][sl] += 1

    rows = _import(trace, tmp_path)
    problems = [
        f"Perfetto stat {r[1]} ({r[2]}): {r[3]}" for r in rows if r[0] == "STAT"
    ]
    got: dict = defaultdict(Counter)
    got_depths: dict = defaultdict(Counter)  # lane -> {(slice, depth): count}
    for _, ts, dur, depth, name, tid, pid, scope in (r for r in rows if r[0] == "SL"):
        lane, sl = _lane(int(pid), int(tid), scope), (int(ts), int(dur), name)
        got[lane][sl] += 1
        got_depths[lane][(sl, int(depth))] += 1
    for lane in expected.keys() | got.keys():
        want, have = expected[lane], got[lane]
        if want != have:
            problems.append(
                f"lane {lane}: only in the trace {list((want - have).elements())[:4]}, "
                f"only in Perfetto {list((have - want).elements())[:4]}"
            )
    for lane, slices in xs.items():
        want = Counter(zip(slices, _depths(slices)))
        known = set(slices)
        have = Counter(
            {
                (sl, depth): n
                for (sl, depth), n in got_depths[lane].items()
                if sl in known and not (sl[1] == 0 and sl in instants[lane])
            }
        )
        if want != have:
            problems.append(
                f"lane {lane}: depths differ: trace {list((want - have).elements())[:3]}"
                f", Perfetto {list((have - want).elements())[:3]}"
            )
    arrived = sum(1 for r in rows if r[0] == "ARG")
    if arrived != flags:
        problems.append(
            f"args.clipped/unfinished: {flags} in the trace, {arrived} in Perfetto"
        )
    return problems


def test_unit_test_captures_import(tmp_path):
    """(a) The perfetto unit tests' mixed capture (lifetimes, runs across
    threads and tasks, cancellation, clipped and unfinished slices) and
    random captures, two services each."""
    traces = {"mixed": [mixed_capture()]}
    for seed in range(12):
        rng = random.Random(seed)
        traces[f"random-{seed}"] = [random_capture(rng), random_capture(rng)]
    for name, captures in traces.items():
        out = tmp_path / f"{name}.perfetto.json"
        write_trace(captures, out)
        assert import_problems(out, tmp_path) == [], name


@traced
@posix
def test_walkthrough_merge_imports(tmp_path):
    """(b) The docs' walkthrough: two services traced, then merged."""
    from tests.integration.test_trace_examples import run_walkthrough

    assert import_problems(run_walkthrough(tmp_path), tmp_path) == []


@traced
@posix
def test_exec_trace_imports(tmp_path):
    """(c) A main process that execs another traced program: main, open
    at the exec, ends where its part ends (Ruling 32), and Perfetto shows
    it so, before the new program's call on the same thread."""
    from tests.integration.test_trace_launch import run_exec_program

    r = run_exec_program(tmp_path)
    assert r.returncode == 0, r.stderr
    trace = tmp_path / "out.perfetto.json"
    assert import_problems(trace, tmp_path) == []

    base = json.loads(trace.read_text(encoding="utf-8"))["metadata"][
        "tdb_base_epoch_ns"
    ]
    pre, post = load_capture(tmp_path / "out.jsonl").processes
    ends = {
        r[4]: (int(r[1]), int(r[1]) + int(r[2]))
        for r in _import(trace, tmp_path)
        if r[0] == "SL"
    }
    assert ends["main"][1] == pre["end_ns"] - base
    assert ends["main"][0] < ends["before"][0] < ends["before"][1] < ends["main"][1]
    assert ends["after"][0] >= post["start_ns"] - base > ends["main"][1]


def test_the_check_sees_a_partial_overlap(tmp_path):
    """Negative control: a slice that overlaps its neighbour partially,
    which tdb never writes, is caught (Perfetto drops or re-nests it)."""
    bad = tmp_path / "bad.json"
    x = {"ph": "X", "pid": 1, "tid": 1, "cat": "call", "args": {}}
    events = [{**x, "name": "a", "ts": 0.0, "dur": 2.0}]
    events.append({**x, "name": "b", "ts": 1.0, "dur": 2.0})
    bad.write_text(json.dumps({"traceEvents": events}), encoding="utf-8")
    problems = import_problems(bad, tmp_path)
    assert problems and any("lane (1, 1)" in p for p in problems), problems
