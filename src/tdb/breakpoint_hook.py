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
    calling thread via `debugpy.breakpoint()`. When the tdb subprocess exits
    (user quits with `q`), this function returns and the user's program
    continues.

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

    global _server_port
    if _server_port is None:
        try:
            # subProcess=False: otherwise debugpy would try to attach to the
            # tdb subprocess we spawn below, causing tdb to debug itself
            # (the nested-debugging case).
            debugpy.configure(subProcess=False)
            _, _server_port = debugpy.listen((_SERVER_HOST, 0))
            log.info(
                "tdb.breakpoint: debugpy listening on %s:%d",
                _SERVER_HOST, _server_port,
            )
        except Exception:
            log.exception("tdb.breakpoint: debugpy.listen failed")
            return

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tdb", "-r", f"{_SERVER_HOST}:{_server_port}"],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    except Exception:
        log.exception("tdb.breakpoint: failed to launch tdb subprocess")
        return

    try:
        debugpy.wait_for_client()
        debugpy.breakpoint()
    finally:
        # Block until tdb fully exits so the tty is handed back cleanly
        # before user code runs the next line.
        try:
            proc.wait()
        except Exception:
            log.exception("tdb.breakpoint: error waiting for tdb subprocess")
