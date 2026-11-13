"""pty driver for the TUI -> detach relay handover, reused per language.

Spawns `python -m tdb ...` on a pty with its own session (so Ctrl-C on
the pty reaches tdb and the program like a real terminal), waits for the
TUI's entry stop, plays the keys, then feeds stdin / Ctrl-C and collects
everything until tdb exits.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Sequence

from .conftest import SRC_DIR

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]")
_KEY_DELAY = 0.4


def _tree(root: int) -> list[int]:
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        found.append(pid)
        with contextlib.suppress(OSError):
            for task in os.listdir(f"/proc/{pid}/task"):
                with open(f"/proc/{pid}/task/{task}/children") as f:
                    todo += [int(child) for child in f.read().split()]
    return found


def _become_session_leader() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class _Pty:
    def __init__(self, master: int) -> None:
        self.master = master
        self.buf = b""

    def pump(self, wait: float = 0.05) -> bool:
        """Read what is available; False once the pty is closed."""
        if select.select([self.master], [], [], wait)[0]:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return False
            if not data:
                return False
            self.buf += data
        return True

    def text(self) -> str:
        return _ANSI.sub("", self.buf.decode("utf-8", errors="replace"))

    def wait_for(self, needle: str, deadline: float, proc: subprocess.Popen) -> bool:
        while time.monotonic() < deadline:
            if needle in self.text():
                return True
            if not self.pump() and proc.poll() is not None:
                return needle in self.text()
        return False

    def send(self, data: bytes, settle: float = 0.0) -> None:
        os.write(self.master, data)
        end = time.monotonic() + settle
        while time.monotonic() < end:
            self.pump()


def run_detached(
    argv: Sequence[str],
    *,
    keys: bytes = b"qd",
    stdin_lines: Sequence[str] = (),
    interrupt_after: float | None = None,
    timeout: float = 30,
    cwd: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
    entry: str = "Paused",
) -> tuple[str, int]:
    """Returns (terminal output with escape codes removed, tdb's exit code)."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    tdb = subprocess.Popen(
        [sys.executable, "-P", "-m", "tdb", *argv],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=cwd,
        env={
            **os.environ,
            "PYTHONPATH": str(SRC_DIR),
            "TERM": "xterm-256color",
            **(env or {}),
        },
        preexec_fn=_become_session_leader,
    )
    os.close(slave)
    term = _Pty(master)
    family: list[int] = []
    deadline = time.monotonic() + timeout
    try:
        assert term.wait_for(entry, deadline, tdb), f"no entry stop:\n{term.text()[-2000:]}"
        family = _tree(tdb.pid)
        for key in keys:
            term.send(bytes([key]), settle=_KEY_DELAY)
        detached = b"d" in keys.lower() and keys.lower().rfind(b"d") > keys.lower().find(b"q")
        if detached and (stdin_lines or interrupt_after is not None):
            assert term.wait_for("tdb: detached", deadline, tdb), term.text()[-2000:]
            for line in stdin_lines:
                term.send(line.encode() + b"\n", settle=0.3)
            if interrupt_after is not None:
                assert term.wait_for("looping-now", deadline, tdb), term.text()[-2000:]
                term.send(b"", settle=interrupt_after)
                term.send(b"\x03")
        while tdb.poll() is None and time.monotonic() < deadline:
            term.pump()
        assert tdb.poll() is not None, f"tdb did not exit:\n{term.text()[-2000:]}"
        while term.pump(0.2):
            pass
        return term.text(), tdb.returncode
    finally:
        os.close(master)
        everyone = family + _tree(tdb.pid)
        if tdb.poll() is not None:
            # tdb is gone: whatever it started should follow it promptly.
            grace = time.monotonic() + 5
            while time.monotonic() < grace and any(map(_alive, family)):
                time.sleep(0.1)
        leftovers[:] = [pid for pid in family if _alive(pid)]
        for pid in reversed(everyone):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        tdb.wait()


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


# Processes from the last run_detached (seen under tdb at its entry stop)
# that were still alive 5s after tdb exited.
leftovers: list[int] = []
