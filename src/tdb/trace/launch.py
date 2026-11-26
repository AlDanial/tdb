"""Run a program under the trace agent and turn its parts into outputs.

``tdb --trace SPEC PROGRAM`` (``cli._run_trace``) builds a
:class:`TraceRequest` and calls :func:`run`, which:

1. resolves what to run: a ``.py`` program or ``-m MODULE`` under an
   interpreter (checked to be Python 3.12 or newer), or any other command,
   run as is (the tracer loads in whatever Python it starts);
2. creates ``BASE.parts/`` holding ``run.json`` (the capture header fields
   known at launch) and ``config.json`` (the agent config);
3. starts the program with the agent's environment on tdb's own terminal
   and in tdb's process group, so Ctrl-C reaches it as it would without
   tdb.  tdb outlives SIGINT and passes SIGTERM and SIGHUP on;
4. once the program has exited, waits up to ``CHILD_FLUSH_WAIT`` seconds
   for its traced children to finish writing;
5. assembles ``BASE.jsonl`` and ``BASE.perfetto.json``, each written to a
   temp file and renamed into place, removes the parts directory only
   after both are written, and prints the summary.

This module runs inside tdb, not the traced program; it must work on
Python 3.11.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tdb.languages.python_attach import pid_exists
from tdb.trace.capture import PART_FILE, CaptureSummary, assemble, write_capture
from tdb.trace.contract import (
    CHILD_FLUSH_WAIT,
    DEFAULT_MAX_EVENTS,
    build_env,
    write_config,
)
from tdb.trace.perfetto import write_trace
from tdb.trace.spec import TraceSpec, unmatched_entries

MIN_PYTHON = (3, 12)
PROBE_TIMEOUT = 10  # seconds the interpreter version probe may take
_POLL = 0.05  # seconds between looks at children still writing
_TAIL = 4096  # bytes read from a part's end to find its exit record
_UNSAFE = re.compile(r"[^\w.-]")  # replaced in default output names
# Signals tdb passes on to the program (SIGINT reaches it from the terminal).
_FORWARDED = (signal.SIGTERM,) if os.name == "nt" else (signal.SIGTERM, signal.SIGHUP)

NO_PROCESS_WARNING = (
    "tdb: warning: no process was traced (is it Python 3.12+, "
    "started without -I/-E, with PYTHONPATH intact?)"
)


class LaunchError(Exception):
    """The program could not be started; tdb reports it and exits 2."""


@dataclass
class TraceRequest:
    mode: str  # "trace" or "discover"
    program: str | None  # a .py file or a command; None with -m
    module: str | None
    args: list[str]
    python: str | None  # --python/--pv as given
    cwd: str | None  # --cwd: the program's working directory
    name: str
    out_base: Path
    spec: TraceSpec | None = None
    discover_out: Path | None = None
    max_events: int = DEFAULT_MAX_EVENTS


def default_name(program: str | None, module: str | None) -> str:
    """The module name, else the program's file stem (``main`` for main.py)."""
    if module is not None:
        return module
    return Path(program).stem


def default_out_base(name: str, now: datetime) -> Path:
    """``NAME-YYYYmmdd-HHMMSS`` in the cwd, with NAME made safe as a file name."""
    return Path.cwd() / f"{_UNSAFE.sub('_', name)}-{now:%Y%m%d-%H%M%S}"


def is_py_program(program: str) -> bool:
    """A ``.py`` file runs under an interpreter; anything else runs as is."""
    return os.path.normcase(program).endswith(".py")


def _interpreter(req: TraceRequest) -> str:
    """--python made absolute against tdb's cwd (so --cwd cannot change
    what it names; a bare name is looked up on PATH), else tdb's own."""
    if req.python is None:
        return sys.executable
    if os.path.dirname(req.python):
        return os.path.abspath(req.python)
    return shutil.which(req.python) or os.path.abspath(req.python)


def _uses_interpreter(req: TraceRequest) -> bool:
    """Forms 1 (``.py`` program) and 2 (``-m``) run under an interpreter."""
    return req.module is not None or is_py_program(req.program or "")


def resolve_command(req: TraceRequest) -> list[str]:
    """The program's argv, its first element an absolute path."""
    if req.module is not None:
        return [_interpreter(req), "-m", req.module, *req.args]
    if req.program is None:
        raise LaunchError("no program or module to run")
    if is_py_program(req.program):
        return [_interpreter(req), os.path.abspath(req.program), *req.args]
    found = shutil.which(req.program)
    if found is None and os.path.exists(req.program):
        found = req.program
    if found is None:
        raise LaunchError(f"command not found: {req.program}")
    return [os.path.abspath(found), *req.args]


def probe_version(interpreter: str) -> tuple[int, int]:
    """``(major, minor)`` of a Python interpreter, asked in isolated mode."""
    try:
        done = subprocess.run(
            [interpreter, "-I", "-c", "import sys; print(*sys.version_info[:2])"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise LaunchError(
            f"{interpreter} did not report its version within {PROBE_TIMEOUT} s"
        ) from None
    except OSError as exc:
        raise LaunchError(f"cannot run {interpreter}: {exc}") from None
    try:
        major, minor = map(int, done.stdout.split())
    except ValueError:
        last = done.stderr.strip().splitlines()[-1:]
        raise LaunchError(
            f"cannot tell the Python version of {interpreter}"
            + (f": {last[0]}" if last else "")
        ) from None
    return major, minor


def run(req: TraceRequest) -> int:
    """Run the traced program to its end and write the outputs.

    Returns tdb's exit code: the program's (128 + N for its death by signal
    N), or 1 if an output could not be written.  Raises LaunchError when
    the program cannot be started.
    """
    if req.mode == "discover":
        raise NotImplementedError("--trace-discover")
    cmd = resolve_command(req)
    if _uses_interpreter(req):
        version = probe_version(cmd[0])
        if version < MIN_PYTHON:
            raise LaunchError(
                f"--trace needs Python 3.12 or newer; {cmd[0]} is "
                f"{version[0]}.{version[1]}"
            )
    parts_dir = Path(f"{req.out_base}.parts")
    run_info = {
        "name": req.name,
        "tdb": _tdb_version(),
        "command": cmd,
        "cwd": os.path.abspath(req.cwd or os.getcwd()),
        "spec": None if req.spec is None else str(req.spec.path),
        "host": socket.gethostname(),
    }
    _create_parts_dir(parts_dir, req, run_info)
    proc = _supervise(cmd, req, parts_dir)
    code, main_signal = _exit_status(proc.returncode)
    return _write_outputs(req, parts_dir, run_info, code, proc.pid, main_signal)


def _tdb_version() -> str:
    from tdb import __version__

    return __version__


def _create_parts_dir(parts_dir: Path, req: TraceRequest, run_info: dict) -> None:
    try:
        parts_dir.mkdir()
    except FileExistsError:
        raise LaunchError(
            f"{parts_dir} already exists (left by an earlier run?); "
            "merge it with --trace-merge or remove it"
        ) from None
    except OSError as exc:
        raise LaunchError(f"cannot create {parts_dir}: {exc}") from None
    try:
        (parts_dir / "run.json").write_text(
            json.dumps(run_info, indent=2) + "\n", encoding="utf-8"
        )
        write_config(
            parts_dir / "config.json",
            mode=req.mode,
            parts_dir=parts_dir,
            targets=[] if req.spec is None else req.spec.agent_targets(),
            max_events=req.max_events,
        )
    except OSError as exc:
        shutil.rmtree(parts_dir, ignore_errors=True)
        raise LaunchError(f"cannot write to {parts_dir}: {exc}") from None


def _supervise(cmd: list[str], req: TraceRequest, parts_dir: Path) -> subprocess.Popen:
    """Run the program to its end, then give its children time to flush.

    The program shares tdb's terminal (no pipes) and process group.  While
    it runs, SIGINT only makes tdb say it is waiting (the program gets the
    terminal's SIGINT itself), and SIGTERM/SIGHUP are passed on.  The
    handlers are installed before the program starts, so no signal is
    lost, and the previous ones are restored at the end.
    """
    proc: subprocess.Popen | None = None
    pending: list[int] = []  # forwarded signals that came before the start
    told = False

    def on_sigint(signum, frame):
        nonlocal told
        if not told:
            told = True
            print(f"tdb: waiting for {req.name} to exit", file=sys.stderr, flush=True)

    def forward(signum, frame):
        if proc is None:
            pending.append(signum)
        elif os.name == "nt":
            proc.terminate()
        else:
            proc.send_signal(signum)  # a no-op once the program is reaped

    previous: dict[int, object] = {}
    try:
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, on_sigint)
        for sig in _FORWARDED:
            previous[sig] = signal.signal(sig, forward)
        env = build_env(
            os.environ, config_path=parts_dir / "config.json", parts_dir=parts_dir
        )
        try:
            # close_fds=False: descriptors tdb was handed (`3>log`, a
            # supervisor's socket) reach the program as they would without
            # tdb.  tdb's own are non-inheritable (PEP 446).
            proc = subprocess.Popen(cmd, cwd=req.cwd, env=env, close_fds=False)
        except OSError as exc:
            shutil.rmtree(parts_dir, ignore_errors=True)
            raise LaunchError(f"cannot run {cmd[0]}: {exc}") from None
        for sig in pending:
            forward(sig, None)
        proc.wait()
        _wait_for_children(parts_dir)
    finally:
        for sig, handler in previous.items():
            # None: the old handler was not set from Python; SIG_DFL is
            # the closest it can be restored to.
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)
    return proc


def _part_pid(name: str) -> int:
    """The pid in a part file name: ``<pid>.jsonl`` or ``<pid>-<n>.jsonl``."""
    return int(re.match(r"\d+", name).group())


def _has_exit(path: Path) -> bool:
    """True if the part's last record is its ``exit`` (written last)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _TAIL))
            lines = f.read().splitlines()
        rec = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return False
    return isinstance(rec, dict) and rec.get("k") == "exit"


def _unflushed(parts_dir: Path, done: set[str]) -> bool:
    """True while a part has no exit record and its pid is alive.

    ``done`` collects the parts already found finished, so each is read
    at most until then.  A pre-exec part (``fork`` + ``exec`` of another
    traced program) never gets an exit record while its pid runs on as
    the new program, so it holds the wait to its timeout.
    """
    try:
        names = os.listdir(parts_dir)
    except OSError:
        return False
    waiting = False
    for name in names:
        if name in done or not PART_FILE.match(name):
            continue
        if not pid_exists(_part_pid(name)) or _has_exit(parts_dir / name):
            done.add(name)
        else:
            waiting = True
    return waiting


def _wait_for_children(parts_dir: Path, timeout: float = CHILD_FLUSH_WAIT) -> None:
    """Wait up to ``timeout`` s for every traced process to finish writing."""
    deadline = time.monotonic() + timeout
    done: set[str] = set()
    while _unflushed(parts_dir, done) and time.monotonic() < deadline:
        time.sleep(_POLL)


def _exit_status(returncode: int) -> tuple[int, str | None]:
    """tdb's exit code for the program's returncode (128 + N for death by
    signal N), and that signal's name."""
    if returncode >= 0:
        return returncode, None
    n = -returncode
    try:
        name = signal.Signals(n).name
    except ValueError:  # e.g. a real-time signal
        name = str(n)
    return 128 + n, name


def _write_atomic(path: Path, write: Callable[[Path], None]) -> None:
    """Have ``write`` fill a temp file next to ``path``, then rename it into
    place, so a failure never leaves a partial ``path``."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_outputs(
    req: TraceRequest,
    parts_dir: Path,
    run_info: dict,
    code: int,
    main_pid: int,
    main_signal: str | None,
) -> int:
    try:
        capture, summary = assemble(
            parts_dir,
            header={**run_info, "exit_code": code},
            main_pid=main_pid,
            main_signal=main_signal,
        )
    except OSError as exc:
        print(f"tdb: error: cannot read {parts_dir}: {exc}", file=sys.stderr)
        return 1
    jsonl = Path(f"{req.out_base}.jsonl")
    perfetto = Path(f"{req.out_base}.perfetto.json")
    for path, write in (
        (jsonl, lambda p: write_capture(capture, p)),
        (perfetto, lambda p: write_trace([capture], p)),
    ):
        try:
            _write_atomic(path, write)
        except OSError as exc:
            print(
                f"tdb: error: could not write {path}: {exc}; events kept in {parts_dir}",
                file=sys.stderr,
            )
            return 1
    never = [] if req.spec is None else unmatched_entries(req.spec, summary.called)
    lines = _summary_lines(req.name, summary, never, jsonl, perfetto)
    try:
        shutil.rmtree(parts_dir)
    except OSError as exc:
        lines.append(f"tdb: warning: could not remove {parts_dir}: {exc}")
    print("\n".join(lines), file=sys.stderr)
    return code


def _counted(n: int, one: str, many: str, spec: str = "") -> str:
    return f"{n:{spec}} {one if n == 1 else many}"


def _summary_lines(
    name: str,
    summary: CaptureSummary,
    never_called: list[str],
    jsonl: Path,
    perfetto: Path,
) -> list[str]:
    """The end-of-run summary, one string per stderr line."""
    s = summary
    counts = ", ".join(
        [
            _counted(s.processes, "process", "processes"),
            _counted(s.threads, "thread", "threads"),
            _counted(s.tasks, "task", "tasks"),
            _counted(s.calls, "call", "calls", ","),
        ]
    )
    lines = [
        f'tdb: trace "{name}": {counts}',
        f"tdb: wrote {jsonl}",
        f"tdb: wrote {perfetto}  (open in https://ui.perfetto.dev)",
    ]
    for m in s.skipped:
        argv0 = (m.get("argv") or ["?"])[0]
        lines.append(
            f"tdb: warning: skipped {argv0} (pid {m.get('pid')}): {m.get('reason')}"
        )
    for m in s.agent_errors:
        lines.append(
            f"tdb: warning: tracer error in pid {m.get('pid')}: {m.get('error')}"
        )
    for pid in s.abrupt:
        lines.append(
            f"tdb: warning: pid {pid} ended abruptly; its last events may be missing"
        )
    lines += [f"tdb: warning: {note}" for note in s.notes]
    lines += [f"tdb: never called: {entry}" for entry in never_called]
    if not s.processes:
        lines.append(NO_PROCESS_WARNING)
    return lines
