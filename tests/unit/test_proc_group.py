"""tdb._proc.stop_group: an owned process group is cleaned up whether or
not its leader already exited, and no wait is unbounded (Codex #4)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

import pytest

from tdb import _proc

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); /proc tells them apart on Linux.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


async def _gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def _spawn(code: str) -> tuple[asyncio.subprocess.Process, int]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    child = int(await asyncio.wait_for(proc.stdout.readline(), 10))
    return proc, child


# The grandchild inherits our pipes and ignores SIGTERM, as a debuggee
# that installed its own handler would.
_STUBBORN_CHILD = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
)


async def test_descendants_die_after_the_leader_already_exited():
    proc, child = await _spawn(
        "import subprocess, sys\n"
        f"c = subprocess.Popen([sys.executable, '-c', {_STUBBORN_CHILD!r}])\n"
        "print(c.pid, flush=True)\n"
    )
    try:
        # Leader exits on its own; its child lives on, holding the pipes.
        assert await _gone(proc.pid)
        started = time.monotonic()
        await asyncio.wait_for(_proc.stop_group(proc, grace=0.5, kill_grace=2), 10)
        assert time.monotonic() - started < 5
        assert await _gone(child)
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)


async def test_stubborn_leader_and_child_holding_pipes_are_killed_in_bounded_time():
    proc, child = await _spawn(
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"c = subprocess.Popen([sys.executable, '-c', {_STUBBORN_CHILD!r}])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    try:
        started = time.monotonic()
        await asyncio.wait_for(_proc.stop_group(proc, grace=0.5, kill_grace=2), 10)
        assert time.monotonic() - started < 5
        assert proc.returncode == -signal.SIGKILL
        assert await _gone(child)
    finally:
        for pid in (child, proc.pid):
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


async def test_cooperative_group_exits_on_sigterm_without_sigkill():
    proc, child = await _spawn(
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(c.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    await asyncio.wait_for(_proc.stop_group(proc, grace=3, kill_grace=2), 10)
    assert proc.returncode == -signal.SIGTERM
    assert await _gone(child)


async def test_already_reaped_empty_group_returns_at_once():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", start_new_session=True
    )
    await proc.wait()
    started = time.monotonic()
    await _proc.stop_group(proc, grace=2, kill_grace=2)
    assert time.monotonic() - started < 0.5


# --- the backends use it (Codex #4 reproductions) ------------------------

_ORPHANING_LEADER = (
    "import subprocess, sys\n"
    f"c = subprocess.Popen([sys.executable, '-c', {_STUBBORN_CHILD!r}])\n"
    "print(c.pid, flush=True)\n"
)


async def _orphaning_leader():
    proc, child = await _spawn(_ORPHANING_LEADER)
    assert await _gone(proc.pid)  # leader exited; child holds the pipes
    return proc, child


async def _check_cleanup(cleanup, child: int) -> None:
    try:
        started = time.monotonic()
        await asyncio.wait_for(cleanup(), 15)
        assert time.monotonic() - started < 8
        assert await _gone(child), "descendant outlived its exited leader"
    finally:
        if _alive(child):
            os.kill(child, signal.SIGKILL)


async def test_ruby_cleanup_kills_descendants_of_an_exited_rdbg():
    from tdb.adapters.ruby.server import RubyDapServer

    from .test_ruby_proxy_units import SinkWriter

    server = RubyDapServer(asyncio.StreamReader(), SinkWriter())
    server._proc, child = await _orphaning_leader()
    await _check_cleanup(lambda: server._ensure_rdbg_dead(grace=0.5), child)


async def test_powershell_cleanup_kills_descendants_of_an_exited_pwsh():
    from tdb.adapters.powershell.server import PowerShellDapServer

    server = PowerShellDapServer(asyncio.StreamReader(), None)
    server._proc, child = await _orphaning_leader()
    await _check_cleanup(lambda: server._ensure_pwsh_dead(grace=0.5), child)


async def test_perl_stop_is_bounded_when_a_descendant_holds_the_pipes():
    from tdb.adapters.perl.session import PerlSession

    session = PerlSession(on_output=lambda *a: None, on_stop=lambda: None)
    # Leader still running (perl5db), its child holding the pipes.
    proc, child = await _spawn(_ORPHANING_LEADER + "import time; time.sleep(60)\n")
    session._process = proc
    await _check_cleanup(session.stop, child)
