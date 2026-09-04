"""Live-attach breakpoint: drop into tdb at the call site (pdb.set_trace analog).

Usage from user code:
    import tdb
    tdb.breakpoint()

Or, to replace the builtin `breakpoint()` globally:
    PYTHONBREAKPOINT=tdb.breakpoint python myscript.py

Mechanics:
    First call starts an in-process debugpy server on an ephemeral loopback
    port. Each call spawns `python -m tdb -r <port>` as a subprocess (so the
    TUI takes over the terminal), waits for it to attach, then pauses the
    calling thread via `debugpy.breakpoint()`. tdb auto-steps out of this
    helper on first stop so the user sees their own frame at the call site.
    Stepping and `continue` from tdb resume the calling thread normally;
    quitting tdb disconnects without terminating the program (debugpy
    auto-resumes any paused threads).

No-op if stdin/stdout aren't a tty, mirroring the post-mortem hook.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

log = logging.getLogger(__name__)

# Loopback only — we never want the breakpoint hook exposing a debugpy
# server to the network. Port 0 lets the OS pick a free port on first use;
# _server_port is memoized so subsequent calls reuse the same listener.
_SERVER_HOST = "127.0.0.1"
_server_port: int | None = None
# The most recently spawned tdb subprocess. If it's still alive on a
# repeat tdb.breakpoint() call, we reuse it: that single TUI receives the
# new stopped event over its existing DAP connection. Spawning a second
# tdb here would put two full-screen TUIs on the same tty and they would
# race on rendering and on dismiss/quit keypresses.
_subprocess: subprocess.Popen[bytes] | None = None


def breakpoint(*args: Any, **kwargs: Any) -> None:
    """Pause the caller and open tdb attached to this process."""
    # Args are accepted and ignored for compatibility with the builtin
    # breakpoint() signature (which forwards *args/**kwargs to the hook).

    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
    except (AttributeError, ValueError):
        return

    try:
        import debugpy
    except ImportError:
        log.error(
            "tdb.breakpoint: debugpy is not installed "
            "(install with: uv pip install debugpy)"
        )
        return

    global _server_port, _subprocess
    if _server_port is None:
        try:
            # subProcess=False: otherwise debugpy would try to attach to the
            # tdb subprocess we spawn below, causing tdb to debug itself
            # (the nested-debugging case).
            debugpy.configure(subProcess=False)
            _, _server_port = debugpy.listen((_SERVER_HOST, 0))
            log.info(
                "tdb.breakpoint: debugpy listening on %s:%d",
                _SERVER_HOST,
                _server_port,
            )
        except Exception:
            log.exception("tdb.breakpoint: debugpy.listen failed")
            return

    if _subprocess is not None and _subprocess.poll() is None:
        # Existing tdb is still running and attached — let it handle this
        # stop event. Skip Popen and skip wait_for_client (a client is
        # already connected, so debugpy.breakpoint() pauses immediately).
        debugpy.breakpoint()
        return

    # --no-pause-on-attach: tdb normally pre-arms a `pause` on remote
    # attach so a plain listen()+wait_for_client() program stops at its
    # first statement. Here debugpy.breakpoint() below is the stop, and the
    # pause can fire *inside* that call (after its client-connected check,
    # before it arms its own step). The stop looks fine, but when tdb
    # quits, disconnect resumes the thread, debugpy.breakpoint() finishes
    # arming, and the thread suspends again with nobody attached — the
    # program hangs forever. Seen reliably on Python 3.12+ (sys.monitoring).
    try:
        _subprocess = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tdb",
                "-r",
                f"{_SERVER_HOST}:{_server_port}",
                "--no-pause-on-attach",
            ],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except Exception:
        log.exception("tdb.breakpoint: failed to launch tdb subprocess")
        return

    debugpy.wait_for_client()
    debugpy.breakpoint()
