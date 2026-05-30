"""Typed registry of TUI modal singletons + transient UI flags owned by TdbApp.

The three "inspection" modals (Threads / Processes / Async Tasks) are
push_screen'd at most one at a time and the App needs a live reference
to the instance for follow-up routing — async detail-load completions,
periodic refreshes arriving from the controller, the processes cache
write on dismiss. Previously these lived as ad-hoc
`self.app._processes_modal` attributes assigned outside `__init__`,
never cleared on dismiss, and read via `getattr(self.app, "_X", None)`.
That pattern hid lifecycle gaps (a dismissed modal's reference
persisted until the next open replaced it).

This holder makes the App's modal surface declarative:
  - typed Optional fields, declared in TdbApp.__init__
  - explicit `clear()` for session-restart cleanup
  - call sites set / read directly: no `getattr`, no `hasattr`

`exception_modal_shown` lives here too because it's the same shape of
"ephemeral UI state owned by App that several handlers need to read".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdb.widgets.async_tasks_modal import AsyncTasksModal
    from tdb.widgets.processes_modal import ProcessesModal
    from tdb.widgets.threads_modal import ThreadsModal


@dataclass
class UIPanels:
    """Live references to TdbApp's modal singletons + ephemeral UI flags."""

    threads: "ThreadsModal | None" = None
    processes: "ProcessesModal | None" = None
    async_tasks: "AsyncTasksModal | None" = None
    # True after `_show_exception_modal` opens the traceback modal for
    # the current stop. Reset on `on_continued`. Distinct from the modal
    # references above because the traceback modal is dismissed via
    # Textual's screen stack and we don't track its instance — this
    # flag just records "did we already react to the current stop?".
    exception_modal_shown: bool = False
    # Cached payload of the most recent `_TracebackModal` so the user
    # can re-summon it with `e` (CodeView DEBUG mode) after dismissal.
    # Cleared on session restart, not on `on_continued` — once seen,
    # the user may want to revisit it later in the same session.
    last_exception_text: str | None = None
    last_frames_text: str | None = None
    last_can_restart: bool = False

    def clear(self) -> None:
        """Drop all modal refs and reset transient flags. Called on
        session restart so stale modal instances aren't kept alive."""
        self.threads = None
        self.processes = None
        self.async_tasks = None
        self.exception_modal_shown = False
        self.last_exception_text = None
        self.last_frames_text = None
        self.last_can_restart = False
