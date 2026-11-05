"""Injection strategy for `tdb -a PID` on Python.

Helpers are real short-lived subprocesses (only their command lines are
swapped), so the kill and reap paths run for real.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

from unittest.mock import Mock

import pytest

from tdb import _timeouts
from tdb.languages._python_gdb_helper import background_code
from tdb.languages.python_attach import (
    PidAttachCancelled,
    PidAttachError,
    TargetInfo,
    _gdb_queue_commands,
    _hand_to_target,
    _queued_code,
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
        self.gdb_queue = fail_with('No symbol "Py_AddPendingCall"')
        self.gdb_queue_scripts: list[str] = []
        self.debugpy_calls: list[list[str]] = []
        self.on_fallback = lambda: None
        self.real_debugpy_argv = None
        monkeypatch.setattr(f"{MODULE}._remote_exec_argv", self._remote_exec_argv)
        monkeypatch.setattr(f"{MODULE}._gdb_queue_argv", self._gdb_queue_argv)
        import tdb.languages.python_attach as mod

        self.real_debugpy_argv = mod._debugpy_argv
        monkeypatch.setattr(f"{MODULE}._debugpy_argv", self._debugpy_argv)
        monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: "/usr/bin/gdb")
        # The suite itself may run on musl (CI is Alpine).
        monkeypatch.setattr(f"{MODULE}.uses_musl", lambda pid: False)

    def _remote_exec_argv(self, target, script_path):
        self.remote_exec_scripts.append(script_path)
        return self.remote_exec

    def _gdb_queue_argv(self, pid, script_path):
        self.gdb_queue_scripts.append(script_path)
        return self.gdb_queue

    def _debugpy_argv(self, pid, port, ran_marker):
        self.debugpy_calls.append(self.real_debugpy_argv(pid, port, ran_marker))
        self.on_fallback()
        if callable(self.debugpy):
            return self.debugpy(ran_marker)
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


def test_script_speeds_up_gil_handoffs_then_restores(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    (server / "attach_pid_injected.py").write_text(
        "import sys\ndef attach(setup): print(sys.getswitchinterval())\n"
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    marker = workdir / "marker"
    marker.touch()
    script = build_remote_exec_script(
        str(server), 5678, str(marker), str(workdir / "claimed")
    )
    program = f"import sys\nexec({script!r})\nprint(sys.getswitchinterval())\n"
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    during, after = (float(x) for x in result.stdout.split())
    assert during < 0.001
    assert after == pytest.approx(0.005)


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


# --- the code gdb injects ------------------------------------------------


def _run_background_code(tmp_path, code: str) -> subprocess.CompletedProcess:
    marker = tmp_path / "ran"
    injected = background_code(code, str(marker))
    program = f"""
import sys, time
exec({injected!r})
time.sleep(0.5)
print("main", flush=True)
"""
    return subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )


def test_background_code_has_nothing_gdb_cannot_quote(tmp_path):
    injected = background_code("print('it\\'s')\n", str(tmp_path / "it's"))
    assert not {'"', "'", "\r", "\n"} & set(injected)


def test_background_code_returns_before_the_code_finishes(tmp_path):
    code = "import time;time.sleep(0.2);print(chr(98)+chr(103),flush=True)"
    result = _run_background_code(tmp_path, code)
    assert result.stdout.split() == ["bg", "main"]


def test_background_code_runs_off_the_main_thread(tmp_path):
    code = (
        "import threading;"
        "print(threading.current_thread() is threading.main_thread(),flush=True)"
    )
    assert _run_background_code(tmp_path, code).stdout.split()[0] == "False"


def test_background_code_records_that_it_ran(tmp_path):
    _run_background_code(tmp_path, "pass")
    assert (tmp_path / "ran").exists()


def test_background_code_stops_if_tdb_cleaned_up(tmp_path):
    # tdb gave up and removed the directory: nothing may dial back.
    marker = tmp_path / "gone" / "ran"
    injected = background_code("print(chr(98),flush=True)", str(marker))
    result = subprocess.run(
        [sys.executable, "-c", f"exec({injected!r})\nimport time; time.sleep(0.3)"],
        capture_output=True,
        text=True,
    )
    assert "b" not in result.stdout.split()
    assert result.stderr == ""


def test_background_code_speeds_up_gil_handoffs_then_restores(tmp_path):
    code = "import sys;print(sys.getswitchinterval(),flush=True)"
    marker = tmp_path / "ran"
    injected = background_code(code, str(marker))
    program = f"""
import sys, time
exec({injected!r})
time.sleep(0.5)
print(sys.getswitchinterval(), flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    during, after = (float(x) for x in result.stdout.split())
    assert during < 0.001
    assert after == pytest.approx(0.005)


def test_background_code_leaves_no_names_in_the_program(tmp_path):
    injected = background_code("pass", str(tmp_path / "ran"))
    program = (
        f"before = set(globals())\nexec({injected!r})\n"
        "print(sorted(set(globals()) - before - {'before'}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.stdout.strip() == "[]"


def test_background_code_ignores_names_in_the_program(tmp_path):
    injected = background_code("pass", str(tmp_path / "ran"))
    program = f"from os import open\nbytes = chr = None\nexec({injected!r})\n"
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.stderr == ""
    assert (tmp_path / "ran").exists()


def test_switch_interval_comes_back_if_the_attach_never_returns(monkeypatch):
    # A lost adapter leaves pydevd waiting for good. Watch from a thread, as
    # the program would.
    import tdb.languages._python_gdb_helper as helper

    code = helper.FAST_HANDOFFS.replace(
        repr(helper.SWITCH_INTERVAL_LIMIT), "0.2"
    ) + (
        "import sys, threading, time\n"
        "threading.Thread(target=_tdb_fast_handoffs,"
        " args=(lambda: time.sleep(5),), daemon=True).start()\n"
        "time.sleep(0.1)\nprint(sys.getswitchinterval())\n"
        "time.sleep(0.4)\nprint(sys.getswitchinterval())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    during, after = (float(x) for x in result.stdout.split())
    assert during < 0.001
    assert after == pytest.approx(0.005)


def test_background_code_keeps_the_real_main_thread(tmp_path):
    # attach() assumes the main thread if threading isn't imported yet.
    code = "import threading;print(threading.main_thread().name,flush=True)"
    assert _run_background_code(tmp_path, code).stdout.split()[0] == "MainThread"


# --- classifying failures -----------------------------------------------


def classify(**kw):
    defaults = dict(
        inject_output="",
        remote_exec_error="",
        gdb_missing=False,
        ptrace_scope=None,
        is_root=False,
        musl=False,
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


def test_classify_remote_exec_permission_error_is_permission():
    reason = classify(
        remote_exec_error="PermissionError: [Errno 1] Operation not permitted",
        ptrace_scope=1,
    )
    assert "ptrace_scope" in reason


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


def test_classify_injection_library_failed_to_load():
    output = 'noise\nNo symbol "DoAttach" in current context.\nCalledProcessError: gdb\n'
    assert "could not load" in classify(inject_output=output)


def test_classify_musl_explains_what_can_attach():
    reason = classify(musl=True)
    assert "musl" in reason
    assert "running Python code" in reason


def test_classify_permission_wins_over_musl():
    reason = classify(
        musl=True,
        remote_exec_error="PyRuntime address lookup failed",
        ptrace_scope=1,
    )
    assert "ptrace_scope" in reason


# --- strategy order -----------------------------------------------------


async def test_inject_uses_remote_exec_when_it_connects(helpers):
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_314, 5678, connected) == "sys.remote_exec"
    assert helpers.debugpy_calls == []


async def test_inject_falls_back_when_blocked(helpers, short_timeouts):
    helpers.debugpy = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    connected = asyncio.Event()
    helpers.on_fallback = connected.set
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


QUEUED = [sys.executable, "-c", "print('tdb-queued 0')"]


async def test_inject_queues_through_gdb_below_314(helpers):
    helpers.gdb_queue = QUEUED
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_OLD, 5678, connected) == "gdb pending call"
    assert helpers.debugpy_calls == []


async def test_inject_falls_back_when_gdb_queue_never_fires(helpers, short_timeouts):
    helpers.gdb_queue = QUEUED
    helpers.debugpy = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    connected = asyncio.Event()
    helpers.on_fallback = connected.set
    assert await inject(TARGET_OLD, 5678, connected) == "debugpy injection"
    # tdb claimed it, so the call left queued in the target stays silent.
    workdir = os.path.dirname(helpers.gdb_queue_scripts[0])
    assert not os.path.exists(os.path.join(workdir, "marker"))


async def test_inject_falls_back_when_gdb_cannot_queue(helpers):
    connected = asyncio.Event()
    helpers.on_fallback = connected.set
    assert await inject(TARGET_OLD, 5678, connected) == "debugpy injection"
    assert helpers.gdb_queue_scripts


async def test_inject_never_signals_gdb_while_queueing(
    helpers, short_timeouts, tmp_path, monkeypatch
):
    monkeypatch.setattr(f"{MODULE}._GDB_QUEUE_TIMEOUT", 0.2)
    path = tmp_path / "gdb-pid"
    helpers.gdb_queue = recording_pid(path, 3)
    with pytest.raises(PidAttachError, match="still attached"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    pid = await read_pid(path)
    assert alive(pid)
    os.kill(pid, 9)
    # A second gdb can't attach, and would report that as a permissions error.
    assert helpers.debugpy_calls == []


async def test_inject_never_falls_back_once_quitting(helpers, short_timeouts):
    # The fallback stops the target; quitting has no use for it, and on a
    # main thread waiting for the GIL it can deadlock.
    helpers.gdb_queue = QUEUED
    quitting = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, quitting.set)
    with pytest.raises(PidAttachCancelled):
        await inject(TARGET_OLD, 5678, asyncio.Event(), quitting)
    assert helpers.debugpy_calls == []


async def test_inject_queue_wait_respects_the_deadline(helpers, monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_REMOTE_EXEC", 30.0)
    monkeypatch.setattr(_timeouts, "PID_ATTACH_TOTAL", 0.5)
    helpers.gdb_queue = QUEUED
    started = time.monotonic()
    with pytest.raises(PidAttachError):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert time.monotonic() - started < 5


async def test_inject_no_fallback_once_past_the_deadline(helpers, monkeypatch):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_TOTAL", 0.2)
    helpers.gdb_queue = QUEUED
    with pytest.raises(PidAttachError, match="did not"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert helpers.debugpy_calls == []


async def test_inject_no_fallback_when_the_script_owns_the_attempt(
    helpers, monkeypatch
):
    monkeypatch.setattr(_timeouts, "PID_ATTACH_REMOTE_EXEC", 0.1)
    monkeypatch.setattr(_timeouts, "PID_ATTACH_TOTAL", 0.4)

    def fired(pid, script_path):
        os.rename(
            os.path.join(os.path.dirname(script_path), "marker"),
            os.path.join(os.path.dirname(script_path), "claimed"),
        )
        return QUEUED

    monkeypatch.setattr(f"{MODULE}._gdb_queue_argv", fired)
    with pytest.raises(PidAttachError, match="did not connect"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert helpers.debugpy_calls == []


async def test_inject_gdb_queue_output_reaches_the_diagnosis(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.uses_musl", lambda pid: True)
    helpers.gdb_queue = fail_with("ptrace: Operation not permitted.")
    with pytest.raises(PidAttachError, match="permission denied"):
        await inject(TARGET_OLD, 5678, asyncio.Event())


async def test_inject_musl_without_gdb_says_install_gdb(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.uses_musl", lambda pid: True)
    monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: None)
    with pytest.raises(PidAttachError, match="install gdb"):
        await inject(TARGET_OLD, 5678, asyncio.Event())


async def test_inject_gdb_queue_skipped_with_remote_exec(helpers):
    connected = asyncio.Event()
    connect_soon(connected)
    await inject(TARGET_314, 5678, connected)
    assert helpers.gdb_queue_scripts == []


async def test_inject_gdb_queue_used_when_remote_exec_errors(helpers):
    helpers.remote_exec = fail_with("Remote debugging is not enabled")
    helpers.gdb_queue = QUEUED
    connected = asyncio.Event()
    connect_soon(connected, 0.3)
    assert await inject(TARGET_314, 5678, connected) == "gdb pending call"


async def test_inject_queues_through_gdb_on_musl(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.uses_musl", lambda pid: True)
    helpers.gdb_queue = QUEUED
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_OLD, 5678, connected) == "gdb pending call"


def _run_queued(path, prelude: str = "") -> subprocess.CompletedProcess:
    program = f"{prelude}\nexec({_queued_code(str(path))!r})\nprint('survived')\n"
    return subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )


@pytest.mark.parametrize(
    "script",
    ("raise ValueError('boom')", "raise SystemExit(3)", "raise KeyboardInterrupt", "(("),
)
def test_queued_code_cannot_fail(tmp_path, script):
    path = tmp_path / "script.py"
    path.write_text(script)
    result = _run_queued(path)
    assert result.stdout.split() == ["survived"]
    assert result.stderr == ""


def test_queued_code_survives_a_missing_script(tmp_path):
    result = _run_queued(tmp_path / "gone.py")
    assert result.stdout.split() == ["survived"]
    assert result.stderr == ""


def test_queued_code_ignores_names_in_the_program(tmp_path):
    path = tmp_path / "script.py"
    marker = tmp_path / "ran"
    path.write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()")
    result = _run_queued(path, "from os import open\nexec_ = exec\nbytes = None")
    assert result.stdout.split() == ["survived"]
    assert marker.exists()


def test_queued_code_runs_outside_main_globals(tmp_path):
    path = tmp_path / "script.py"
    path.write_text("left_behind = 1")
    program = (
        f"exec({_queued_code(str(path))!r})\n"
        "print('left_behind' in globals())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=10
    )
    assert result.stdout.split() == ["False"]


def test_gdb_commands_never_allocate_in_the_target():
    commands = _gdb_queue_commands("print(1)")
    assert "mmap" in commands
    assert "strdup" not in commands
    assert "malloc" not in commands
    assert '"' not in commands.replace('"tdb-queued %d\\n"', "")


def test_gdb_commands_write_every_byte_and_a_terminator():
    commands = _gdb_queue_commands("ab")
    assert "= 97" in commands
    assert "= 98" in commands
    assert "($tdb_code + 2) = 0" in commands


# --- a target run by another user ----------------------------------------


def _as_root(monkeypatch, tmp_path, owner):
    (tmp_path / "4242").mkdir()
    real_stat = os.stat
    chowned = []

    def fake_stat(path, *args, **kwargs):
        if str(path).endswith("/4242"):
            return Mock(st_uid=owner)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(f"{MODULE}._PROC", str(tmp_path))
    monkeypatch.setattr(f"{MODULE}.os.geteuid", lambda: 0)
    monkeypatch.setattr(f"{MODULE}.os.stat", fake_stat)
    monkeypatch.setattr(
        f"{MODULE}.os.chown", lambda path, uid, gid: chowned.append((path, uid))
    )
    return chowned


def test_root_hands_files_to_the_targets_user(monkeypatch, tmp_path):
    chowned = _as_root(monkeypatch, tmp_path, 1000)
    _hand_to_target("/some/dir", 4242)
    assert chowned == [("/some/dir", 1000)]


def test_target_owned_by_root_needs_no_handing_over(monkeypatch, tmp_path):
    chowned = _as_root(monkeypatch, tmp_path, 0)
    _hand_to_target("/some/dir", 4242)
    assert chowned == []


def test_other_users_never_chown(monkeypatch, tmp_path):
    chowned = _as_root(monkeypatch, tmp_path, 1000)
    monkeypatch.setattr(f"{MODULE}.os.geteuid", lambda: 1000)
    _hand_to_target("/some/dir", 4242)
    assert chowned == []


def marks_ran(ran_marker: str) -> list[str]:
    """A helper that exits at once, as gdb does once the code has run."""
    return [sys.executable, "-c", f"open({ran_marker!r}, 'w').close()"]


async def test_inject_waits_for_target_after_helper_exits(helpers):
    helpers.debugpy = marks_ran
    connected = asyncio.Event()
    connect_soon(connected, 2.5)  # later than the clean-exit grace
    assert await inject(TARGET_OLD, 5678, connected) == "debugpy injection"


async def test_inject_reports_code_that_ran_but_never_connected(
    helpers, short_timeouts
):
    helpers.debugpy = marks_ran
    with pytest.raises(PidAttachError, match="did not connect"):
        await inject(TARGET_OLD, 5678, asyncio.Event())


async def test_inject_clean_exit_without_running_is_not_python(helpers):
    with pytest.raises(PidAttachError, match="Python"):
        await inject(TARGET_OLD, 5678, asyncio.Event())


async def test_inject_gdb_missing_on_linux(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.shutil.which", lambda name: None)
    with pytest.raises(PidAttachError, match="gdb"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert helpers.debugpy_calls == []


async def test_inject_never_runs_gdb_on_musl(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.uses_musl", lambda pid: True)
    with pytest.raises(PidAttachError, match="musl"):
        await inject(TARGET_OLD, 5678, asyncio.Event())
    assert helpers.debugpy_calls == []


async def test_inject_tries_remote_exec_on_musl(helpers, monkeypatch):
    monkeypatch.setattr(f"{MODULE}.sys.platform", "linux")
    monkeypatch.setattr(f"{MODULE}.uses_musl", lambda pid: True)
    connected = asyncio.Event()
    connect_soon(connected)
    assert await inject(TARGET_314, 5678, connected) == "sys.remote_exec"


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
