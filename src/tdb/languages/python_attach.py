"""Attach to a running Python process by pid (`tdb -a PID`).

The target needs no preparation. tdb's adapter waits for a connection, and
code injected into the target dials back to it, so quitting tdb leaves no
open port or helper process behind. This module holds everything specific
to reaching the target; it imports no controller or UI code.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import IO

from tdb import _timeouts
from tdb.languages import _python_gdb_helper
from tdb.languages.base import LanguageNotSupportedError

MINIMUM_VERSION = (3, 11)

_PYTHON_NAME = re.compile(r"^(python|pypy)(\d+(\.\d+)?t?)?(\.exe)?$", re.IGNORECASE)

_PROC = "/proc"
_TCP_ESTABLISHED = "01"
_PROBE_TIMEOUT = 10.0
_PROBE_CODE = (
    "import sys;"
    "print(sys.implementation.name, sys.version_info[0], sys.version_info[1],"
    " hasattr(sys, 'remote_exec'))"
)


# Remote-exec helpers finish at once: they only queue the script.
_REMOTE_EXEC_HELPER_TIMEOUT = 10.0
# gdb has to load the target's symbols first. It takes a fraction of a second
# for most programs.
_GDB_QUEUE_TIMEOUT = 10.0
_GDB_QUEUED = re.compile(r"^\$1 = 0$", re.MULTILINE)
# How often a wait re-checks the target and the helper.
_POLL = 0.25
# A helper that exits cleanly and never ran the code found no Python to run
# in. The grace covers a connection already on its way.
_HELPER_EXIT_GRACE = 2.0
_MAX_REASON_LENGTH = 300

_SCRIPT_NAME = "attach.py"
_MARKER_NAME = "marker"
_CLAIMED_NAME = "claimed"
_RAN_NAME = "ran"

METHOD_REMOTE_EXEC = "sys.remote_exec"
METHOD_GDB_QUEUE = "gdb pending call"
METHOD_DEBUGPY = "debugpy injection"

class PidAttachError(Exception):
    """Injection failed; `str()` is the message shown to the user."""


@dataclass(frozen=True)
class TargetInfo:
    """What tdb knows about the process it is attaching to.

    `interpreter` is an executable of the target's own Python, or None
    when it can't be found or safely run; `sys.remote_exec` needs a
    caller of the same version, so None rules that method out.
    """

    pid: int
    interpreter: str | None
    has_remote_exec: bool


def is_python_executable(path: str) -> bool:
    """True if `path` is named like a Python interpreter."""
    return _PYTHON_NAME.match(os.path.basename(path)) is not None


def pid_exists(pid: int) -> bool:
    """True if `pid` is running, or on Windows, where this can't tell:
    `os.kill(pid, 0)` there terminates the process."""
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline(pid: str) -> str:
    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _socket_inodes(pid: str) -> set[str]:
    try:
        fds = os.listdir(f"{_PROC}/{pid}/fd")
    except OSError:
        return set()
    inodes = set()
    for fd in fds:
        try:
            link = os.readlink(f"{_PROC}/{pid}/fd/{fd}")
        except OSError:
            continue
        if link.startswith("socket:["):
            inodes.add(link[8:-1])
    return inodes


def _tcp_connections() -> list[tuple[str, str, str]]:
    """(local port, remote port, inode) of each established connection."""
    connections = []
    for table in ("tcp", "tcp6"):
        try:
            with open(f"{_PROC}/net/{table}") as f:
                rows = f.readlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if cols[3] == _TCP_ESTABLISHED:
                local, remote = cols[1], cols[2]
                connections.append(
                    (local.rpartition(":")[2], remote.rpartition(":")[2], cols[9])
                )
    return connections


def _connected_to_adapter(pid: int) -> bool:
    """True if `pid` holds a connection to a debugpy adapter process."""
    inodes = _socket_inodes(str(pid))
    connections = _tcp_connections()
    peer_ports = {remote for _, remote, inode in connections if inode in inodes}
    if not peer_ports:
        return False
    peer_inodes = {inode for local, _, inode in connections if local in peer_ports}
    try:
        pids = [name for name in os.listdir(_PROC) if name.isdigit()]
    except OSError:
        return False
    return any(
        ("debugpy/adapter" in cmdline or "debugpy.adapter" in cmdline)
        and _socket_inodes(other) & peer_inodes
        for other, cmdline in ((p, _cmdline(p)) for p in pids)
    )


def already_debugged(pid: int) -> bool:
    """True if debugpy is already running this process, as far as Linux
    can show. Injecting there would tear down that session.

    A launched program has debugpy's launcher as its parent. After
    debugpy.listen() the adapter is daemonized, so it isn't a child; the
    connection between them is the link. A target tdb detached from has
    neither, and can be attached again.
    """
    if sys.platform != "linux":
        return False
    try:
        with open(f"{_PROC}/{pid}/stat") as f:
            parent = f.read().rpartition(")")[2].split()[1]
    except (OSError, IndexError):
        return False
    return "debugpy/launcher" in _cmdline(parent) or _connected_to_adapter(pid)


def uses_musl(pid: int) -> bool:
    """True if `pid` runs on musl libc (Alpine). debugpy's injection library
    is built for glibc and can't load there."""
    try:
        with open(f"{_PROC}/{pid}/maps") as f:
            return any("/ld-musl-" in line or "/libc.musl-" in line for line in f)
    except OSError:
        return False


def _exe_of_pid(pid: int) -> str | None:
    if sys.platform != "linux":
        return None
    return os.path.realpath(f"/proc/{pid}/exe")


def probe_target(pid: int, python: str | None = None) -> TargetInfo:
    """Find the target's interpreter and what it supports.

    `python` (the --python flag) overrides the lookup. A binary not named
    like Python is never run: it may embed an interpreter (uWSGI), and
    these arguments mean nothing to it. Such targets are still attachable
    through debugpy's injection, so they come back as unknown, not as an
    error.

    Raises LanguageNotSupportedError for a missing pid or a known
    unsupported interpreter.
    """
    if not pid_exists(pid):
        raise LanguageNotSupportedError(f"no process with pid {pid}")
    if already_debugged(pid):
        raise LanguageNotSupportedError(
            f"pid {pid} is already being debugged with debugpy; attaching "
            "would end that session. If it called debugpy.listen(), connect "
            "with `tdb -r PORT` instead"
        )
    unknown = TargetInfo(pid, None, False)
    path = python or _exe_of_pid(pid)
    if path is None or not os.path.isfile(path) or not is_python_executable(path):
        return unknown
    try:
        result = subprocess.run(
            [path, "-I", "-c", _PROBE_CODE],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
        name, major, minor, remote_exec = result.stdout.split()
        version = (int(major), int(minor))
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown
    if name != "cpython":
        raise LanguageNotSupportedError(
            f"pid {pid} runs {name}; attaching by pid needs CPython"
        )
    if version < MINIMUM_VERSION:
        raise LanguageNotSupportedError(
            f"pid {pid} runs Python {major}.{minor}; attaching by pid needs "
            f"Python {MINIMUM_VERSION[0]}.{MINIMUM_VERSION[1]} or newer"
        )
    return TargetInfo(pid, path, remote_exec == "True")


def free_port() -> int:
    """An unused localhost port for the adapter to wait on."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_remote_exec_script(
    server_dir: str, port: int, marker: str, claimed: str
) -> str:
    """Source of the script `sys.remote_exec` runs inside the target.

    A queued script can fire long after tdb gave up on it, when a blocked
    main thread wakes. Renaming `marker` is an atomic claim that tdb makes
    too: whichever side renames it first owns this attempt, and a script
    that loses only deletes its directory. Everything lives in one
    function so no names are left in the target. The switch interval goes
    back once `attach` returns, which is when the handshake ends.
    """
    return textwrap.dedent(
        f"""\
        def _tdb_attach():
            import os, shutil, sys
            marker = {marker!r}
            try:
                os.rename(marker, {claimed!r})
            except OSError:
                return
            finally:
                shutil.rmtree(os.path.dirname(marker), ignore_errors=True)
            interval = sys.getswitchinterval()
            sys.setswitchinterval({_python_gdb_helper.ATTACH_SWITCH_INTERVAL})
            try:
                sys.path.insert(0, {server_dir!r})
                try:
                    import attach_pid_injected
                finally:
                    del sys.path[0]
                attach_pid_injected.attach({{
                    "mode": "connect",
                    "address": ["127.0.0.1", {port!r}],
                    "wait_for_client": False,
                    "log_to": None,
                    "adapter_access_token": None,
                }})
            finally:
                sys.setswitchinterval(interval)
        try:
            _tdb_attach()
        finally:
            del _tdb_attach
        """
    )


def read_ptrace_scope() -> int | None:
    """Linux Yama's ptrace_scope, or None where there is no such setting."""
    try:
        with open("/proc/sys/kernel/yama/ptrace_scope") as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


def _permission_message(ptrace_scope: int | None) -> str:
    if ptrace_scope == 3:
        return (
            "the system forbids attaching to processes "
            "(kernel.yama.ptrace_scope=3); only a reboot can lower that setting"
        )
    if ptrace_scope is None:
        return "permission denied; run tdb as root"
    return (
        f"permission denied by kernel.yama.ptrace_scope={ptrace_scope}. Run "
        "`sudo sysctl kernel.yama.ptrace_scope=0`, or run tdb as root"
    )


def _last_useful_line(text: str) -> str:
    """The line of helper output most worth showing. debugpy echoes its gdb
    command, kilobytes of byte values, after the error."""
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith(("Running:", "PYDEVD_GDB_SCAN"))
    ]
    errors = [line for line in lines if "error" in line.lower()]
    best = (errors or lines or ["injection failed"])[-1]
    return best[:_MAX_REASON_LENGTH]


def classify_failure(
    *,
    inject_output: str,
    remote_exec_error: str,
    gdb_missing: bool,
    ptrace_scope: int | None,
    is_root: bool,
    musl: bool = False,
) -> str:
    """The user-facing reason injection failed.

    "PyRuntime address lookup failed" is also what a version mismatch
    gives, so on its own it only means permissions when Yama could be
    the cause.
    """
    blocked_by_yama = (
        "PyRuntime address lookup failed" in remote_exec_error
        and (ptrace_scope or 0) >= 1
        and not is_root
    )
    denied = "PermissionError" in remote_exec_error or blocked_by_yama
    if "ptrace" in inject_output.lower() or denied:
        return _permission_message(ptrace_scope)
    if musl:
        return (
            "this program uses musl libc, where debugpy's injection library "
            "can't load, so tdb can only reach a main thread that is running "
            "Python code"
        )
    if gdb_missing and "not enabled" in remote_exec_error:
        return (
            "remote debugging is disabled in the target "
            "(PYTHON_DISABLE_REMOTE_DEBUG or -X disable-remote-debug); "
            "install gdb to attach anyway"
        )
    if gdb_missing:
        return (
            "install gdb: it is needed for Python below 3.14 and for programs "
            "whose main thread is blocked"
        )
    if sys.platform != "win32" and any(c in sys.executable for c in "'\"\\"):
        return (
            f"debugpy's injection can't handle a quote or backslash in the path "
            f"tdb is installed under ({sys.executable})"
        )
    if 'No symbol "DoAttach"' in inject_output:
        return (
            "gdb reached the process, but it could not load debugpy's injection "
            "library. The target may not be a standard CPython build"
        )
    return _last_useful_line(inject_output or remote_exec_error)


def _remote_exec_argv(target: TargetInfo, script_path: str) -> list[str]:
    # Through the target's interpreter: caller and target versions must match.
    code = f"import sys; sys.remote_exec({target.pid}, {script_path!r})"
    return [target.interpreter or "", "-I", "-c", code]


def _gdb_queue_argv(pid: int, script_path: str) -> list[str]:
    """gdb asks the target to run the script, as `sys.remote_exec` would.

    Py_AddPendingCall takes no GIL, so this is safe on a main thread waiting
    for it. Calling into a thread stopped there is not: below glibc 2.41 the
    wait can't be re-entered, and the target deadlocks. gdb is gone before
    the script even starts.
    """
    script = _python_gdb_helper.as_bytes(script_path)
    code = f"exec(open({script}.decode()).read())"
    call = (
        "call (int)Py_AddPendingCall((int(*)(void*))PyRun_SimpleString,"
        f'(void*)strdup("{code}"))'
    )
    return ["gdb", "--nw", "--nh", "--nx", "--pid", str(pid), "--batch",
            f"--eval-command={call}"]


def _debugpy_argv(pid: int, port: int, ran_marker: str) -> list[str]:
    # -P: the cwd otherwise comes first, and a local debugpy/ would win.
    # lldb and DLL injection have no gdb to get out of, and are untested
    # with a thread, so they keep debugpy's plain entry point.
    if sys.platform == "linux":
        entry = [_python_gdb_helper.__file__, ran_marker]
    else:
        entry = ["-m", "debugpy"]
    return [
        sys.executable,
        "-P",
        *entry,
        "--connect",
        f"127.0.0.1:{port}",
        "--disable-sys-remote-exec",
        "--pid",
        str(pid),
    ]


async def _spawn_remote_exec(argv: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


def _spawn_detached(
    argv: list[str], output: IO[bytes], env: dict[str, str] | None = None
) -> subprocess.Popen:
    """Start the injection helper so that nothing can stop it.

    Its gdb runs a call inside the target and must see that through: a
    gdb killed mid-call leaves the target to return into nothing, and it
    segfaults. That call can last as long as native code holds the GIL,
    so the helper may well outlive tdb. So not asyncio (its transports
    kill their process on shutdown), and its own session, out of reach of
    the terminal's Ctrl-C.
    """
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=sys.platform != "win32",
    )


def _read_output(output: IO[bytes]) -> str:
    output.seek(0)
    return output.read().decode(errors="replace")


async def _wait_for_connection(
    pid: int,
    connected: asyncio.Event,
    until: float,
    helper: subprocess.Popen | None = None,
    ran_marker: str = "",
    quitting: asyncio.Event | None = None,
) -> bool:
    """True once the target connects. False at `until`, once `quitting`, if
    `helper` fails, or if it exits cleanly without having run the code
    (`ran_marker`). Raises if the target exits first."""
    helper_exited_at = None
    while not connected.is_set():
        if not pid_exists(pid):
            raise PidAttachError(f"cannot attach to pid {pid}: the process exited")
        if quitting is not None and quitting.is_set():
            return False
        now = time.monotonic()
        code = helper.poll() if helper is not None else None
        if code not in (None, 0):
            return False
        if code == 0 and not os.path.exists(ran_marker):
            helper_exited_at = helper_exited_at or now
            if now - helper_exited_at >= _HELPER_EXIT_GRACE:
                return False
        if now >= until:
            return False
        try:
            await asyncio.wait_for(connected.wait(), min(_POLL, until - now))
        except TimeoutError:
            pass
    return True


async def _wait_for_exit(helper: subprocess.Popen, until: float) -> None:
    while helper.poll() is None and time.monotonic() < until:
        await asyncio.sleep(0.05)


def _claim(marker: str, claimed: str) -> bool:
    try:
        os.rename(marker, claimed)
    except OSError:
        return False
    return True


async def _queue_with_remote_exec(
    target: TargetInfo, script_path: str
) -> tuple[bool, str]:
    proc = await _spawn_remote_exec(_remote_exec_argv(target, script_path))
    try:
        output, _ = await asyncio.wait_for(
            proc.communicate(), _REMOTE_EXEC_HELPER_TIMEOUT
        )
        return proc.returncode == 0, output.decode(errors="replace")
    finally:
        if proc.returncode is None:
            # Safe, unlike gdb: it only writes to the target.
            proc.kill()
            await proc.wait()


async def _queue_with_gdb(pid: int, script_path: str) -> tuple[bool, str]:
    # gdb's own embedded Python may choke on a PYTHONPATH meant for another.
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with tempfile.TemporaryFile() as output:
        gdb = _spawn_detached(_gdb_queue_argv(pid, script_path), output, env)
        await _wait_for_exit(gdb, time.monotonic() + _GDB_QUEUE_TIMEOUT)
        if gdb.poll() is None:
            raise TimeoutError
        text = _read_output(output)
    return gdb.returncode == 0 and _GDB_QUEUED.search(text) is not None, text


async def _try_queued(
    queue: Callable[[str], Awaitable[tuple[bool, str]]],
    pid: int,
    port: int,
    connected: asyncio.Event,
    deadline: float,
    quitting: asyncio.Event,
) -> tuple[bool, str]:
    """Have the main thread run the attach script. `queue` takes the script's
    path and returns (queued, output). Returns (connected, error text)."""
    from debugpy import server

    server_dir = os.path.dirname(server.__file__)
    workdir = tempfile.mkdtemp(prefix="tdb-attach-")
    marker = os.path.join(workdir, _MARKER_NAME)
    claimed = os.path.join(workdir, _CLAIMED_NAME)
    script_path = os.path.join(workdir, _SCRIPT_NAME)
    with open(marker, "w"):
        pass
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(build_remote_exec_script(server_dir, port, marker, claimed))

    try:
        queued, output = await queue(script_path)
        if not queued:
            shutil.rmtree(workdir, ignore_errors=True)
            return False, output
        wait_until = min(deadline, time.monotonic() + _timeouts.PID_ATTACH_REMOTE_EXEC)
        if await _wait_for_connection(pid, connected, wait_until, quitting=quitting):
            return True, ""
        if _claim(marker, claimed):
            return False, ""
        # The script fired just now: it owns the attempt.
        return await _wait_for_connection(pid, connected, deadline), ""
    except TimeoutError:
        return False, ""
    finally:
        if not connected.is_set():
            # Silences a script left queued in the target.
            _claim(marker, claimed)


async def inject(
    target: TargetInfo,
    port: int,
    connected: asyncio.Event,
    quitting: asyncio.Event | None = None,
) -> str:
    """Make the target connect to the adapter waiting on `port`.

    `connected` is set when the session starts. Injection blocks until the
    DAP handshake finishes, so this runs alongside it, not before it, and
    returns only once the helper is gone. `quitting` means give up on
    anything not yet started. Returns the method used; raises PidAttachError
    with a message for the user.
    """
    pid = target.pid
    quitting = quitting or asyncio.Event()
    deadline = time.monotonic() + _timeouts.PID_ATTACH_TOTAL
    remote_exec_error = ""
    if target.has_remote_exec:
        reached, remote_exec_error = await _try_queued(
            partial(_queue_with_remote_exec, target),
            pid,
            port,
            connected,
            deadline,
            quitting,
        )
        if reached:
            return METHOD_REMOTE_EXEC
    gdb_found = shutil.which("gdb") is not None
    # Not after a remote_exec that timed out: its script is queued already,
    # and whatever stopped that stops this.
    if (
        sys.platform == "linux"
        and gdb_found
        and not quitting.is_set()
        and (remote_exec_error or not target.has_remote_exec)
    ):
        reached, _ = await _try_queued(
            partial(_queue_with_gdb, pid), pid, port, connected, deadline, quitting
        )
        if reached:
            return METHOD_GDB_QUEUE

    if quitting.is_set():
        raise PidAttachError(f"cannot attach to pid {pid}: quitting")

    def failure(
        inject_output: str = "", gdb_missing: bool = False, musl: bool = False
    ) -> PidAttachError:
        reason = classify_failure(
            inject_output=inject_output,
            remote_exec_error=remote_exec_error,
            gdb_missing=gdb_missing,
            ptrace_scope=read_ptrace_scope(),
            is_root=hasattr(os, "geteuid") and os.geteuid() == 0,
            musl=musl,
        )
        return PidAttachError(f"cannot attach to pid {pid}: {reason}")

    if sys.platform == "linux":
        if uses_musl(pid):
            raise failure(musl=True)
        if not gdb_found:
            raise failure(gdb_missing=True)

    with (
        tempfile.TemporaryFile() as output,
        tempfile.TemporaryDirectory(prefix="tdb-attach-") as workdir,
    ):
        ran_marker = os.path.join(workdir, _RAN_NAME)
        helper = _spawn_detached(_debugpy_argv(pid, port, ran_marker), output)
        if await _wait_for_connection(pid, connected, deadline, helper, ran_marker):
            # lldb and DLL injection stay in the target until the handshake
            # ends. Wait so a session never has a debugger in it.
            await _wait_for_exit(helper, deadline)
            return METHOD_DEBUGPY
        code = helper.poll()
        if code is None:
            raise PidAttachError(
                f"cannot attach to pid {pid}: injection did not finish within "
                f"{_timeouts.PID_ATTACH_TOTAL:.0f} seconds, probably because "
                "native code is holding the GIL. It is still pending, and ends "
                "by itself when the program releases the GIL"
            )
        if code != 0:
            raise failure(_read_output(output))
        if os.path.exists(ran_marker):
            raise PidAttachError(
                f"cannot attach to pid {pid}: the injected code ran, but the "
                f"program did not connect within "
                f"{_timeouts.PID_ATTACH_TOTAL:.0f} seconds"
            )
        raise PidAttachError(
            f"cannot attach to pid {pid}: injection ended with no connection; "
            "the process doesn't seem to be running a Python tdb can reach"
        )
