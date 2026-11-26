"""Run a program under the trace agent and read back its part files.

Each ``run_traced``/``start_traced`` call gets a fresh parts directory
(and config file inside it) under ``tmp_path``, so one test can trace
several programs.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tdb.trace.contract import DEFAULT_MAX_EVENTS, build_env, write_config
from tdb.trace.spec import norm_path

_PART = re.compile(r"^\d+(-\d+)?\.jsonl$")
_MARKER = re.compile(r"^(skipped|agent-error)-\d+(-\d+)?\.json$")


def _function_entry(spec: str | dict) -> dict:
    entry = {
        "pattern": None,
        "capture": [],
        "return": False,
        "if": None,
        "max_hits": None,
    }
    entry.update({"pattern": spec} if isinstance(spec, str) else spec)
    return entry


def target(path, *functions: str | dict) -> dict:
    """A config TARGET for one file; a str function takes default options."""
    return {
        "files": [norm_path(path)],
        "suffixes": [],
        "functions": [_function_entry(f) for f in functions],
    }


# A finalizer that waits for a lock the main thread holds, run by the
# flusher: garbage collection is off until the main thread sleeps, so the
# flusher, the only thread then running, collects the garbage and gets stuck.
STUCK_FLUSHER = (
    "import _thread, gc, os, threading, time\n"
    "L = threading.RLock()\nmain, stuck = _thread.get_ident(), []\n"
    "class D:\n    def __init__(self):\n        self.me = self\n"
    "    def __del__(self):\n        if _thread.get_ident() != main:\n            stuck.append(1)\n"
    "        with L:\n            pass\n"
    "def f():\n    return 1\n"
    "thresholds = gc.get_threshold()\n"
    "with L:\n    gc.disable()\n    for _ in range(100):\n        D()\n"
    "    for _ in range(5_000):\n        f()\n"  # records left for the flusher
    "    gc.set_threshold(1)\n    gc.enable()\n    time.sleep(0.6)\n"
    "    gc.set_threshold(*thresholds)\n"
)


@dataclass
class Parts:
    dir: Path
    records: dict[str, list[dict]]  # part file stem -> its records
    markers: list[dict]
    returncode: int
    stdout: str
    stderr: str

    def all(self, kind: str) -> list[dict]:
        return [r for recs in self.records.values() for r in recs if r["k"] == kind]

    def qual_of(self, stem: str, fn_id: int) -> str:
        return {r["id"]: r["qual"] for r in self.records[stem] if r["k"] == "fn"}[fn_id]

    def calls(self, qual: str) -> list[dict]:
        out = []
        for stem, recs in self.records.items():
            quals = {r["id"]: r["qual"] for r in recs if r["k"] == "fn"}
            out.extend(
                {**r, "_stem": stem}
                for r in recs
                if r["k"] == "call" and quals.get(r["fn"]) == qual
            )
        return out

    def events_for(self, stem: str, call_id: int) -> list[dict]:
        return [r for r in self.records[stem] if r.get("c") == call_id]


def _read_part(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").split("\n")
    # A process killed mid-write may leave a cut-off last line; anything
    # else unparsable is an agent bug and fails the test.
    if lines[-1]:
        lines.pop()
    return [json.loads(line) for line in lines if line]


def read_parts(
    parts_dir: Path, *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> Parts:
    """Read a parts directory; the other fields are whatever the caller knows."""
    parts_dir = Path(parts_dir)
    names = sorted(p.name for p in parts_dir.iterdir())
    records = {Path(n).stem: _read_part(parts_dir / n) for n in names if _PART.match(n)}
    markers = [
        json.loads((parts_dir / n).read_text(encoding="utf-8"))
        for n in names
        if _MARKER.match(n)
    ]
    return Parts(parts_dir, records, markers, returncode, stdout, stderr)


def _prepare(
    tmp_path, source, targets, *, mode, max_events, python, args, extra_env, files
):
    tmp_path = Path(tmp_path)
    prog = tmp_path / "prog.py"
    prog.write_text(source, encoding="utf-8")
    for name, text in (files or {}).items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    if targets is None:
        targets = [target(prog, "*")]
    parts_dir = Path(tempfile.mkdtemp(prefix="parts-", dir=tmp_path))
    config = parts_dir / "config.json"
    write_config(
        config, mode=mode, parts_dir=parts_dir, targets=targets, max_events=max_events
    )
    extra_env = extra_env or {}
    base = dict(os.environ)
    base.update({k: v for k, v in extra_env.items() if not k.startswith("TDB_TRACE_")})
    env = build_env(base, config_path=config, parts_dir=parts_dir)
    env.update({k: v for k, v in extra_env.items() if k.startswith("TDB_TRACE_")})
    return [python, "prog.py", *args], env, parts_dir


def run_traced(
    tmp_path,
    source: str,
    targets: list[dict] | None = None,
    *,
    mode="trace",
    max_events=DEFAULT_MAX_EVENTS,
    python=sys.executable,
    args=(),
    timeout=60,
    extra_env=None,
    files: dict[str, str] | None = None,
    own_group: bool = False,
) -> Parts:
    """Write ``source`` to ``tmp_path/prog.py``, run it traced, read its parts.

    ``targets=None`` traces every function in prog.py.  ``own_group`` (for
    programs that start children; POSIX) runs the program in a new session
    and SIGKILLs whatever is left of its process group afterwards, even when
    it times out, so no child outlives the test.
    """
    cmd, env, parts_dir = _prepare(
        tmp_path,
        source,
        targets,
        mode=mode,
        max_events=max_events,
        python=python,
        args=args,
        extra_env=extra_env,
        files=files,
    )
    if not own_group or os.name == "nt":
        done = subprocess.run(
            cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=timeout
        )
        return read_parts(
            parts_dir,
            returncode=done.returncode,
            stdout=done.stdout,
            stderr=done.stderr,
        )
    proc = subprocess.Popen(
        cmd,
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        _kill_group(proc.pid)
        proc.communicate()
        raise
    _kill_group(proc.pid)  # children that outlived the program, if any
    return read_parts(parts_dir, returncode=proc.returncode, stdout=out, stderr=err)


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:  # no process left in the group
        pass


def start_traced(
    tmp_path,
    source: str,
    targets: list[dict] | None = None,
    *,
    mode="trace",
    max_events=DEFAULT_MAX_EVENTS,
    python=sys.executable,
    args=(),
    extra_env=None,
    files: dict[str, str] | None = None,
) -> tuple[subprocess.Popen, Path]:
    """Like run_traced, without waiting: returns the process (stdout piped)
    and its parts directory, for ``read_parts`` afterwards."""
    cmd, env, parts_dir = _prepare(
        tmp_path,
        source,
        targets,
        mode=mode,
        max_events=max_events,
        python=python,
        args=args,
        extra_env=extra_env,
        files=files,
    )
    proc = subprocess.Popen(
        cmd, cwd=tmp_path, env=env, stdout=subprocess.PIPE, text=True
    )
    return proc, parts_dir
