"""`q` then `d` in the TUI: the Python program keeps running under tdb's
relay (output, stdin, exit code, Ctrl-C). Drives tdb on a pty.

If these prove flaky under `-n auto`, run with `--dist loadgroup`.
"""

from __future__ import annotations

import sys

import pytest

from . import detach_harness
from .conftest import FIXTURES_DIR
from .detach_harness import run_detached

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="needs a pty"),
    pytest.mark.xdist_group("pty"),
]

PROG = str(FIXTURES_DIR / "detach_prog.py")


@pytest.fixture
def env(tmp_path):
    return {"cwd": tmp_path, "env": {"TDB_LOG_DIR": str(tmp_path), "TDB_CONFIG_DIR": str(tmp_path)}}


def test_python_detach_output_stdin_exit_code(env):
    out, code = run_detached([PROG], stdin_lines=["hello"], **env)
    assert "tdb: detached" in out
    assert "after-ran" in out
    assert "got:hello" in out
    assert code == 7
    assert detach_harness.leftovers == []


def test_python_detach_ctrl_c_kills_looping_program(env):
    out, code = run_detached([PROG, "loop"], interrupt_after=1.0, **env)
    assert "looping-now" in out
    assert code == 130
    assert detach_harness.leftovers == []


def test_python_detach_saved_breakpoint_does_not_stop(env):
    out, code = run_detached(["-k", "6", PROG], stdin_lines=["hi"], **env)
    assert "got:hi" in out
    assert code == 7


def test_q_q_still_kills_program(env):
    out, _ = run_detached([PROG], keys=b"qq", **env)
    assert "after-ran" not in out
    assert "tdb: detached" not in out
    assert detach_harness.leftovers == []


def test_q_escape_keeps_tui(env):
    out, code = run_detached([PROG], keys=b"q\x1bqq", **env)
    assert "tdb: detached" not in out
    assert "after-ran" not in out
    assert code == 0


HEARTBEAT_SRC = """\
import pathlib, socket, sys, time
import debugpy

debugpy.listen(("127.0.0.1", {port}))
pathlib.Path({ready!r}).touch()
beat = pathlib.Path({beat!r})
i = 0
while True:
    i += 1
    beat.write_text(str(i))
    time.sleep(0.05)
"""


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def heartbeat_target(tmp_path):
    import subprocess
    import time

    port = _free_port()
    ready, beat = tmp_path / "ready", tmp_path / "beat"
    script = tmp_path / "target.py"
    script.write_text(HEARTBEAT_SRC.format(port=port, ready=str(ready), beat=str(beat)))
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert proc.poll() is None and time.monotonic() < deadline, "target did not start"
            time.sleep(0.05)
        yield proc, port, beat
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_remote_attach_detach_disconnects_and_program_keeps_running(env, heartbeat_target):
    import time

    proc, port, beat = heartbeat_target
    out, code = run_detached(["-r", f"127.0.0.1:{port}"], **env)
    assert code == 0
    assert "keeps running" in out
    assert proc.poll() is None
    first = int(beat.read_text() or 0)
    time.sleep(0.5)
    assert proc.poll() is None
    assert int(beat.read_text() or 0) > first


TERMINAL_PROG = """\
import pathlib, time
beat = pathlib.Path({beat!r})
for i in range(1, 80):
    beat.write_text(str(i))
    time.sleep(0.1)
pathlib.Path({done!r}).write_text("done")
"""


def test_terminal_session_detach_keeps_debugger_attached(env, tmp_path):
    """A launched debugpy `--terminal` session does NOT survive a
    disconnect (measured: the program dies when the adapter goes, even
    reparented to init), so Python's survives_disconnect stays False:
    `qd` must leave tdb attached, silently, until the program finishes."""
    import os

    bindir = tmp_path / "bin"
    bindir.mkdir()
    xterm = bindir / "xterm"
    # Stand-in emulator: runs the command in its own session, like a window.
    xterm.write_text('#!/bin/sh\nshift\nexec setsid "$@" >/dev/null 2>&1 </dev/null\n')
    xterm.chmod(0o755)
    beat, done = tmp_path / "beat", tmp_path / "done"
    prog = tmp_path / "term_prog.py"
    prog.write_text(TERMINAL_PROG.format(beat=str(beat), done=str(done)))
    env["env"]["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
    out, code = run_detached(["--terminal", "xterm", str(prog)], **env)
    assert code == 0, out[-1500:]
    assert "keeps running" not in out
    assert "could not confirm" not in out
    assert done.exists(), "the program must have been allowed to finish"
