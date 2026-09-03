"""`tdb --run`: headless execution with signal-triggered TUI episodes.

The debuggee runs under the normal adapter session but with no TUI, no
stop-on-entry, and no breakpoints. Debuggee output streams to the
terminal. Ctrl-C (or SIGUSR1 on POSIX) pauses the debuggee and opens
the TUI at the paused line; quitting the TUI can detach back here.
See docs/superpowers/specs/2026-08-15-run-mode-signal-tui-design.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from tdb.session.controller import DebugController
from tdb.session.event_bus import SwappableEventHandler

if TYPE_CHECKING:
    from tdb.languages.base import LanguageProfile
    from tdb.persist import TdbConfig

log = logging.getLogger(__name__)


class ConsoleRunHandler:
    """Event sink for the headless phase of run mode.

    Called synchronously from the DAP read loop (same asyncio loop),
    so setting asyncio.Events here is safe without call_soon_threadsafe.
    """

    def __init__(self) -> None:
        self.initialized = asyncio.Event()
        self.stopped = asyncio.Event()
        self.exited = asyncio.Event()
        self.exit_code: int | None = None
        self.last_stop: tuple[int | None, str, str | None, str | None] | None = None

    def on_initialized(self) -> None:
        self.initialized.set()

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        self.last_stop = (thread_id, reason, description, text)
        self.stopped.set()

    def on_continued(self) -> None:
        self.stopped.clear()

    def on_terminated(self) -> None:
        self.exited.set()

    def on_exited(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.exited.set()

    def on_output(self, text: str, category: str) -> None:
        stream = sys.stderr if category == "stderr" else sys.stdout
        stream.write(text)
        stream.flush()

    def on_external_terminal_started(self) -> None:
        pass


# --- Shared headless-session lifecycle -------------------------------------
# Used by both run mode and eval mode; the invariants here (adapter-not-
# found contract, orphan prevention on escaping exceptions) must stay in
# one place so a fix can't land in one headless mode and miss the other.


async def start_session(controller: DebugController, **start_kwargs) -> int | None:
    """controller.start() with the shared headless-mode contract: a
    missing adapter prints its one-line hint and yields exit code 2.
    Returns the exit code to bail with, or None on success."""
    from tdb.languages.base import AdapterNotFoundError

    try:
        await controller.start(**start_kwargs)
    except AdapterNotFoundError as exc:
        print(f"tdb: {exc.hint}", file=sys.stderr)
        return 2
    return None


@asynccontextmanager
async def stop_session_on_error(controller: DebugController):
    """Guard for everything after a successful start(): the debuggee is
    running under a live adapter session, and any escaping exception
    (timeout waiting for `initialized`, a raising TUI episode, ...) must
    not leave the adapter+debuggee orphaned and detached from the
    terminal's process group — best-effort stop before re-raising."""
    try:
        yield
    except BaseException:
        if not controller.state.is_terminated:
            try:
                await controller.stop()
            except Exception:
                log.exception("cleanup stop failed after headless-mode error")
        raise


async def configure_when_initialized(
    console: ConsoleRunHandler, controller: DebugController
) -> None:
    """Wait for the adapter's `initialized` event, then push breakpoints
    and configurationDone (which unblocks the launch response)."""
    from tdb._timeouts import DAP_INITIALIZED

    await asyncio.wait_for(console.initialized.wait(), timeout=DAP_INITIALIZED)
    await controller.do_configure()


TuiEpisode = Callable[
    [DebugController, SwappableEventHandler, ConsoleRunHandler, "TdbConfig", str],
    Awaitable[bool],
]

# Longer than the interactive pause timeout: nothing else is happening,
# and slow-to-stop debuggees (deep C calls) deserve the extra grace.
_PAUSE_TIMEOUT = 5.0


def _arm_signals(loop: asyncio.AbstractEventLoop, trigger: Callable[[], None]) -> list:
    """Route SIGINT (and SIGUSR1 on POSIX) to `trigger`.

    Returns the list of signals actually armed. Failure (non-main
    thread — embedded use, some test runners) degrades to "no signal
    interruption" rather than crashing run mode.
    """
    installed: list = []
    try:
        if os.name != "nt":
            for sig in (signal.SIGINT, signal.SIGUSR1):
                loop.add_signal_handler(sig, trigger)
                installed.append(sig)
        else:
            signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(trigger))
            installed.append(signal.SIGINT)
    except (ValueError, NotImplementedError, RuntimeError):
        log.warning("cannot install run-mode signal handlers", exc_info=True)
    return installed


def _disarm_signals(
    loop: asyncio.AbstractEventLoop, installed: list, *, ignore: bool
) -> None:
    """Remove run-mode handlers.

    ignore=True while a TUI episode owns the terminal: a stray SIGUSR1
    must be a no-op, not the default action (which kills the process).
    ignore=False on final exit: restore Python defaults.
    """
    for sig in installed:
        if os.name != "nt":
            try:
                loop.remove_signal_handler(sig)
            except (ValueError, RuntimeError):
                pass
        if ignore:
            handler = signal.SIG_IGN
        elif sig == signal.SIGINT:
            handler = signal.default_int_handler
        else:
            handler = signal.SIG_DFL
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


async def _wait_first(*events: asyncio.Event) -> None:
    tasks = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()


async def _default_tui_episode(
    controller: DebugController,
    handler: SwappableEventHandler,
    console: ConsoleRunHandler,
    config: "TdbConfig",
    program: str,
) -> bool:
    from tdb.app import TdbApp

    app = TdbApp(
        program=program,
        config=config,
        profile=controller.profile,
        adopted_controller=controller,
        adopted_handler=handler,
        adopted_stop=console.last_stop,
    )
    await app.run_async()
    return app.detach_and_resume


async def run(
    program: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    just_my_code: bool = True,
    python: str | None = None,
    sub_process: bool = True,
    profile: "LanguageProfile | None" = None,
    config: "TdbConfig | None" = None,
    tui_episode: TuiEpisode | None = None,
    on_session_ready: Callable[[DebugController], None] | None = None,
) -> int:
    """Run `program` headless; signals open TUI episodes. Returns tdb's
    exit code (the debuggee's when it exits during the run phase)."""
    from tdb.persist import load_config

    if config is None:
        config = load_config()
    console = ConsoleRunHandler()
    handler = SwappableEventHandler(console)
    controller = DebugController(handler, profile=profile)
    controller.step_mode = config.step_mode
    controller.adopted_session = True  # restart is never offered in run mode

    bail = await start_session(
        controller,
        program=program,
        args=args,
        cwd=cwd or str(Path.cwd()),
        stop_on_entry=False,
        just_my_code=just_my_code,
        python=python,
        sub_process=sub_process,
    )
    if bail is not None:
        return bail

    async with stop_session_on_error(controller):
        await configure_when_initialized(console, controller)
        if on_session_ready is not None:
            on_session_ready(controller)

        hint = "Ctrl-C" if os.name == "nt" else f"Ctrl-C or `kill -USR1 {os.getpid()}`"
        print(f"tdb: running {program} — {hint} opens the debugger", file=sys.stderr)

        loop = asyncio.get_running_loop()
        interrupt = asyncio.Event()
        episode = tui_episode or _default_tui_episode
        installed = _arm_signals(loop, interrupt.set)
        exit_code = 0
        try:
            while True:
                await _wait_first(console.exited, interrupt, console.stopped)
                if console.exited.is_set():
                    exit_code = console.exit_code or 0
                    break
                if interrupt.is_set() and not console.stopped.is_set():
                    interrupt.clear()
                    ok = await controller.pause(timeout=_PAUSE_TIMEOUT)
                    if console.exited.is_set():
                        # Died between the signal and the pause landing.
                        exit_code = console.exit_code or 0
                        print(
                            f"tdb: program exited (code {exit_code}) before "
                            "the debugger could open",
                            file=sys.stderr,
                        )
                        break
                    if not ok:
                        print(
                            "tdb: pause requested — the program is blocked inside "
                            "a single call; the debugger opens when it returns",
                            file=sys.stderr,
                        )
                        continue
                # Reached on a landed pause, or on a spontaneous stop (a
                # breakpoint set during a previous episode).
                interrupt.clear()
                _disarm_signals(loop, installed, ignore=True)
                detach = await episode(controller, handler, console, config, program)
                handler.retarget(console)
                console.stopped.clear()
                if controller.state.is_terminated:
                    break
                if not detach:
                    await controller.stop()
                    break
                await controller.continue_()
                installed = _arm_signals(loop, interrupt.set)
        finally:
            _disarm_signals(loop, installed, ignore=False)
        return exit_code
