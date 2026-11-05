"""`tdb -a PID` against real Python processes.

Targets opt in to tracing with PR_SET_PTRACER, so these pass under Yama
ptrace_scope=1 with no system changes. Every target is killed in teardown.
"""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import glob
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tdb import _timeouts
from tdb.languages.python import build_python_profile
from tdb.languages.python_attach import PidAttachError, uses_musl
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

from .conftest import RpcClient, SRC_DIR, _free_port

TARGET = Path(__file__).parent / "fixtures" / "python_attach_target.py"
LINUX = sys.platform == "linux"

pytestmark = [
    pytest.mark.skipif(sys.platform == "darwin", reason="injection needs root on macOS"),
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


PYTHONS = {v: _find_python(v) for v in ("3.11", "3.12", "3.14")}
PY_314 = PYTHONS["3.14"]
needs_314 = pytest.mark.skipif(PY_314 is None, reason="Python 3.14 not found")
linux_only = pytest.mark.skipif(not LINUX, reason="Linux process inspection")
MUSL = LINUX and uses_musl(os.getpid())
needs_gdb_injection = pytest.mark.skipif(
    MUSL, reason="debugpy's injection library needs glibc"
)


@pytest.fixture(params=list(PYTHONS))
def python(request):
    path = PYTHONS[request.param]
    if path is None:
        pytest.skip(f"Python {request.param} not found")
    if MUSL and request.param != "3.14":
        pytest.skip("only sys.remote_exec works on musl")
    return path


class Target:
    def __init__(self, python, mode, tmp_path, opt_in=True):
        self.heartbeat = tmp_path / f"heartbeat-{mode}"
        self.stderr_path = tmp_path / f"stderr-{mode}"
        self.stdout_path = tmp_path / f"stdout-{mode}"
        argv = [python, str(TARGET), mode, str(self.heartbeat)]
        if not opt_in:
            argv.append("no-opt-in")
        self.stdout = open(self.stdout_path, "w")
        self.stderr = open(self.stderr_path, "w")
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=self.stdout, stderr=self.stderr
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

    def __init__(self, pid, tmp_path, *extra):
        self.port = _free_port()
        self.stderr_path = tmp_path / f"tdb-stderr-{self.port}"
        self.stderr = open(self.stderr_path, "w")
        env = {
            **os.environ,
            "PYTHONPATH": str(SRC_DIR) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "TDB_LOG_DIR": str(tmp_path),
            "TMPDIR": str(tmp_path),
        }
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tdb",
                "--headless",
                "--server-port",
                str(self.port),
                "-a",
                str(pid),
                *extra,
            ],
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
def test_quit_while_paused_at_attach_leaves_program_running(make_target, attach, python):
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


@linux_only
def test_no_pause_on_attach_leaves_program_running(make_target, attach):
    target = make_target("busy")
    attach(target, "--no-pause-on-attach")
    target.assert_running()


@linux_only
@needs_314
@needs_gdb_injection
def test_queued_script_stays_silent_after_fallback(make_target, attach, tmp_path):
    target = make_target("stdin", PY_314)
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
def test_injection_timeout_leaves_program_alive(make_target, monkeypatch):
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
