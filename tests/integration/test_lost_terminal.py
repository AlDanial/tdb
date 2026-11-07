"""tdb has to end with its terminal, even when no SIGHUP says it has gone.

A process outside the terminal's foreground group gets none, and neither does
one started with SIGHUP ignored, as `nohup` does. tdb then used to spin.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time

import pytest

from .conftest import SRC_DIR

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs a pty")


def _tree(root):
    found, todo = [], [root]
    while todo:
        pid = todo.pop()
        found.append(pid)
        with contextlib.suppress(OSError):
            for task in os.listdir(f"/proc/{pid}/task"):
                with open(f"/proc/{pid}/task/{task}/children") as f:
                    todo += [int(child) for child in f.read().split()]
    return found


def _ignoring_hangups():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


# "starting" is where a loaded CI runner closed it: Textual's writer thread
# died on the first EIO, its queue filled, and shutdown blocked on it for good.
# "run mode" is a --run TUI opened by a -k breakpoint: nobody can answer its
# detach-or-terminate dialog, so it ends the program.
@pytest.mark.parametrize(
    "settle, options",
    [(0.0, []), (6.0, []), (6.0, ["--run", "-k", "3"])],
    ids=["starting", "running", "run mode"],
)
def test_tdb_and_its_program_end_with_the_terminal(tmp_path, settle, options):
    program = tmp_path / "loop.py"
    program.write_text("import time\nwhile True:\n    time.sleep(0.2)\n")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    tdb = subprocess.Popen(
        [sys.executable, "-P", "-m", "tdb", *options, str(program)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(SRC_DIR),
            "TERM": "xterm-256color",
            "TDB_LOG_DIR": str(tmp_path),
        },
        preexec_fn=_ignoring_hangups,
    )
    os.close(slave)
    family = []
    try:
        started = time.monotonic()
        seen = None
        while time.monotonic() - started < 30:
            if select.select([master], [], [], 0.05)[0]:
                os.read(master, 65536)
            if seen is None and len(_tree(tdb.pid)) > 1:
                seen = time.monotonic()
            if seen is not None and time.monotonic() - seen >= settle:
                break
        family = _tree(tdb.pid)
        assert len(family) > 1, "the session never started"
        os.close(master)
        master = None
        tdb.wait(timeout=20)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            left = [pid for pid in family if os.path.exists(f"/proc/{pid}")]
            if not left:
                break
            time.sleep(0.1)
        assert not left, f"still running: {left}"
    finally:
        if master is not None:
            os.close(master)
        for pid in reversed(family + _tree(tdb.pid)):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        tdb.wait()
