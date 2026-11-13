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
import time
from typing import TYPE_CHECKING, Any, BinaryIO

from tdb.run_mode import final_exit_code

if TYPE_CHECKING:
    from tdb.run_mode import ConsoleRunHandler

log = logging.getLogger(__name__)

INTERRUPT_EXIT = 130
# gdb / lldb-dap hide a SIGINT death behind exit 0 / 2. An exit that code
# arrives this soon after tdb delivered Ctrl-C is taken for that death.
SIGINT_DEATH_GRACE = 2.0
# How often the relay checks that the adapter connection is still alive:
# a dead adapter delivers no event, so nothing else would wake the loop.
LIVENESS_POLL = 1.0

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

    # os.read on the raw fd never takes the BufferedReader lock, so a read
    # still blocked at interpreter shutdown cannot abort Python. Objects
    # without a real fd (BytesIO in tests) fall back to read1.
    try:
        fd: int | None = stdin.fileno()
    except (OSError, ValueError, AttributeError):
        fd = None

    def reader() -> None:
        while True:
            try:
                if fd is not None:
                    chunk = os.read(fd, _READ_SIZE)
                else:
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
    # Textual's driver installed SIGTSTP/SIGCONT handlers that it never
    # removes; a stray SIGCONT (gdb sends one when it resumes a program)
    # would re-enter application mode and restart the closed TUI's input
    # thread in the middle of the relay.
    for name in ("SIGTSTP", "SIGCONT"):
        signum = getattr(signal, name)
        undo.append((signum, signal.getsignal(signum)))
        signal.signal(signum, signal.SIG_DFL)
    return undo


def _restore_signals(loop: asyncio.AbstractEventLoop, undo: "list[tuple[int, Any]]") -> None:
    for signum, previous in undo:
        with contextlib.suppress(Exception):
            if os.name != "nt":
                loop.remove_signal_handler(signum)
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)


def _is_terminated(controller: Any) -> bool:
    return bool(getattr(getattr(controller, "state", None), "is_terminated", False))


def _exit_code(
    console: "ConsoleRunHandler",
    interrupted: bool = False,
    sigint_death_codes: tuple[int, ...] = (),
    since_interrupt: float | None = None,
) -> int:
    """tdb's exit code; a program killed by a signal (negative status) maps
    to the shell convention 128+signal. debugpy's launcher reports a
    SIGINT death as -2 truncated to 254, so after we delivered Ctrl-C
    that value also means "interrupted" (130); so do the codes an adapter
    declares it reports for a SIGINT death (gdb and lldb-dap hide the
    signal behind 0 / 2), but only within SIGINT_DEATH_GRACE seconds of the
    delivered Ctrl-C (`since_interrupt`); a later identical code is real."""
    code = final_exit_code(console)
    if os.name == "nt":
        return code
    if code < 0:
        return 128 - code
    if interrupted and code == 256 - signal.SIGINT:
        return INTERRUPT_EXIT
    if (
        code in sigint_death_codes
        and since_interrupt is not None
        and since_interrupt <= SIGINT_DEATH_GRACE
    ):
        return INTERRUPT_EXIT
    return code


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
        return _exit_code(console)

    loop = asyncio.get_running_loop()
    sig_queue: asyncio.Queue[int] = asyncio.Queue()
    undo: list[tuple[int, Any]] = []
    forwarder: asyncio.Task | None = None
    interrupted = False
    interrupted_at: float | None = None
    adapter = getattr(getattr(controller, "profile", None), "adapter", None)
    death_codes = tuple(getattr(getattr(adapter, "quirks", None), "sigint_death_codes", ()))
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
                since = None if interrupted_at is None else time.monotonic() - interrupted_at
                return _exit_code(console, interrupted, death_codes, since)
            waiters = {
                asyncio.ensure_future(console.exited.wait()): "exited",
                asyncio.ensure_future(console.stopped.wait()): "stopped",
                asyncio.ensure_future(sig_queue.get()): "signal",
            }
            try:
                done, _ = await asyncio.wait(
                    waiters, timeout=LIVENESS_POLL, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for task in waiters:
                    if not task.done():
                        task.cancel()
            # A signal consumed from the queue must be handled even if
            # another event completed in the same pass.
            signums = [t.result() for t in done if waiters[t] == "signal"]
            for signum in signums:
                if signum == signal.SIGINT:
                    try:
                        delivered = await controller.interrupt_debuggee()
                    except Exception:
                        log.debug("interrupt_debuggee failed", exc_info=True)
                        delivered = False
                    if not delivered:
                        await _cleanup(controller)
                        return INTERRUPT_EXIT
                    interrupted = True
                    interrupted_at = time.monotonic()
                else:
                    await _cleanup(controller)
                    return 128 + signum
            if console.exited.is_set():
                continue
            if getattr(controller, "connection_lost", False) is True:
                # The adapter is gone and took the session with it; no
                # event will ever say so. Nothing can resume the program.
                sys.stderr.write("tdb: lost the connection to the debug adapter\n")
                sys.stderr.flush()
                await _cleanup(controller)
                return 1
            if console.stopped.is_set():
                console.stopped.clear()
                log.debug("detach relay: auto-continuing after a stop")
                try:
                    await controller.continue_()
                except Exception:
                    log.debug("continue_ failed", exc_info=True)
                    if _is_terminated(controller) and not console.exited.is_set():
                        # The session ended under us without an event
                        # reaching the console; nothing more will arrive.
                        console.exit_code = getattr(
                            controller.state, "last_exit_code", None
                        )
                        console.exited.set()
                    if console.exited.is_set():
                        continue
                    # A program we cannot resume must not be left frozen.
                    await _cleanup(controller)
                    return 1
    finally:
        if forwarder is not None:
            forwarder.cancel()
        if undo:
            _restore_signals(loop, undo)


async def run_after_tui(app: Any, program: str) -> int:
    """Hand a still-live session from the closed TUI to the relay.

    Runs on the loop the TUI ran on, after Textual has restored the
    terminal. Returns tdb's exit code.
    """
    from tdb.run_mode import ConsoleRunHandler, stop_session_on_error

    controller = app.controller
    console = ConsoleRunHandler()
    async with stop_session_on_error(controller):
        state = controller.state
        if state.is_terminated:
            console.exit_code = state.last_exit_code
            console.exited.set()
        app.event_switch.retarget(console)

        if controller.is_remote_attach or controller.used_terminal:
            # The program does not depend on tdb's pipes: nothing to
            # replay, no stdin to forward. detach_disconnect resumes
            # every client itself before disconnecting.
            if not state.is_terminated:
                from tdb.session.controller import DetachNotConfirmed

                name = program or "program"
                try:
                    detached = await controller.detach_disconnect()
                except DetachNotConfirmed:
                    sys.stderr.write(
                        f"tdb: detached \u2014 could not confirm {name} is still running\n"
                    )
                    sys.stderr.flush()
                    return 1
                if detached:
                    sys.stderr.write(f"tdb: detached \u2014 {name} keeps running\n")
                    sys.stderr.flush()
                    return 0
                # Not verified to survive the adapter: hold the session silently.
                if not state.is_running:
                    await controller.continue_()
            return await relay(controller, console)

        if app.program_output_truncated:
            sys.stderr.write("tdb: earlier program output truncated\n")
        for text, category in app.program_output:
            (sys.stderr if category == "stderr" else sys.stdout).write(text)
        sys.stdout.flush()
        sys.stderr.flush()
        sys.stderr.write(f"tdb: detached \u2014 {program} running; Ctrl-C interrupts it\n")
        sys.stderr.flush()

        if not state.is_terminated and not state.is_running:
            await controller.continue_()
        return await relay(controller, console)
