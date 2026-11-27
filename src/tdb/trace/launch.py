"""Run a program under the trace agent and turn its parts into outputs.

``tdb --trace SPEC PROGRAM`` (``cli._run_trace``) builds a
:class:`TraceRequest` and calls :func:`run`, which:

1. resolves what to run: a ``.py`` program or ``-m MODULE`` under an
   interpreter (checked to be Python 3.12 or newer), or any other command,
   run as is (the tracer loads in whatever Python it starts);
2. creates ``BASE.parts/`` holding ``run.json`` (the capture header fields
   known at launch) and ``config.json`` (the agent config);
3. starts the program with the agent's environment on tdb's own terminal
   and in tdb's process group, so Ctrl-C and Ctrl-\\ reach it as they would
   without tdb.  tdb outlives SIGINT and SIGQUIT (SIGBREAK on Windows) and
   passes SIGTERM and SIGHUP on; a signal tdb inherited as ignored stays
   ignored, for tdb and the program;
4. once the program has exited, waits up to ``CHILD_FLUSH_WAIT`` seconds
   for its traced children to finish writing;
5. assembles ``BASE.jsonl`` and ``BASE.perfetto.json``, each written to a
   temp file and renamed into place, removes the parts directory only
   after both are written, and prints the summary.

``tdb --trace-discover OUT.yaml PROGRAM`` takes the same path in discover
mode, with ``OUT.yaml.parts/`` as the parts directory, and in step 5
writes the spec of the functions the program called to OUT.yaml (see
:mod:`tdb.trace.discover`).

The CLI then ends with :func:`exit_as`: the program's exit code, or death
by the program's own signal, so a shell sees what it would without tdb.

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from tdb import __version__
from tdb.languages.python_attach import pid_exists
from tdb.trace.capture import PART_FILE, CaptureSummary, assemble, write_capture
from tdb.trace.contract import (
    CHILD_FLUSH_WAIT,
    DEFAULT_MAX_EVENTS,
    build_env,
    write_config,
)
from tdb.trace.perfetto import write_trace
from tdb.trace.spec import TraceSpec, norm_path, unmatched_entries

MIN_PYTHON = (3, 12)
PROBE_TIMEOUT = 10  # seconds the interpreter version probe may take
_POLL = 0.05  # seconds between looks at children still writing
_TAIL = 4096  # bytes read from a part's end to find its exit record
_UNSAFE = re.compile(r"[^\w.-]")  # replaced in default output names
_SIGHUP = getattr(signal, "SIGHUP", None)  # POSIX only
# Signals tdb survives while the program runs: the terminal sends them to
# the program too (Ctrl-C; Ctrl-\ or Ctrl-Break), so tdb does not.
_SURVIVED = (
    (signal.SIGINT, signal.SIGBREAK)
    if os.name == "nt"
    else (signal.SIGINT, signal.SIGQUIT)
)
# Signals tdb passes on to the program.
_FORWARDED = (signal.SIGTERM,) if os.name == "nt" else (signal.SIGTERM, _SIGHUP)
# A program killed by one of these dumps core; tdb then exits 128 + N
# rather than dumping a core of its own as a false trail.
_CORE_SIGNALS = frozenset(
    getattr(signal, name)
    for name in (
        "SIGQUIT",
        "SIGSEGV",
        "SIGABRT",
        "SIGBUS",
        "SIGFPE",
        "SIGILL",
        "SIGTRAP",
        "SIGSYS",
        "SIGXCPU",
        "SIGXFSZ",
    )
    if hasattr(signal, name)
)
# run()'s status after Ctrl-C outside the program's lifetime.
_INTERRUPTED = 128 + signal.SIGINT if os.name == "nt" else -signal.SIGINT

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
    """The program's argv.

    The program's path is kept as typed, so it sees the ``sys.argv[0]``
    (and ``__file__``) it would without tdb.  Only with ``--cwd``, which
    would change what a relative path names, is it made absolute, and
    never resolved: a symlinked script stays the symlink.
    """
    if req.module is not None:
        return [_interpreter(req), "-m", req.module, *req.args]
    if req.program is None:
        raise LaunchError("no program or module to run")
    if is_py_program(req.program):
        program = req.program if req.cwd is None else os.path.abspath(req.program)
        return [_interpreter(req), program, *req.args]
    found = shutil.which(req.program)
    if found is None and os.path.exists(req.program):
        found = req.program
    if found is None:
        raise LaunchError(f"command not found: {req.program}")
    # A bare name (no directory part) would be looked up on PATH again, so
    # one found only as a file in the cwd is run by its absolute path.
    if req.cwd is not None or not os.path.dirname(found):
        found = os.path.abspath(found)
    return [found, *req.args]


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
    """Run the traced program to its end and write the outputs (in
    discover mode, the spec).

    Returns tdb's status for :func:`exit_as`: the program's exit code;
    on POSIX ``-N`` when it died by signal N (``128 + N`` for one that
    dumps core, and always on Windows); 1 if an output could not be
    written; ``-SIGINT`` (Windows: 130) after a Ctrl-C outside the
    program's lifetime.  Raises LaunchError when the program cannot be
    started.
    """
    discover = req.mode == "discover"
    if discover:
        out = req.discover_out
        parts_dir = out.with_name(out.name + ".parts")
    else:
        parts_dir = Path(f"{req.out_base}.parts")
    created = False
    try:
        cmd = resolve_command(req)
        if _uses_interpreter(req):
            version = probe_version(cmd[0])
            if version < MIN_PYTHON:
                raise LaunchError(
                    f"{_mode_flag(req)} needs Python 3.12 or newer; {cmd[0]} is "
                    f"{version[0]}.{version[1]}"
                )
        run_info = {
            "name": req.name,
            "tdb": __version__,
            "command": cmd,
            "cwd": os.path.abspath(req.cwd or os.getcwd()),
            "spec": None if req.spec is None else str(req.spec.path),
            "host": socket.gethostname(),
        }
        roots = _user_roots(req, cmd) if discover else []
        _create_parts_dir(parts_dir, req, run_info, user_roots=roots)
        created = True
        started, start = datetime.now(), time.monotonic()
        proc = _supervise(cmd, req, parts_dir)
        duration = time.monotonic() - start
    except KeyboardInterrupt:  # before tdb's handler was in place: nothing ran
        if created:
            _remove_unused(parts_dir)
        return _INTERRUPTED
    code, main_signal = _exit_status(proc.returncode)
    try:
        if discover:
            ended = (code, proc.pid, main_signal)
            written = _write_discovery(
                req, parts_dir, run_info, ended, started, duration
            )
        else:
            written = _write_outputs(
                req, parts_dir, run_info, code, proc.pid, main_signal
            )
    except KeyboardInterrupt:
        return _INTERRUPTED
    return _end_status(proc.returncode) if written else 1


def _mode_flag(req: TraceRequest) -> str:
    return "--trace-discover" if req.mode == "discover" else "--trace"


def _user_roots(req: TraceRequest, cmd: list[str]) -> list[str]:
    """Discover mode (Ruling 26): paths whose code is the program's own
    even under a library directory, so a program installed in
    site-packages is still discovered.  A ``.py`` program gives its file
    and its directory (the agent lets a library directory as deep or
    deeper win, so a program right in site-packages does not take all of
    it, nor a project directory its .venv); a command gives its file, if
    that is a Python script; ``-m`` gives none (the module is found only
    when the program imports it)."""
    if req.module is not None:
        return []
    if is_py_program(req.program):
        program = norm_path(cmd[1])
        return [program, os.path.dirname(program)]
    command = norm_path(cmd[0])
    return [command] if _is_python_script(command) else []


def _is_python_script(path: str) -> bool:
    """A ``.py`` file, or one whose ``#!`` line runs a Python."""
    if is_py_program(path):
        return True
    try:
        with open(path, "rb") as f:
            first = f.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def say(text: str) -> None:
    """Print one tdb message on stderr.

    A stderr that is gone (the reader of a ``| tee`` killed by the same
    Ctrl-C, a hung-up terminal) must stop neither tdb's supervision nor
    change its exit status, which CPython sets to 120 when flushing stderr
    fails at exit.  So after a failed write tdb's stderr descriptor is
    pointed at the null device, where the stuck bytes and every later
    message go.  The program's own stderr is a separate descriptor.
    """
    try:
        print(text, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        with contextlib.suppress(OSError, ValueError):
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, sys.stderr.fileno())
            finally:
                os.close(null)
        with contextlib.suppress(OSError, ValueError):
            sys.stderr.flush()


def _end_status(returncode: int) -> int:
    """tdb's status for the program's returncode: its exit code, or (POSIX)
    ``-N`` to die by the same signal N, so that a shell sees what it would
    without tdb (a loop stops at Ctrl-C).  A signal that dumps core gives
    exit code 128 + N instead."""
    if returncode >= 0 or os.name == "nt" or -returncode in _CORE_SIGNALS:
        return _exit_status(returncode)[0]
    return returncode


def exit_as(status: int) -> NoReturn:
    """End tdb with a :func:`run` status: exit with it, or for ``-N`` die
    by signal N, as CPython does after an uncaught KeyboardInterrupt."""
    if status < 0:
        sig = -status
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(OSError, ValueError):
                stream.flush()
        with contextlib.suppress(OSError, ValueError):  # SIGKILL: no handler
            signal.signal(sig, signal.SIG_DFL)
        with contextlib.suppress(OSError):
            os.kill(os.getpid(), sig)
        status = 128 + sig  # still here: the signal is blocked
    sys.exit(status)


def _remove_unused(parts_dir: Path) -> None:
    """Remove a parts directory no traced process wrote to."""
    with contextlib.suppress(OSError):
        if set(os.listdir(parts_dir)) <= {"run.json", "config.json"}:
            shutil.rmtree(parts_dir)


def _create_parts_dir(
    parts_dir: Path, req: TraceRequest, run_info: dict, *, user_roots: list[str]
) -> None:
    try:
        parts_dir.mkdir()
    except FileExistsError:
        if req.mode == "discover":  # counts, of no use to --trace-merge
            what = "remove it"
        else:
            what = "merge it with --trace-merge or remove it"
        raise LaunchError(
            f"{parts_dir} already exists (left by an earlier run?); {what}"
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
            user_roots=user_roots,
        )
    except BaseException as exc:  # a Ctrl-C too: leave nothing behind
        shutil.rmtree(parts_dir, ignore_errors=True)
        if isinstance(exc, OSError):
            raise LaunchError(f"cannot write to {parts_dir}: {exc}") from None
        raise


def _supervise(cmd: list[str], req: TraceRequest, parts_dir: Path) -> subprocess.Popen:
    """Run the program to its end, then give its children time to flush.

    The program shares tdb's terminal (no pipes) and process group.  While
    it runs, SIGINT and SIGQUIT (SIGBREAK) only make tdb say it is waiting:
    the terminal sends them to the program as well.  SIGTERM and SIGHUP
    are passed on, except a SIGHUP that its shell sent the program too
    (below).  A signal tdb inherited as ignored (``nohup``, a background
    job) is left ignored, so the program inherits that too.  The handlers
    are installed before the program starts, and a signal that comes
    before it does is sent to it right after; the previous handlers are
    restored at the end.

    A terminal hangup SIGHUPs only the session leader.  When that is a
    shell (bash, ssh, tmux), it passes SIGHUP on to its jobs, the program
    among them, so a SIGHUP that comes once tdb's terminal is gone is not
    forwarded.  When tdb is the session leader itself (``xterm -e tdb``),
    the kernel signalled tdb alone, and tdb forwards it.
    """
    proc: subprocess.Popen | None = None
    pending: list[int] = []  # signals that came before the program started
    told = False
    shell_relays_hangup = _has_tty() and os.getsid(0) != os.getpid()

    def survive(signum, frame):
        nonlocal told
        if proc is None:
            pending.append(signum)  # the terminal could not reach it yet
        if not told:
            told = True
            say(f"tdb: waiting for {req.name} to exit")

    def forward(signum, frame):
        if signum == _SIGHUP and shell_relays_hangup and not _has_tty():
            return  # a terminal hangup: the shell signalled the program too
        if proc is None:
            pending.append(signum)
        else:
            _send(proc, signum)

    previous: dict[int, object] = {}
    try:
        for sigs, handler in ((_SURVIVED, survive), (_FORWARDED, forward)):
            for sig in sigs:
                if signal.getsignal(sig) is not signal.SIG_IGN:
                    previous[sig] = signal.signal(sig, handler)
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
            _send(proc, sig)
        proc.wait()
        _wait_for_children(parts_dir)
    finally:
        for sig, handler in previous.items():
            # None: the old handler was not set from Python; SIG_DFL is
            # the closest it can be restored to.
            signal.signal(sig, signal.SIG_DFL if handler is None else handler)
    return proc


def _send(proc: subprocess.Popen, sig: int) -> None:
    if os.name == "nt":
        proc.terminate()
    else:
        proc.send_signal(sig)  # a no-op once the program is reaped


def _has_tty() -> bool:
    """True while tdb has a controlling terminal (POSIX): after a hangup,
    opening /dev/tty fails."""
    if os.name == "nt":
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def _part_pid(name: str) -> int:
    """The pid in a part file name: ``<pid>.jsonl`` or ``<pid>-<n>.jsonl``."""
    return int(re.match(r"\d+", name).group())


def _has_exit(path: Path) -> bool:
    """True if the part's last record is its ``exit`` (written last), or
    the ``exec`` record of a part that ended at an exec."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - _TAIL))
            lines = f.read().splitlines()
        rec = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return False
    return isinstance(rec, dict) and rec.get("k") in ("exit", "exec")


def _unflushed(parts_dir: Path, done: set[str]) -> bool:
    """True while a part has no exit record and its pid is alive.

    ``done`` collects the parts already found finished, so each is read
    at most until then.  A part that ended at an exec is finished, though
    its pid runs on as the new program.
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


def write_atomic(path: Path, write: Callable[[Path], None]) -> None:
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
) -> bool:
    """Write both outputs, remove the parts, print the summary.  False if
    an output could not be written (the parts are then kept)."""
    jsonl = Path(f"{req.out_base}.jsonl")
    perfetto = Path(f"{req.out_base}.perfetto.json")
    try:
        try:
            capture, summary = assemble(
                parts_dir,
                header={**run_info, "exit_code": code},
                main_pid=main_pid,
                main_signal=main_signal,
            )
        except OSError as exc:
            say(f"tdb: error: cannot read {parts_dir}: {exc}")
            return False
        for path, write in (
            (jsonl, lambda p: write_capture(capture, p)),
            (perfetto, lambda p: write_trace([capture], p)),
        ):
            try:
                write_atomic(path, write)
            except OSError as exc:
                say(
                    f"tdb: error: could not write {path}: {exc}; "
                    f"events kept in {parts_dir}"
                )
                return False
    except KeyboardInterrupt:
        say(f"tdb: interrupted; events kept in {parts_dir}")
        raise
    never = [] if req.spec is None else unmatched_entries(req.spec, summary.called)
    lines = _summary_lines(req.name, summary, never, jsonl, perfetto)
    try:
        shutil.rmtree(parts_dir)
    except OSError as exc:
        lines.append(f"tdb: warning: could not remove {parts_dir}: {exc}")
    say("\n".join(lines))
    return True


def _write_discovery(
    req: TraceRequest,
    parts_dir: Path,
    run_info: dict,
    ended: tuple[int, int, str | None],
    started: datetime,
    duration_s: float,
) -> bool:
    """Write the discovered spec, remove the parts, print the summary with
    --trace's warnings (what the counts may be missing).  False if the
    parts could not be read or the spec written (the parts are then kept).
    ``ended`` is the main process's (exit code, pid, signal name)."""
    from tdb.trace import discover  # it imports this module

    code, main_pid, main_signal = ended
    out = req.discover_out
    desc = req.program if req.module is None else f"-m {req.module}"
    try:
        try:
            _, summary = assemble(
                parts_dir,
                header={**run_info, "exit_code": code},
                main_pid=main_pid,
                main_signal=main_signal,
            )
            counts = discover.read_counts(parts_dir)
        except OSError as exc:
            say(f"tdb: error: cannot read {parts_dir}: {exc}")
            return False
        try:
            found = discover.write_spec(
                counts, out, program_desc=desc, started=started, duration_s=duration_s
            )
        except OSError as exc:
            say(f"tdb: error: could not write {out}: {exc}; events kept in {parts_dir}")
            return False
    except KeyboardInterrupt:
        say(f"tdb: interrupted; events kept in {parts_dir}")
        raise
    functions = _counted(found.functions, "function", "functions", ",")
    lines = [f"tdb: wrote {out} ({functions})"]
    if found.omitted:
        omitted = _counted(found.omitted, "function", "functions", ",")
        lines.append(f"tdb: note: {omitted} omitted (not found in their source files)")
    lines += _warning_lines(summary)
    try:
        shutil.rmtree(parts_dir)
    except OSError as exc:
        lines.append(f"tdb: warning: could not remove {parts_dir}: {exc}")
    say("\n".join(lines))
    return True


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
    return [
        f'tdb: trace "{name}": {counts}',
        f"tdb: wrote {jsonl}",
        f"tdb: wrote {perfetto}  (open in https://ui.perfetto.dev)",
        *_warning_lines(s, never_called),
    ]


def _warning_lines(
    summary: CaptureSummary, never_called: Sequence[str] = ()
) -> list[str]:
    """What the outputs (a capture, or a discovered spec) may be missing,
    one string per stderr line; shared by --trace and --trace-discover."""
    s = summary
    lines = []
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
    for name, pid, sig in s.killed:
        lines.append(
            f"tdb: warning: {name} (pid {pid}) was killed by {sig}; "
            "its last events may be missing"
        )
    lines += [f"tdb: warning: {note}" for note in s.notes]
    lines += [f"tdb: never called: {entry}" for entry in never_called]
    if not s.processes:
        lines.append(NO_PROCESS_WARNING)
    return lines
