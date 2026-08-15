"""Event handler protocol for decoupling DebugController from the TUI."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class DebugEventHandler(Protocol):
    """Protocol for receiving DAP events from the DebugController.

    Implementations bridge these calls into whatever system is consuming
    debug events (textual Messages, RPC responses, etc.).

    All methods are called synchronously from the DAP read loop.
    """

    def on_initialized(self) -> None: ...
    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None: ...
    def on_continued(self) -> None: ...
    def on_terminated(self) -> None: ...
    def on_exited(self, exit_code: int) -> None: ...
    def on_output(self, text: str, category: str) -> None: ...
    def on_external_terminal_started(self) -> None: ...


class CompositeEventHandler:
    """Fans out events to multiple handlers (for dual TUI + server mode)."""

    def __init__(self, *handlers: DebugEventHandler) -> None:
        self._handlers = list(handlers)

    def add(self, handler: DebugEventHandler) -> None:
        self._handlers.append(handler)

    def on_initialized(self) -> None:
        for h in self._handlers:
            h.on_initialized()

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        for h in self._handlers:
            h.on_stopped(thread_id, reason, description, text)

    def on_continued(self) -> None:
        for h in self._handlers:
            h.on_continued()

    def on_terminated(self) -> None:
        for h in self._handlers:
            h.on_terminated()

    def on_exited(self, exit_code: int) -> None:
        for h in self._handlers:
            h.on_exited(exit_code)

    def on_output(self, text: str, category: str) -> None:
        for h in self._handlers:
            h.on_output(text, category)

    def on_external_terminal_started(self) -> None:
        for h in self._handlers:
            h.on_external_terminal_started()


class SwappableEventHandler:
    """Delegates every event to a replaceable target handler.

    Run mode (`tdb --run`) alternates between a console-printing
    handler (headless phase) and the TUI's TextualEventHandler
    (interactive episodes) on one live controller; `retarget` is the
    switch. Called synchronously from the DAP read loop, same as any
    other DebugEventHandler.
    """

    def __init__(self, target: DebugEventHandler) -> None:
        self._target = target

    @property
    def target(self) -> DebugEventHandler:
        return self._target

    def retarget(self, new_target: DebugEventHandler) -> DebugEventHandler:
        old, self._target = self._target, new_target
        return old

    def on_initialized(self) -> None:
        self._target.on_initialized()

    def on_stopped(
        self,
        thread_id: int | None,
        reason: str,
        description: str | None = None,
        text: str | None = None,
    ) -> None:
        self._target.on_stopped(thread_id, reason, description, text)

    def on_continued(self) -> None:
        self._target.on_continued()

    def on_terminated(self) -> None:
        self._target.on_terminated()

    def on_exited(self, exit_code: int) -> None:
        self._target.on_exited(exit_code)

    def on_output(self, text: str, category: str) -> None:
        self._target.on_output(text, category)

    def on_external_terminal_started(self) -> None:
        self._target.on_external_terminal_started()
