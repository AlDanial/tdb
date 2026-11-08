"""`tdb -a PID` against real Python processes.

Targets opt in to tracing with PR_SET_PTRACER, so these pass under Yama
ptrace_scope=1 with no system changes. Every target is killed in teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import dataclasses
import glob
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from tdb import _timeouts
from tdb.languages import _python_gdb_helper
from tdb.languages.python import build_python_profile
from tdb.languages.python_attach import (
    PidAttachError,
    _debugpy_argv,
    _gdb_queue_commands,
    _hand_to_target,
    _queue_with_gdb,
    _queued_code,
    uses_musl,
)
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

from .conftest import TEST_SERVER_TOKEN, RpcClient, SRC_DIR, _free_port

TARGET = Path(__file__).parent / "fixtures" / "python_attach_target.py"
LINUX = sys.platform == "linux"

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "darwin", reason="injection needs root on macOS"
    ),
    pytest.mark.skipif(
        LINUX and shutil.which("gdb") is None, reason="gdb is needed on Linux"
    ),
]


def _line(tag: str) -> int:
    return next(
        i for i, text in enumerate(TARGET.read_text().splitlines(), 1) if tag in text
    )


WORK_LINE = _line("# WORK_LINE")
LOOP_LINE = _line("# LOOP_LINE")
CHILD_LINE = _line("# CHILD_LINE")


def _find_python(version: str) -> str | None:
    if f"{sys.version_info[0]}.{sys.version_info[1]}" == version:
        return sys.executable
    uv = shutil.which("uv")
    if uv is None:
        return None
    result = subprocess.run(
        [uv, "python", "find", "--no-project", version],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


PYTHONS = {v: _find_python(v) for v in ("3.11", "3.12", "3.13", "3.14")}
PY_314 = PYTHONS["3.14"]
needs_314 = pytest.mark.skipif(PY_314 is None, reason="Python 3.14 not found")
linux_only = pytest.mark.skipif(not LINUX, reason="Linux process inspection")
MUSL = LINUX and uses_musl(os.getpid())
needs_gdb_injection = pytest.mark.skipif(
    MUSL, reason="debugpy's injection library needs glibc"
)


def _glibc_below_2_41() -> bool:
    name, version = platform.libc_ver()
    return name == "glibc" and tuple(map(int, version.split(".")[:2])) < (2, 41)


# gdb's call into a main thread waiting for the GIL deadlocks in pthread_cond
# there. tdb only gets that far when native code holds the GIL for seconds.
needs_reentrant_condvar = pytest.mark.skipif(
    _glibc_below_2_41(), reason="glibc below 2.41 deadlocks here; see the docs"
)


@pytest.fixture(params=list(PYTHONS))
def python(request):
    path = PYTHONS[request.param]
    if path is None:
        pytest.skip(f"Python {request.param} not found")
    return path


class Target:
    def __init__(self, python, mode, tmp_path, opt_in=True, user=None):
        self.heartbeat = tmp_path / f"heartbeat-{mode}"
        self.stderr_path = tmp_path / f"stderr-{mode}"
        self.stdout_path = tmp_path / f"stdout-{mode}"
        argv = [python, str(TARGET), mode, str(self.heartbeat)]
        if not opt_in:
            argv.append("no-opt-in")
        self.stdout = open(self.stdout_path, "w")
        self.stderr = open(self.stderr_path, "w")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=self.stdout,
            stderr=self.stderr,
            user=user,
        )
        deadline = time.monotonic() + 15
        while "ready" not in self.stdout_path.read_text():
            assert time.monotonic() < deadline, "target never became ready"
            time.sleep(0.02)
        time.sleep(0.3)

    @property
    def pid(self):
        return self.proc.pid

    def alive(self):
        return self.proc.poll() is None

    def beat(self):
        return self.heartbeat.read_text() if self.heartbeat.exists() else ""

    def assert_running(self):
        """The program is alive and making progress."""
        assert self.alive()
        before = self.beat()
        deadline = time.monotonic() + 5
        while self.beat() == before:
            assert time.monotonic() < deadline, "target stopped making progress"
            time.sleep(0.02)

    def kill(self):
        if self.alive():
            self.proc.kill()
        self.proc.wait()
        self.stdout.close()
        self.stderr.close()


@pytest.fixture
def make_target(tmp_path):
    targets = []

    def make(mode, python=sys.executable, **kw):
        target = Target(python, mode, tmp_path, **kw)
        targets.append(target)
        return target

    yield make
    for target in targets:
        target.kill()


class Session:
    """`tdb --headless -a PID`, driven over RPC."""

    def __init__(self, pid, tmp_path, *extra, cwd=None):
        self.port = _free_port()
        self.stderr_path = tmp_path / f"tdb-stderr-{self.port}"
        self.stderr = open(self.stderr_path, "w")
        env = {
            **os.environ,
            # No empty entry: that means the cwd, even under -P.
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(SRC_DIR), os.environ.get("PYTHONPATH")])
            ),
            "TDB_LOG_DIR": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "TDB_SERVER_TOKEN": TEST_SERVER_TOKEN,
        }
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-m",
                "tdb",
                "--headless",
                "--server-port",
                str(self.port),
                "-a",
                str(pid),
                *extra,
            ],
            cwd=cwd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=self.stderr,
        )
        self.client = RpcClient(f"http://127.0.0.1:{self.port}")

    def wait_ready(self, timeout=40):
        """True once RPC is up; False if tdb exited first."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                with httpx.Client(timeout=1.0) as probe:
                    probe.post(
                        f"http://127.0.0.1:{self.port}/rpc",
                        json={"action": "status", "params": []},
                    )
                return True
            except httpx.HTTPError:
                time.sleep(0.1)
        raise AssertionError(f"tdb never came up: {self.errors()}")

    def errors(self):
        return self.stderr_path.read_text()

    def quit(self):
        try:
            self.client.call("quit")
        except httpx.HTTPError:
            pass
        self.close()

    def close(self):
        self.client.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.stderr.close()


@pytest.fixture
def attach(tmp_path):
    sessions = []

    def start(target, *extra, ready=True):
        session = Session(target.pid, tmp_path, *extra)
        sessions.append(session)
        if ready:
            assert session.wait_ready(), session.errors()
        return session

    yield start
    for session in sessions:
        session.close()


def run_to_line(session, line):
    """Set a breakpoint, resume, and return the stack at the hit."""
    session.client.ok("wait_for_stop", 10)
    session.client.ok("set_breakpoint", f"{TARGET}:{line}")
    session.client.ok("continue", 10)
    return session.client.ok("get_stack_trace")


def listening_sockets(pid):
    inodes = set()
    for fd in glob.glob(f"/proc/{pid}/fd/*"):
        try:
            link = os.readlink(fd)
        except OSError:
            continue
        if link.startswith("socket:["):
            inodes.add(link[8:-1])
    found = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        with open(table) as f:
            for row in f.readlines()[1:]:
                cols = row.split()
                if cols[3] == "0A" and cols[9] in inodes:
                    found.append(cols[1])
    return found


def child_pids(pid):
    with open(f"/proc/{pid}/task/{pid}/children") as f:
        return f.read().split()


def adapter_pids():
    result = subprocess.run(
        ["pgrep", "-f", "debugpy.adapter"], capture_output=True, text=True
    )
    return set(result.stdout.split())


def test_busy(make_target, attach, python):
    target = make_target("busy", python)
    session = attach(target)
    stack = run_to_line(session, WORK_LINE)
    assert "work" in stack and "loop" in stack
    session.client.ok("remove_breakpoint", f"{TARGET}:{WORK_LINE}")
    session.quit()
    target.assert_running()


@linux_only
def test_all_threads_busy(make_target, attach, python):
    # gdb used to stay inside the program for the whole injected call, and
    # a subprocess started there left the other threads stopped for good.
    target = make_target("busy_threads", python)
    session = attach(target)
    assert session.proc.poll() is None
    frames = run_to_line(session, WORK_LINE)
    assert frames
    session.quit()
    target.assert_running()


@linux_only
def test_program_that_allocates_constantly(make_target, attach, python):
    # gdb's queue call used to malloc in the target, and a main thread
    # stopped inside malloc deadlocked on itself.
    target = make_target("allocating", python)
    session = attach(target)
    assert session.proc.poll() is None
    session.quit()
    target.assert_running()


@linux_only
def test_program_shadowing_builtins(make_target, attach, python):
    # The queued call runs in __main__, where these names are the program's.
    target = make_target("shadowed", python)
    session = attach(target)
    assert session.proc.poll() is None
    # Failing here would only look like a blocked program, and fall back.
    assert "debugpy injection" not in session.errors()
    session.quit()
    target.assert_running()
    assert "SystemError" not in target.stderr_path.read_text()


def _pump_signals(pid, stop):
    while not stop.is_set():
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            return
        time.sleep(0.02)


@contextlib.contextmanager
def signals_to(target):
    stop = threading.Event()
    pump = threading.Thread(target=_pump_signals, args=(target.pid, stop))
    pump.start()
    try:
        yield
    finally:
        stop.set()
        pump.join()


@linux_only
def test_signals_during_gdb_queue_leave_program_running(make_target, python, tmp_path):
    target = make_target("signalled", python)
    script = tmp_path / "queued.py"
    script.write_text("pass")
    with signals_to(target):
        queued, output = asyncio.run(_queue_with_gdb(target.pid, str(script)))
        time.sleep(0.3)
    assert queued, output
    target.assert_running()


@linux_only
@needs_gdb_injection
def test_signals_during_fallback_leave_program_running(make_target, python, tmp_path):
    target = make_target("signalled", python)
    (tmp_path / "token").write_text("token")
    with signals_to(target):
        subprocess.run(
            _debugpy_argv(target.pid, _free_port(), str(tmp_path / "ran")),
            capture_output=True,
            timeout=30,
        )
        time.sleep(0.3)
    target.assert_running()


@linux_only
def test_gdb_queue_never_calls_malloc(make_target, tmp_path):
    # Only the main thread runs, so a hit can only come from gdb's own calls.
    target = make_target("sleep")
    commands = tmp_path / "commands.gdb"
    commands.write_text(
        "set scheduler-locking on\nbreak malloc\n"
        + _gdb_queue_commands(_queued_code("/nonexistent"))
    )
    result = subprocess.run(
        ["gdb", "--nw", "--nh", "--nx", "--pid", str(target.pid), "--batch"]
        + ["-x", str(commands)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "tdb-queued 0" in result.stdout, result.stdout + result.stderr
    assert "Breakpoint 1," not in result.stdout


needs_root = pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="needs root")
NOBODY = 65534


def _shared_workdir():
    """For root's temp files and the target's: sticky, like /tmp."""
    workdir = Path(tempfile.mkdtemp(prefix="tdb-root-test-"))
    workdir.chmod(0o1777)
    return workdir


def _as_nobody():
    os.setgid(NOBODY)
    os.setuid(NOBODY)


@linux_only
@needs_root
def test_root_target_user_cannot_replace_gdb_commands(python):
    # They own the work directory, and root's gdb must not run their commands.
    workdir = _shared_workdir()
    proof = workdir / "proof"
    attacker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import glob, os, sys, time\n"
            "work, proof = sys.argv[1:]\n"
            "evil = os.path.join(work, 'evil')\n"
            "open(evil, 'w').write(f'shell touch {proof}\\n')\n"
            "while True:\n"
            "    for d in glob.glob(os.path.join(work, 'tdb-*')):\n"
            "        try:\n"
            "            with open(evil) as f: text = f.read()\n"
            "            with open(d + '/new', 'w') as f: f.write(text)\n"
            "            os.rename(d + '/new', d + '/queue.gdb')\n"
            "        except OSError:\n"
            "            pass\n",
            str(workdir),
            str(proof),
        ],
        preexec_fn=_as_nobody,
    )
    target = Target(python, "busy", workdir, user=NOBODY)
    session = Session(target.pid, workdir)
    try:
        assert session.wait_ready(), session.errors()
        assert not proof.exists()
    finally:
        session.close()
        attacker.kill()
        attacker.wait()
        target.kill()
        shutil.rmtree(workdir, ignore_errors=True)


@linux_only
@needs_root
def test_root_refuses_a_tmpdir_others_can_change(python):
    # sudo's env_keep can leave root a TMPDIR the user can write to. Nothing
    # stops them swapping tdb's directory there, and root writes through it.
    workdir = Path(tempfile.mkdtemp(prefix="tdb-root-test-"))
    workdir.chmod(0o777)
    target = Target(python, "busy", workdir, user=NOBODY)
    session = Session(target.pid, workdir)
    try:
        session.proc.wait(timeout=40)
        assert "TMPDIR" in session.errors()
        assert glob.glob(str(workdir / "tdb-attach-*")) == []
        target.assert_running()
    finally:
        session.close()
        target.kill()
        shutil.rmtree(workdir, ignore_errors=True)


@linux_only
@needs_root
def test_root_chown_never_follows_a_symlink():
    # A real process owned by nobody, for its uid.
    target = subprocess.Popen(["sleep", "30"], preexec_fn=_as_nobody)
    workdir = Path(tempfile.mkdtemp(prefix="tdb-root-test-"))
    victim = workdir / "victim"
    victim.touch()
    link = workdir / "link"
    link.symlink_to(victim)
    try:
        _hand_to_target([str(link)], target.pid)
        assert victim.stat().st_uid == 0
    finally:
        target.kill()
        target.wait()
        shutil.rmtree(workdir, ignore_errors=True)


@linux_only
@pytest.mark.parametrize(
    "script", ("raise ValueError('boom')", "raise SystemExit(3)", None)
)
def test_failing_queued_call_leaves_program_running(
    make_target, python, tmp_path, script
):
    # A pending call that fails raises SystemError in the main thread.
    target = make_target("busy", python)
    path = tmp_path / "queued.py"
    if script is not None:
        path.write_text(script)
    queued, _ = asyncio.run(_queue_with_gdb(target.pid, str(path)))
    assert queued
    time.sleep(0.5)
    target.assert_running()
    assert "SystemError" not in target.stderr_path.read_text()


@linux_only
@needs_gdb_injection
def test_fallback_with_busy_worker(make_target, attach, python):
    # Run inside gdb's call, the injected code froze the other threads.
    target = make_target("join_busy", python)
    session = attach(target)
    assert "debugpy injection" in session.errors()
    assert run_to_line(session, WORK_LINE)
    session.quit()
    target.assert_running()


@linux_only
@needs_root
def test_root_attaches_to_another_users_program(python):
    # What tdb tells people to do under Yama. Run as root by the CI job.
    workdir = _shared_workdir()
    target = Target(python, "busy", workdir, user=65534)
    session = Session(target.pid, workdir)
    try:
        assert session.wait_ready(), session.errors()
        assert run_to_line(session, WORK_LINE)
        session.quit()
        target.assert_running()
    finally:
        session.close()
        target.kill()
        shutil.rmtree(workdir, ignore_errors=True)


def _plant_debugpy(directory):
    """A debugpy/ in `directory` that leaves a proof file if imported."""
    proof = directory / "proof"
    (directory / "debugpy").mkdir()
    (directory / "debugpy" / "__init__.py").write_text(
        f"import os; open({str(proof)!r}, 'a').write(f'uid={{os.getuid()}}')\n"
    )
    return proof


@linux_only
def test_attach_ignores_debugpy_in_cwd(make_target, tmp_path):
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    proof = _plant_debugpy(hostile)
    target = make_target("busy")
    session = Session(target.pid, tmp_path, cwd=hostile)
    try:
        ready = session.wait_ready()
        assert not proof.exists(), f"planted code ran: {proof.read_text()}"
        assert ready, session.errors()
        # The attach has to have happened, or nothing was proved.
        assert run_to_line(session, WORK_LINE)
        session.quit()
        target.assert_running()
    finally:
        session.close()


@linux_only
@needs_root
def test_root_attach_ignores_debugpy_in_cwd(python):
    # `sudo tdb -a PID` from a directory the target's user controls.
    workdir = _shared_workdir()
    hostile = workdir / "hostile"
    hostile.mkdir()
    proof = _plant_debugpy(hostile)
    target = Target(python, "busy", workdir, user=NOBODY)
    session = Session(target.pid, workdir, cwd=hostile)
    try:
        ready = session.wait_ready()
        assert not proof.exists(), f"planted code ran: {proof.read_text()}"
        assert ready, session.errors()
        assert run_to_line(session, WORK_LINE)
        session.quit()
        target.assert_running()
    finally:
        session.close()
        target.kill()
        shutil.rmtree(workdir, ignore_errors=True)


@linux_only
def test_running_frame_breakpoint(make_target, attach, python):
    target = make_target("busy", python)
    session = attach(target)
    assert "loop" in run_to_line(session, LOOP_LINE)


@linux_only
@needs_gdb_injection
def test_main_in_join_worker_breakpoint(make_target, attach, python):
    target = make_target("join", python)
    session = attach(target)
    assert "work" in run_to_line(session, WORK_LINE)


@linux_only
@needs_gdb_injection
def test_blocked_main_attaches_and_quits(make_target, attach, python):
    target = make_target("sleep", python)
    session = attach(target)
    assert "stopped" not in session.client.ok("status").lower()
    session.quit()
    time.sleep(1)
    assert target.alive()


@linux_only
@needs_314
@needs_gdb_injection
def test_blocked_314_falls_back_to_debugpy(make_target, attach):
    target = make_target("recv", PY_314)
    started = time.monotonic()
    session = attach(target)
    # 5s for remote_exec, then headless waits up to 10s for a stop.
    assert time.monotonic() - started < 25
    assert "debugpy injection" in session.errors()


@linux_only
@needs_314
def test_busy_314_uses_remote_exec(make_target, attach):
    session = attach(make_target("busy", PY_314))
    assert "sys.remote_exec" in session.errors()


@linux_only
def test_attach_twice(make_target, attach, python):
    target = make_target("busy", python)
    for _ in range(2):
        session = attach(target)
        assert "work" in run_to_line(session, WORK_LINE)
        session.client.ok("remove_breakpoint", f"{TARGET}:{WORK_LINE}")
        session.quit()
        target.assert_running()


@linux_only
def test_quit_while_paused_at_attach_leaves_program_running(
    make_target, attach, python
):
    # The pause must not land inside the injected call: quitting there
    # used to kill gdb mid-call and segfault the target.
    target = make_target("busy", python)
    session = attach(target)
    assert "stopped" in session.client.ok("status").lower()
    session.quit()
    target.assert_running()


@linux_only
@needs_gdb_injection
def test_gdb_is_gone_once_session_starts(make_target, attach):
    target = make_target("sleep")
    attach(target)
    tracer = next(
        line
        for line in Path(f"/proc/{target.pid}/status").read_text().splitlines()
        if line.startswith("TracerPid")
    )
    assert tracer.split()[1] == "0"


@linux_only
def test_no_port_or_child_left_after_quit(make_target, attach, python):
    target = make_target("busy", python)
    session = attach(target)
    session.client.ok("wait_for_stop", 10)
    session.quit()
    target.assert_running()
    assert listening_sockets(target.pid) == []
    assert child_pids(target.pid) == []


@linux_only
def test_output_reaches_console(make_target, attach):
    session = attach(make_target("busy"))
    session.client.ok("wait_for_stop", 10)
    session.client.call("continue", 0.5)
    deadline = time.monotonic() + 5
    output = ""
    while "tick" not in output and time.monotonic() < deadline:
        output += session.client.ok("get_output") or ""
        time.sleep(0.1)
    assert "tick" in output


def _silent_adapter_program(python, close_it):
    """A program stuck in real pydevd: connected, with no configurationDone."""
    code = _python_gdb_helper.FAST_HANDOFFS.replace(
        repr(_python_gdb_helper.SWITCH_INTERVAL_LIMIT), "1.0"
    ) + (
        "import debugpy, sys, threading, time\n"
        "tools = lambda: [sys.monitoring.get_tool(i) for i in range(6)]\n"
        "state = (lambda: (sys.gettrace(), tools() if hasattr(sys, 'monitoring') else []))\n"
        "seen = {}\n"
        "def other_thread():\n"
        "    released.wait()\n"
        "    (lambda: None)()\n"
        "    (lambda: None)()\n"
        "    seen['worker'] = sys.gettrace()\n"
        "released = threading.Event()\n"
        "worker = threading.Thread(target=other_thread)\n"
        "worker.start()\n"
        "_tdb_fast_handoffs(lambda: debugpy.connect(('127.0.0.1', PORT)))\n"
        "time.sleep(0.5)\n"
        "(lambda: None)()\n"
        "print('released', flush=True)\n"
        "released.set()\n"
        "worker.join()\n"
        "print('main', *state(), flush=True)\n"
        "print('worker', seen['worker'], flush=True)\n"
    )
    import debugpy

    env = {**os.environ, "PYTHONPATH": str(Path(debugpy.__file__).parents[1])}
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        program = subprocess.Popen(
            [python, "-c", code.replace("PORT", str(server.getsockname()[1]))],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        server.settimeout(30)
        accepted, _ = server.accept()
        with accepted:
            if close_it:
                accepted.close()
            try:
                out, err = program.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                program.kill()
                out, err = program.communicate()
                out += "still frozen"
    return out, err


@pytest.mark.parametrize("close_it", [False, True], ids=["silent", "gone"])
def test_program_gets_free_if_the_attach_never_finishes(python, close_it):
    # pydevd waits for configurationDone with no timeout. Every thread has to
    # end up untraced too: leaving a tracer costs the program speed for good.
    out, err = _silent_adapter_program(python, close_it)
    assert "released" in out, out + err
    assert (
        "main None []" in out or "main None [None, None, None, None, None, None]" in out
    ), out + err
    assert "worker None" in out, out + err


def _child_log(target):
    path = Path(f"{target.heartbeat}.children")
    return path.read_text() if path.exists() else ""


def _wait_for_children(target, count):
    wanted = _child_log(target).count("exit ") + count
    deadline = time.monotonic() + 20
    while _child_log(target).count("exit ") < wanted:
        assert time.monotonic() < deadline, _child_log(target)[-2000:]
        time.sleep(0.05)


@linux_only
@needs_gdb_injection
@pytest.mark.parametrize("mode", ["forking", "forking_raise"])
def test_forked_children_never_stop_for_tdb(make_target, attach, python, mode):
    # They have nothing left to talk to tdb with, so a stop there is for good.
    target = make_target(mode, python)
    session = attach(target, "--no-pause-on-attach")
    session.client.ok("set_breakpoint", f"{TARGET}:{CHILD_LINE}")
    _wait_for_children(target, 3)
    during = _child_log(target)
    session.quit()
    _wait_for_children(target, 3)
    assert "hung" not in _child_log(target)
    # The session's socket stays open in a child, and tdb then can't see the
    # target die while the child lives.
    assert "sockets 0" in during and "sockets 1" not in during
    warned = "PYDEV DEBUGGER WARNING" in target.stderr_path.read_text()
    assert not warned, "a forked child wrote to the program's stderr"


@linux_only
@needs_gdb_injection
@pytest.mark.parametrize("mode", ["spawning", "join_spawning"])
def test_python_children_stay_out_of_the_session(make_target, attach, mode):
    # debugpy would rewrite their command lines to dial the adapter: that
    # shows its port and token to every local user, and once tdb has gone
    # they die on the dead port or connect to whoever took it.
    target = make_target(mode)
    session = attach(target, "--no-pause-on-attach")
    _wait_for_children(target, 3)
    session.quit()
    _wait_for_children(target, 3)
    log = _child_log(target)
    assert "settrace" not in log
    assert {line for line in log.splitlines() if line.startswith("exit")} == {"exit 0"}


@linux_only
def test_no_pause_on_attach_leaves_program_running(make_target, attach):
    target = make_target("busy")
    attach(target, "--no-pause-on-attach")
    target.assert_running()


@linux_only
@needs_gdb_injection
def test_queued_script_stays_silent_after_fallback(
    make_target, attach, tmp_path, python
):
    target = make_target("stdin", python)
    session = attach(target)
    assert "debugpy injection" in session.errors()
    session.quit()
    target.proc.stdin.write(b"wake\n")
    target.proc.stdin.flush()
    target.assert_running()
    time.sleep(0.5)
    assert target.alive()
    assert glob.glob(str(tmp_path / "tdb-attach-*")) == []
    assert "Traceback" not in target.stderr_path.read_text()


@linux_only
def test_target_already_using_debugpy(make_target, attach):
    target = make_target("debugpy_listen")
    session = attach(target, ready=False)
    assert session.proc.wait(timeout=10) == 2
    assert "tdb -r" in session.errors()
    target.assert_running()


class _ConfiguringHandler(ServerEventHandler):
    """Configures on `initialized`, as the TUI does."""

    controller: DebugController
    configure: asyncio.Task | None = None

    def on_initialized(self):
        super().on_initialized()
        self.configure = asyncio.ensure_future(self.controller.do_configure())


async def _quit_at(target, delay, *, use_gdb):
    """Attach in-process and call stop() `delay` seconds in."""
    profile = build_python_profile(attach_pid=target.pid)
    if use_gdb:
        profile.adapter.target = dataclasses.replace(
            profile.adapter.target, has_remote_exec=False
        )
    handler = _ConfiguringHandler()
    controller = DebugController(handler, profile=profile)
    handler.controller = controller
    attach = asyncio.ensure_future(controller.remote_attach("127.0.0.1", 0))
    await asyncio.sleep(delay)
    await controller.stop()
    for task in (attach, handler.configure):
        if task is not None:
            await asyncio.wait({task}, timeout=5)
            task.cancel()


# Spans injection and the handshake, where quitting used to hurt the target.
QUIT_DELAYS = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7)


@linux_only
@needs_gdb_injection
@pytest.mark.parametrize("delay", QUIT_DELAYS)
def test_quit_during_gdb_injection_leaves_program_running(make_target, delay):
    target = make_target("busy")
    asyncio.run(_quit_at(target, delay, use_gdb=True))
    time.sleep(0.5)
    target.assert_running()


@linux_only
@needs_314
@pytest.mark.parametrize("delay", (0.1, 0.2, 0.3, 0.4))
def test_quit_during_remote_exec_leaves_program_running(make_target, delay):
    target = make_target("busy", PY_314)
    asyncio.run(_quit_at(target, delay, use_gdb=False))
    time.sleep(0.5)
    target.assert_running()


@linux_only
@needs_gdb_injection
def test_quit_while_gil_is_held_leaves_program_alive(make_target):
    target = make_target("gil")
    asyncio.run(_quit_at(target, 1.5, use_gdb=True))
    assert target.proc.wait(timeout=20) == 0


@linux_only
@needs_gdb_injection
@needs_reentrant_condvar
def test_injection_timeout_leaves_program_alive(make_target, monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_REMOTE_EXEC", 0.5)
    monkeypatch.setattr(_timeouts, "PID_ATTACH_TOTAL", 2.0)
    target = make_target("gil")

    async def attach():
        profile = build_python_profile(attach_pid=target.pid)
        controller = DebugController(ServerEventHandler(), profile=profile)
        with pytest.raises(PidAttachError, match="pending"):
            await controller.remote_attach("127.0.0.1", 0)

    asyncio.run(attach())
    assert target.proc.wait(timeout=20) == 0


@linux_only
@pytest.mark.parametrize("delay", (0.7, 0.9, 1.1, 1.3, 1.5))
def test_ctrl_c_during_headless_attach_leaves_program_running(
    make_target, tmp_path, delay
):
    target = make_target("busy")
    session = Session(target.pid, tmp_path)
    time.sleep(delay)
    session.proc.send_signal(signal.SIGINT)
    session.proc.wait(timeout=15)
    session.close()
    time.sleep(0.5)
    target.assert_running()


@linux_only
def test_process_without_python_fails_quickly(tmp_path):
    def opt_in():  # PR_SET_PTRACER, PR_SET_PTRACER_ANY
        ctypes.CDLL(None).prctl(0x59616D61, ctypes.c_ulong(-1).value, 0, 0, 0)

    sleeper = subprocess.Popen(["sleep", "60"], preexec_fn=opt_in)
    try:
        session = Session(sleeper.pid, tmp_path, "--lang", "python")
        assert session.proc.wait(timeout=15) == 2
        assert "Python" in session.errors()
        session.close()
    finally:
        sleeper.kill()
        sleeper.wait()


@linux_only
@pytest.mark.skipif(not MUSL, reason="musl only")
@needs_314
def test_blocked_on_musl_explains_and_leaves_target_alone(make_target, attach):
    target = make_target("sleep", PY_314)
    session = attach(target, ready=False)
    assert session.proc.wait(timeout=20) == 2
    assert "musl" in session.errors()
    assert target.alive()


def _ptrace_denied() -> bool:
    try:
        scope = int(Path("/proc/sys/kernel/yama/ptrace_scope").read_text())
    except OSError:
        return False
    return scope >= 1 and os.geteuid() != 0


denied_only = pytest.mark.skipif(not _ptrace_denied(), reason="ptrace is allowed here")


@denied_only
def test_ptrace_denied_message(make_target, attach):
    target = make_target("busy", opt_in=False)
    session = attach(target, ready=False)
    assert session.proc.wait(timeout=40) == 2
    assert "ptrace_scope" in session.errors()
    assert target.alive()


@denied_only
def test_failed_attach_leaves_no_adapter(make_target, attach):
    before = adapter_pids()
    session = attach(make_target("busy", opt_in=False), ready=False)
    session.proc.wait(timeout=40)
    time.sleep(0.5)
    assert adapter_pids() - before == set()


@linux_only
def test_tui_end_to_end(make_target, tmp_path):
    import fcntl
    import pty
    import re
    import select
    import struct
    import termios

    target = make_target("busy")
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC_DIR) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "TDB_LOG_DIR": str(tmp_path),
        "TERM": "xterm-256color",
    }
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(
            sys.executable, [sys.executable, "-m", "tdb", "-a", str(target.pid)], env
        )
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    screen = b""

    def pump(seconds):
        nonlocal screen
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    screen += os.read(fd, 1 << 16)
                except OSError:
                    return

    def text():
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", screen.decode("utf8", "ignore"))

    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (
            "def loop" in text() and "Paused" in text()
        ):
            pump(0.3)
        assert "def loop" in text()
        assert "Paused" in text()
        os.write(fd, b"q")
        pump(1)
        os.write(fd, b"q")
        deadline = time.monotonic() + 15
        exited = 0
        while time.monotonic() < deadline and exited != pid:
            pump(0.3)
            exited = os.waitpid(pid, os.WNOHANG)[0]
        assert exited == pid
        target.assert_running()
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(fd)
