"""Injection strategy for `tdb -a PID` on Python.

Helpers are real short-lived subprocesses (only their command lines are
swapped), so the kill and reap paths run for real.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from tdb import _timeouts
from tdb.languages.python_attach import (
    PidAttachError,
    TargetInfo,
    build_remote_exec_script,
    classify_failure,
    inject,
)

MODULE = "tdb.languages.python_attach"
TARGET_314 = TargetInfo(os.getpid(), sys.executable, True)
TARGET_OLD = TargetInfo(os.getpid(), sys.executable, False)

EXIT_0 = [sys.executable, "-c", "pass"]
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def recording_pid(path, seconds: float = 60) -> list[str]:
    """A helper that writes its pid to `path`, then sleeps."""
    return [
        sys.executable,
        "-c",
        "import os, time\n"
        f"open({str(path)!r}, 'w').write(str(os.getpid()))\n"
        f"time.sleep({seconds})\n",
    ]


async def read_pid(path) -> int:
    while not (path.exists() and path.read_text()):
        await asyncio.sleep(0.01)
    return int(path.read_text())


def fail_with(text: str) -> list[str]:
    return [sys.executable, "-c", f"import sys; print({text!r}); sys.exit(1)"]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class Helpers:
    """Records which helpers ran, and runs the commands the test chose."""

    def __init__(self, monkeypatch):
        self.remote_exec = EXIT_0
        self.debugpy = EXIT_0
        self.remote_exec_scripts: list[str] = []
        self.debugpy_calls: list[list[str]] = []
        self.real_debugpy_argv = None
        monkeypatch.setattr(f"{MODULE}._remote_exec_argv", self._remote_exec_argv)
        import tdb.languages.python_attach as mod

        self.real_debugpy_argv = mod._debugpy_argv
        monkeypatch.setattr(f"{MODULE}._debugpy_argv", self._debugpy_argv)
        monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: "/usr/bin/gdb")

    def _remote_exec_argv(self, target, script_path):
        self.remote_exec_scripts.append(script_path)
        return self.remote_exec

    def _debugpy_argv(self, pid, port):
        self.debugpy_calls.append(self.real_debugpy_argv(pid, port))
        return self.debugpy

    @property
    def workdir(self) -> str:
        return os.path.dirname(self.remote_exec_scripts[0])


@pytest.fixture
def helpers(monkeypatch):
    return Helpers(monkeypatch)


@pytest.fixture
def short_timeouts(monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_REMOTE_EXEC", 0.3)
    monkeypatch.setattr(_timeouts, "PID_ATTACH_TOTAL", 1.5)


def connect_soon(connected: asyncio.Event, delay: float = 0.05) -> None:
    asyncio.get_running_loop().call_later(delay, connected.set)


# --- the script run inside the target -----------------------------------


def test_script_valid_with_awkward_paths():
    src = build_remote_exec_script(r"C:\it's a dir\server", 5678, r"C:\t\m", r'C:\t\"c')
    compile(src, "<payload>", "exec")


def _run_script(tmp_path, marker_present: bool, server_dir: str = "/nonexistent"):
    marker, claimed = tmp_path / "marker", tmp_path / "claimed"
    if marker_present:
        marker.write_text("")
    src = build_remote_exec_script(server_dir, 1, str(marker), str(claimed))
    scope: dict = {}
    exec(compile(src, "payload", "exec"), scope)
    return scope


def test_script_exits_silently_when_marker_taken(tmp_path, capfd):
    # With the marker gone the injection code must not run: importing
    # from /nonexistent would raise.
    _run_script(tmp_path, marker_present=False)
    assert capfd.readouterr().err == ""


def test_script_removes_its_directory_when_marker_taken(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _run_script(work, marker_present=False)
    assert not work.exists()


def test_script_leaves_no_names_in_target(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    scope = _run_script(work, marker_present=False)
    assert [k for k in scope if k != "__builtins__"] == []


def test_script_calls_debugpy_attach_when_it_wins_the_claim(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    (server / "attach_pid_injected.py").write_text(
        "import sys\ndef attach(setup):\n    sys.tdb_test_setup = setup\n"
    )
    work = tmp_path / "work"
    work.mkdir()
    before = list(sys.path)
    try:
        _run_script(work, marker_present=True, server_dir=str(server))
        setup = sys.tdb_test_setup
        assert setup["mode"] == "connect"
        assert setup["address"] == ["127.0.0.1", 1]
        assert sys.path == before
        assert not work.exists()
    finally:
        sys.modules.pop("attach_pid_injected", None)
        sys.__dict__.pop("tdb_test_setup", None)


# --- classifying failures -----------------------------------------------


def classify(**kw):
    defaults = dict(
        inject_output="",
        remote_exec_error="",
        gdb_missing=False,
        ptrace_scope=None,
        is_root=False,
    )
    return classify_failure(**{**defaults, **kw})


def test_classify_ptrace_from_gdb():
    msg = classify(
        inject_output="check the setting of /proc/sys/kernel/yama/ptrace_scope",
        ptrace_scope=1,
    )
    assert "sudo sysctl kernel.yama.ptrace_scope=0" in msg
    assert "root" in msg


def test_classify_runtime_lookup_with_yama_is_permission():
    msg = classify(
        remote_exec_error="RuntimeError: PyRuntime address lookup failed",
        gdb_missing=True,
        ptrace_scope=1,
    )
    assert "ptrace_scope" in msg


def test_classify_runtime_lookup_as_root_not_permission():
    msg = classify(
        remote_exec_error="RuntimeError: PyRuntime address lookup failed",
        ptrace_scope=1,
        is_root=True,
    )
    assert "ptrace_scope" not in msg


def test_classify_scope_3_mentions_reboot():
    msg = classify(inject_output="ptrace: Operation not permitted", ptrace_scope=3)
    assert "reboot" in msg
    assert "sysctl" not in msg


def test_classify_gdb_missing():
    msg = classify(gdb_missing=True)
    assert "install gdb" in msg.lower()
    assert "3.14" in msg and "blocked" in msg


def test_classify_remote_debug_disabled_no_gdb():
    msg = classify(
        remote_exec_error="RuntimeError: Remote debugging is not enabled",
        gdb_missing=True,
    )
    assert "PYTHON_DISABLE_REMOTE_DEBUG" in msg
    assert "gdb" in msg


def test_classify_unknown_keeps_last_output_line():
    assert "boom" in classify(inject_output="noise\nboom\n")


# --- strategy order -----------------------------------------------------


async def test_inject_uses_remote_exec_when_it_connects(helpers):
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_314, 5678, connected) == "sys.remote_exec"
    assert helpers.debugpy_calls == []


async def test_inject_falls_back_when_blocked(helpers, short_timeouts):
    helpers.debugpy = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    connected = asyncio.Event()
    connect_soon(connected, 0.4)
    assert await inject(TARGET_314, 5678, connected) == "debugpy injection"
    argv = helpers.debugpy_calls[0]
    assert "--disable-sys-remote-exec" in argv
    assert "127.0.0.1:5678" in argv
    # tdb claimed the marker, so the queued script stays silent, and the
    # script itself stays in place for the target to read.
    assert not os.path.exists(os.path.join(helpers.workdir, "marker"))
    assert os.path.exists(helpers.remote_exec_scripts[0])


async def test_inject_skips_remote_exec_below_314(helpers):
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_OLD, 5678, connected) == "debugpy injection"
    assert helpers.remote_exec_scripts == []


async def test_inject_skips_remote_exec_for_unknown_interpreter(helpers):
    connected = asyncio.Event()
    connect_soon(connected)
    unknown = TargetInfo(os.getpid(), None, False)
    assert await inject(unknown, 5678, connected) == "debugpy injection"


async def test_inject_falls_back_when_remote_exec_fails(helpers):
    helpers.remote_exec = fail_with("RuntimeError: Remote debugging is not enabled")
    connected = asyncio.Event()
    connect_soon(connected, 0.3)
    assert await inject(TARGET_314, 5678, connected) == "debugpy injection"
    assert not os.path.exists(helpers.workdir)


async def test_inject_no_fallback_if_script_won_claim(
    helpers, monkeypatch, short_timeouts
):
    # The script fired just as tdb gave up: it owns the attempt, so tdb
    # must wait for its connection and not inject a second time.
    def removes_marker(target, script_path):
        marker = os.path.join(os.path.dirname(script_path), "marker")
        helpers.remote_exec_scripts.append(script_path)
        return [sys.executable, "-c", f"import os; os.remove({marker!r})"]

    monkeypatch.setattr(f"{MODULE}._remote_exec_argv", removes_marker)
    connected = asyncio.Event()
    connect_soon(connected, 0.6)
    assert await inject(TARGET_314, 5678, connected) == "sys.remote_exec"
    assert helpers.debugpy_calls == []


async def test_inject_fails_fast_on_nonzero_helper(helpers):
    helpers.debugpy = fail_with("ptrace: Operation not permitted")
    started = time.monotonic()
    with pytest.raises(PidAttachError, match="ptrace"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert time.monotonic() - started < 5


async def test_inject_gdb_missing_on_linux(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: None)
    with pytest.raises(PidAttachError, match="gdb"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert helpers.debugpy_calls == []


async def test_inject_gdb_not_needed_elsewhere(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "darwin")
    monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: None)
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_OLD, 5678, connected) == "debugpy injection"


async def test_inject_timeout_never_signals_helper(helpers, short_timeouts, tmp_path):
    # gdb may be inside the target; killing it there segfaults the program.
    pidfile = tmp_path / "pid"
    helpers.debugpy = recording_pid(pidfile, seconds=3)
    with pytest.raises(PidAttachError, match="pending") as exc_info:
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert str(os.getpid()) in str(exc_info.value)
    assert alive(await read_pid(pidfile))


async def test_inject_target_exits_mid_injection(helpers, monkeypatch, short_timeouts):
    helpers.debugpy = [sys.executable, "-c", "import time; time.sleep(1)"]
    monkeypatch.setattr(_timeouts, "PID_ATTACH_TOTAL", 30.0)
    monkeypatch.setattr(f"{MODULE}.pid_exists", lambda pid: False)
    started = time.monotonic()
    with pytest.raises(PidAttachError, match="exited"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert time.monotonic() - started < 5


async def test_inject_cancel_never_signals_helper(helpers, tmp_path):
    pidfile = tmp_path / "pid"
    helpers.debugpy = recording_pid(pidfile, seconds=3)
    task = asyncio.create_task(inject(TARGET_OLD, 5678, asyncio.Event()))
    pid = await read_pid(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert alive(pid)


async def test_inject_cancel_silences_queued_script(helpers):
    helpers.debugpy = [sys.executable, "-c", "import time; time.sleep(1)"]
    task = asyncio.create_task(inject(TARGET_314, 5678, asyncio.Event()))
    while not helpers.remote_exec_scripts:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not os.path.exists(os.path.join(helpers.workdir, "marker"))


async def test_inject_returns_only_after_helper_exits(helpers, tmp_path):
    # gdb stays attached until the helper exits; returning sooner lets a
    # debug session start with gdb inside the target.
    pidfile = tmp_path / "pid"
    helpers.debugpy = recording_pid(pidfile, seconds=0.5)
    connected = asyncio.Event()
    task = asyncio.create_task(inject(TARGET_OLD, 5678, connected))
    pid = await read_pid(pidfile)
    connected.set()
    await task
    assert not alive(pid)


async def test_inject_leaves_helper_stuck_after_connect(
    helpers, short_timeouts, tmp_path
):
    pidfile = tmp_path / "pid"
    helpers.debugpy = recording_pid(pidfile, seconds=3)
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_OLD, 5678, connected) == "debugpy injection"
    assert alive(await read_pid(pidfile))


async def test_inject_cancel_kills_remote_exec_helper(helpers, tmp_path):
    pidfile = tmp_path / "pid"
    helpers.remote_exec = recording_pid(pidfile)
    task = asyncio.create_task(inject(TARGET_314, 5678, asyncio.Event()))
    pid = await read_pid(pidfile)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(pid)


def test_classify_drops_debugpy_command_echo():
    output = "Error: gdb exited\nRunning: gdb -p 42 " + "1, " * 600 + "\n"
    msg = classify(inject_output=output)
    assert "Running:" not in msg
    assert "gdb exited" in msg
    assert len(msg) < 400


def test_classify_explains_quote_in_install_path(monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.sys.executable", "/opt/it's/bin/python")
    msg = classify(inject_output="Running: gdb\n")
    assert "path" in msg


async def test_inject_helper_exit_without_connection_fails_soon(helpers, monkeypatch):
    # Injection blocks through the handshake, so a helper that exits 0
    # without a connection found no interpreter to run in.
    monkeypatch.setattr(_timeouts, "PID_ATTACH_TOTAL", 30.0)
    monkeypatch.setattr(f"{MODULE}._HELPER_EXIT_GRACE", 0.2)
    started = time.monotonic()
    with pytest.raises(PidAttachError, match="Python"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert time.monotonic() - started < 5
