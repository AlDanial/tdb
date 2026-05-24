"""Textual Message types for DAP events.

These bridge the DAP read loop (asyncio task) into textual's message system.
Separated into their own module to avoid circular imports between
controller.py and textual_handler.py.
"""

from __future__ import annotations

from textual.message import Message


class DapInitialized(Message):
    """debugpy sent the 'initialized' event."""

    pass


class DapStopped(Message):
    """debugpy sent a 'stopped' event."""

    def __init__(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.reason = reason
        self.description = description
        self.text = text
        super().__init__()


class DapContinued(Message):
    pass


class DapTerminated(Message):
    pass


class DapExited(Message):
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__()


class DapOutput(Message):
    def __init__(self, text: str, category: str) -> None:
        self.text = text
        self.category = category
        super().__init__()


class DapExternalTerminalStarted(Message):
    pass
