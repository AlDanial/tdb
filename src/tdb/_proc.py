"""Owned process groups: stop a child tdb started in its own session
together with everything it started, with every wait bounded.

A backend that spawns its debugger with ``start_new_session=True`` owns
that whole process group. Checking only the leader is not enough: the
leader (rdbg, pwsh, perl) can exit while the debuggee or a helper it
started lives on holding our pipes, and asyncio's ``Process.wait()``
waits for those pipes too. So the group is signalled by its id whether
or not the leader has exited (ESRCH just means it is already empty),
and each wait has a deadline before escalating to SIGKILL.

Windows has no process groups to signal; there the leader alone is
terminated and then killed, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

log = logging.getLogger(__name__)

_POLL = 0.05


def signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """Send `sig` to `proc`'s whole process group (POSIX), or to `proc`
    alone on Windows. Never raises for an already-gone target."""
    if os.name == "nt":
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                if sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, sig)


def _group_alive(pgid: int) -> bool:
    if os.name == "nt":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


async def _settle(proc: asyncio.subprocess.Process, timeout: float) -> bool:
    """Wait up to `timeout` for the leader to be reaped and the group to
    empty. True when both happened."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), max(timeout, 0))
    while _group_alive(proc.pid):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_POLL, remaining))
    if proc.returncode is None:
        # Group is empty, so only the pipes are left: a bounded last look.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), max(deadline - loop.time(), _POLL))
    return proc.returncode is not None


async def stop_group(
    proc: asyncio.subprocess.Process, *, grace: float = 2.0, kill_grace: float = 2.0
) -> None:
    """SIGTERM `proc`'s group, give it `grace` seconds, then SIGKILL what
    is left and wait at most `kill_grace` more. Never raises, never
    waits longer than ``grace + kill_grace`` (plus polling slack)."""
    if os.name == "nt" and proc.returncode is not None:
        return
    signal_group(proc, signal.SIGTERM)
    if await _settle(proc, grace):
        return
    signal_group(proc, signal.SIGKILL)
    if not await _settle(proc, kill_grace):
        log.warning(
            "process group %s still not gone %.1fs after SIGKILL", proc.pid, kill_grace
        )
