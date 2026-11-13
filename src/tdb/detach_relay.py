"""Relay loop that runs after the TUI closes with the program still alive.

Keeps the debuggee running unattended: every stop is answered with a
continue, the terminal's stdin is forwarded to the program (when the
session has a stdin route), and Ctrl-C is delivered to the program
instead of reopening the debugger. Returns tdb's exit code once the
program exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING, Any, BinaryIO

from tdb.run_mode import final_exit_code

if TYPE_CHECKING:
    from tdb.run_mode import ConsoleRunHandler

log = logging.getLogger(__name__)

INTERRUPT_EXIT = 130

_READ_SIZE = 65536


def _flush_pending_input(stdin: BinaryIO) -> None:
    """Drop keystrokes typed while the TUI was closing, so they are not
    delivered to the program as input."""
    try:
        if not stdin.isatty():
            return
        import termios

        termios.tcflush(stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError, ValueError, AttributeError):
        return
    except Exception as exc:  # termios.error is not an OSError subclass
        if type(exc).__name__ != "error":
            raise


def _start_stdin_reader(
    stdin: BinaryIO, loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[str | None]"
) -> None:
    """Daemon thread: stdin chunks -> queue (None marks EOF)."""

    def put(item: "str | None") -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # loop already closed
            pass

    def reader() -> None:
        while True:
            try:
                chunk = stdin.read1(_READ_SIZE)  # type: ignore[attr-defined]
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                put(None)
                return
            put(chunk.decode("utf-8", errors="replace"))

    threading.Thread(target=reader, name="tdb-detach-stdin", daemon=True).start()


async def _forward_stdin(controller: Any, queue: "asyncio.Queue[str | None]") -> None:
    try:
        while True:
            item = await queue.get()
            if item is None:
                await controller.close_stdin()
                return
            await controller.write_stdin(item)
    except RuntimeError:
        return  # the program closed its stdin; stop forwarding quietly
    except Exception:
        log.debug("stdin forwarding failed", exc_info=True)


def _install_signals(
    loop: asyncio.AbstractEventLoop, queue: "asyncio.Queue[int]"
) -> "list[tuple[int, Any]]":
    """Route SIGINT (and POSIX SIGTERM/SIGHUP) into `queue`. Returns the
    undo list for _restore_signals."""
    undo: list[tuple[int, Any]] = []

    def post(signum: int) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, signum)
        except RuntimeError:
            pass

    if os.name == "nt":
        undo.append((signal.SIGINT, signal.getsignal(signal.SIGINT)))
        signal.signal(signal.SIGINT, lambda signum, _frame: post(signum))
        return undo
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, name)
        undo.append((signum, signal.getsignal(signum)))
        loop.add_signal_handler(signum, queue.put_nowait, int(signum))
    return undo


def _restore_signals(loop: asyncio.AbstractEventLoop, undo: "list[tuple[int, Any]]") -> None:
    for signum, previous in undo:
        with contextlib.suppress(Exception):
            if os.name != "nt":
                loop.remove_signal_handler(signum)
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)


async def _cleanup(controller: Any) -> None:
    try:
        await controller.stop()
    except Exception:
        log.debug("controller.stop() failed during detach cleanup", exc_info=True)


async def relay(
    controller: Any,
    console: "ConsoleRunHandler",
    *,
    stdin: BinaryIO | None = None,
    install_signals: bool = True,
) -> int:
    """Run until the program exits; return tdb's exit code."""
    if console.exited.is_set():
        await _cleanup(controller)
        return final_exit_code(console)

    loop = asyncio.get_running_loop()
    sig_queue: asyncio.Queue[int] = asyncio.Queue()
    undo: list[tuple[int, Any]] = []
    forwarder: asyncio.Task | None = None
    try:
        # Install handlers first so no signal arrives before they exist.
        if install_signals:
            undo = _install_signals(loop, sig_queue)

        if controller.supports_stdin:
            src = stdin if stdin is not None else sys.stdin.buffer
            _flush_pending_input(src)
            in_queue: asyncio.Queue[str | None] = asyncio.Queue()
            forwarder = asyncio.create_task(_forward_stdin(controller, in_queue))
            _start_stdin_reader(src, loop, in_queue)

        while True:
            if console.exited.is_set():
                await _cleanup(controller)
                return final_exit_code(console)
            waiters = {
                asyncio.ensure_future(console.exited.wait()): "exited",
                asyncio.ensure_future(console.stopped.wait()): "stopped",
                asyncio.ensure_future(sig_queue.get()): "signal",
            }
            try:
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waiters:
                    if not task.done():
                        task.cancel()
            # A signal consumed from the queue must be handled even if
            # another event completed in the same pass.
            signums = [t.result() for t in done if waiters[t] == "signal"]
            for signum in signums:
                if signum == signal.SIGINT:
                    if not await controller.interrupt_debuggee():
                        await _cleanup(controller)
                        return INTERRUPT_EXIT
                else:
                    await _cleanup(controller)
                    return 128 + signum
            if console.exited.is_set():
                continue
            if console.stopped.is_set():
                console.stopped.clear()
                await controller.continue_()
    finally:
        if forwarder is not None:
            forwarder.cancel()
        if undo:
            _restore_signals(loop, undo)
