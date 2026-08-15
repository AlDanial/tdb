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
import sys

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
