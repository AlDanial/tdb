"""Textual event handler: bridges DebugController events into textual Messages."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .event_bus import DebugEventHandler
from .messages import (
    DapContinued,
    DapExited,
    DapExternalTerminalStarted,
    DapInitialized,
    DapOutput,
    DapStopped,
    DapTerminated,
)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from tdb.app import TdbApp


class TextualEventHandler:
    """Posts textual Messages to a TdbApp for each DAP event."""

    def __init__(self, app: TdbApp) -> None:
        self._app = app

    def on_initialized(self) -> None:
        self._app.post_message(DapInitialized())

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        log.info("TextualEventHandler.on_stopped reason=%s desc=%s text=%s",
                 reason, description, text)
        self._app.post_message(DapStopped(thread_id, reason, description, text))

    def on_continued(self) -> None:
        self._app.post_message(DapContinued())

    def on_terminated(self) -> None:
        self._app.post_message(DapTerminated())

    def on_exited(self, exit_code: int) -> None:
        self._app.post_message(DapExited(exit_code))

    def on_output(self, text: str, category: str) -> None:
        self._app.post_message(DapOutput(text, category))

    def on_external_terminal_started(self) -> None:
        self._app.post_message(DapExternalTerminalStarted())
