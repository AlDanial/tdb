"""Threads / Processes / AsyncTasks inspection workflows.

These methods drive the three "what is the program doing right now?"
modals. They share a common shape: gate on state.is_terminated /
is_running, fetch info via DAP `evaluate`, populate a modal.

The collaborator holds a reference to the TdbApp so it can:
  - read controller state and call DAP methods,
  - update the menu-bar action labels (Async Tasks (N), Threads (N),
    Processes (N)),
  - push modal screens and store them on the App for cross-handler
    access (e.g., LazyLoadVariables for process child clients).

Worker decorators (`@work`) stay on the App's stub methods because
Textual's worker manager is owned by the App. The stubs are 1-2 lines.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from tdb.dap.types import Source, StackFrame
from tdb.inspection import (
    PROCESS_COLLECT_EXPR,
    ProcessInfo,
    TASK_COLLECT_EXPR,
    TASK_LOCALS_EXPR,
    parse_process_json,
    parse_task_json,
)
from tdb.widgets.async_tasks_modal import AsyncTasksModal
from tdb.widgets.menu_bar import MenuBar
from tdb.widgets.processes_modal import ProcessesModal
from tdb.widgets.threads_modal import ThreadsModal

# Format of each AsyncTaskInfo.stack entry (see tdb/inspection.py
# TASK_COLLECT_EXPR): "<funcname> at <filepath>:<lineno>".
_TASK_FRAME_RE = re.compile(r"^(.+) at (.+):(\d+)$")

if TYPE_CHECKING:
    from tdb.app import TdbApp

log = logging.getLogger(__name__)


class InspectionWorkflows:
    """Drives the threads/processes/async-tasks UI flows."""

    def __init__(self, app: TdbApp) -> None:
        self.app = app

    # --- Async tasks ----------------------------------------------------

    async def fetch_async_task_count(self) -> None:
        """Evaluate asyncio.all_tasks() count and update the menu bar label."""
        ctrl = self.app.controller
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        try:
            result = await ctrl.evaluate_on_parent(
                "len(__import__('asyncio').all_tasks())"
            )
            count = int(result)
            menu_bar = self.app.query_one("#menu-bar", MenuBar)
            menu_bar.update_action_label(
                "async-tasks-label", f"Async Tasks ({count})",
            )
        except Exception:
            log.debug("Could not fetch async task count (program may not use asyncio)")

    async def open_async_tasks(self) -> None:
        """Fetch full task info and open the modal."""
        ctrl = self.app.controller
        if ctrl.state.is_terminated:
            self.app.notify("Program has terminated", title="Async Tasks")
            return
        if ctrl.state.is_running:
            self.app.notify("Program is running — pause first", title="Async Tasks")
            return
        raw = ""
        try:
            raw = await ctrl.evaluate(TASK_COLLECT_EXPR)
            tasks = parse_task_json(raw)
        except Exception:
            log.exception("Error fetching async tasks")
            tasks = []

        if not tasks:
            log.warning(
                "Async task collection returned no tasks. Raw: %s",
                raw[:300] if raw else "(empty)",
            )
            self.app.notify(
                "No asyncio tasks found (program may not use asyncio)",
                title="Async Tasks",
            )
            return

        self.app._async_tasks_modal = AsyncTasksModal(tasks)
        self.app.push_screen(self.app._async_tasks_modal)

    async def refresh_async_tasks(self) -> None:
        ctrl = self.app.controller
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        try:
            raw = await ctrl.evaluate(TASK_COLLECT_EXPR)
            tasks = parse_task_json(raw)
        except Exception:
            log.exception("Error refreshing async tasks")
            return
        if hasattr(self.app, "_async_tasks_modal"):
            self.app._async_tasks_modal.update_tasks(tasks)

    async def load_task_variables(self, task_name: str) -> None:
        """Fetch a task's local variables via DAP and populate the tree."""
        ctrl = self.app.controller
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        if not hasattr(self.app, "_async_tasks_modal"):
            return
        variables: list = []
        try:
            expr = TASK_LOCALS_EXPR.format(task_name=task_name)
            # Route around synthetic frame ids — see
            # controller.resolve_evaluate_frame_id. Without this, after
            # the user has navigated to a task once, state.current_frame_id
            # is negative and debugpy rejects the evaluate request.
            frame_id = await ctrl.resolve_evaluate_frame_id(ctrl.active_client)
            _result, var_ref = await ctrl.active_client.evaluate(
                expr,
                frame_id=frame_id,
                context="repl",
            )
            if var_ref > 0:
                variables = await ctrl.active_client.variables(var_ref)
        except Exception:
            log.debug("Failed to load variables for task %s", task_name)
        self.app._async_tasks_modal.show_task_variables(variables)

    def navigate_to_task(self, task_name: str) -> bool:
        """Populate `state.stack_frames` with synthetic frames built from
        the chosen task's captured coroutine stack.

        Synthetic because the task isn't a live DAP frame; the frames
        carry negative ids so `controller.select_frame` knows to skip
        the DAP scopes fetch. Returns True if frames were installed.
        """
        modal = getattr(self.app, "_async_tasks_modal", None)
        if modal is None:
            return False
        task = next(
            (t for t in modal._tasks if t.name == task_name), None,
        )
        if task is None or not task.stack:
            return False
        synthetic: list[StackFrame] = []
        for i, line_str in enumerate(task.stack):
            m = _TASK_FRAME_RE.match(line_str)
            if not m:
                continue
            func, path, lineno = m.group(1), m.group(2), int(m.group(3))
            synthetic.append(StackFrame(
                id=-1 - i,
                name=func,
                source=Source(path=path, name=os.path.basename(path)),
                line=lineno,
            ))
        if not synthetic:
            return False
        state = self.app.controller.state
        state.stack_frames = synthetic
        state.current_frame_id = synthetic[0].id
        # No live scopes / variables for a suspended task — clear so the
        # Variable view shows empty rather than the previous frame's data.
        state.scopes = []
        state.variables = {}
        return True

    # --- Threads --------------------------------------------------------

    def update_thread_count(self) -> None:
        """Update the Threads label with the current thread count."""
        threads = self.app.controller.state.threads
        menu_bar = self.app.query_one("#menu-bar", MenuBar)
        if len(threads) >= 2:
            menu_bar.update_action_label(
                "threads-label", f"Threads ({len(threads)})",
            )
        else:
            menu_bar.update_action_label("threads-label", "Threads")

    async def open_threads(self) -> None:
        """Fetch threads and open the modal."""
        ctrl = self.app.controller
        if ctrl.state.is_terminated:
            self.app.notify("Program has terminated", title="Threads")
            return
        if ctrl.state.is_running:
            self.app.notify("Program is running — pause first", title="Threads")
            return
        try:
            threads = await ctrl.client.threads()
        except Exception:
            log.exception("Error fetching threads")
            self.app.notify("Failed to fetch threads", title="Threads")
            return
        if not threads:
            self.app.notify("No threads found", title="Threads")
            return
        self.app._threads_modal = ThreadsModal(
            threads, ctrl.state.current_thread_id,
        )
        self.app.push_screen(self.app._threads_modal)

    async def load_thread_detail(self, thread_id: int) -> None:
        """Fetch stack trace and variables for a thread."""
        ctrl = self.app.controller
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        if not hasattr(self.app, "_threads_modal"):
            return
        try:
            frames = await ctrl.client.stack_trace(thread_id)
        except Exception:
            log.debug("Failed to fetch stack trace for thread %d", thread_id)
            frames = []
        scopes: list = []
        variables: dict = {}
        if frames:
            top_frame = frames[0]
            try:
                scopes = await ctrl.client.scopes(top_frame.id)
                for scope in scopes:
                    variables[scope.variables_reference] = (
                        await ctrl.client.variables(scope.variables_reference)
                    )
            except Exception:
                log.debug("Failed to fetch variables for thread %d", thread_id)
        self.app._threads_modal.show_thread_detail(
            thread_id, frames, scopes, variables,
        )

    async def refresh_threads(self) -> None:
        ctrl = self.app.controller
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        try:
            threads = await ctrl.client.threads()
        except Exception:
            log.exception("Error refreshing threads")
            return
        if hasattr(self.app, "_threads_modal"):
            self.app._threads_modal.update_threads(
                threads, ctrl.state.current_thread_id,
            )

    # --- Processes ------------------------------------------------------

    def get_processes_from_pids(self) -> list[ProcessInfo]:
        """Build ProcessInfo list from tracked child PIDs via /proc.

        Linux-only fallback used when DAP `evaluate` against the parent
        is unavailable (e.g., the parent isn't the active process).
        """
        result = []
        for pid in self.app.controller.get_child_pids():
            try:
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if not cmdline_path.exists():
                    continue
                cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode().strip()
                status_path = Path(f"/proc/{pid}/status")
                name = f"Process-{pid}"
                if status_path.exists():
                    for line in status_path.read_text().splitlines():
                        if line.startswith("Name:"):
                            name = line.split(":", 1)[1].strip()
                            break
                result.append(ProcessInfo(
                    name=name,
                    pid=pid,
                    alive=True,
                    exitcode=None,
                    daemon=False,
                    target=cmdline[:200] if cmdline else "unknown",
                    args="()",
                    kwargs="{}",
                    start_method="",
                ))
            except Exception:
                pass
        return result

    async def get_processes(self) -> list[ProcessInfo]:
        """Get child process info, trying eval on parent first, then /proc."""
        try:
            raw = await self.app.controller.evaluate_on_parent(PROCESS_COLLECT_EXPR)
            processes = parse_process_json(raw)
            if processes:
                return processes
        except Exception:
            pass
        return self.get_processes_from_pids()

    async def fetch_process_count(self) -> None:
        """Update the Processes label with child process count."""
        ctrl = self.app.controller
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        # Use tracked PIDs as primary source — always available
        count = len(ctrl.get_child_pids())
        if count == 0:
            # Try eval as fallback (works when parent is the active process)
            try:
                result = await ctrl.evaluate_on_parent(
                    "len(__import__('multiprocessing').active_children())"
                )
                count = int(result)
            except Exception:
                pass
        menu_bar = self.app.query_one("#menu-bar", MenuBar)
        if count >= 2:
            menu_bar.update_action_label(
                "processes-label", f"Processes ({count})",
            )
        else:
            menu_bar.update_action_label("processes-label", "Processes")

    def open_processes_modal(self) -> None:
        """Open the Processes modal immediately (showing Loading...) and let
        the worker fill it in. Returns whether the worker should be spawned.

        Honors the on-disk cache written by `_save_processes_cache` on
        the previous dismiss. The cache is invalidated by
        controller._on_continued, so a hit means we're still in the same
        stopped episode and can restore the full snapshot (process list +
        per-pid stacks/vars) without any DAP round-trips.
        """
        ctrl = self.app.controller
        if ctrl.state.is_terminated:
            self.app.notify("Program has terminated", title="Processes")
            return False
        if ctrl.state.is_running:
            self.app.notify("Program is running — pause first", title="Processes")
            return False

        from tdb import processes_cache
        cached = processes_cache.load()
        if cached is not None and cached["processes"]:
            self.app._processes_modal = ProcessesModal(
                cached["processes"],
                detail_cache=cached["details"],
                current_pid=cached["current_pid"],
            )
            self.app.push_screen(
                self.app._processes_modal,
                callback=lambda _result: self._save_processes_cache(),
            )
            # Worker is unnecessary — the modal already has everything.
            return False

        self.app._processes_modal = ProcessesModal([])
        self.app.push_screen(
            self.app._processes_modal,
            callback=lambda _result: self._save_processes_cache(),
        )
        return True

    def _save_processes_cache(self) -> None:
        """Push-screen callback: serialize whatever the modal accumulated.

        Skipped when the modal was dismissed before populate (no
        processes) or when the program has since stepped/continued —
        the controller already cleared the file in that case, but
        writing again here would resurrect a stale snapshot, so we also
        gate on `state.can_send_dap` standing in for "still paused".
        """
        from tdb import processes_cache
        modal = getattr(self.app, "_processes_modal", None)
        if modal is None or not modal._processes:
            return
        # If we're not paused anymore, _on_continued already cleared the
        # cache; don't write a fresh snapshot that would survive into
        # the next stop with stale frame ids.
        if not self.app.controller.state.can_step:
            return
        processes, details, current_pid = modal.cache_snapshot()
        processes_cache.save(processes, details, current_pid)

    async def open_processes_worker(self) -> None:
        """Background: fetch processes and populate the already-open modal,
        or dismiss it and toast when there are none."""
        processes = await self.get_processes()
        modal = getattr(self.app, "_processes_modal", None)
        if modal is None:
            return
        if not processes:
            modal.dismiss(None)
            self.app.notify("No extra processes", title="Processes")
            return
        modal.update_processes(processes)

    async def refresh_processes(self) -> None:
        ctrl = self.app.controller
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        processes = await self.get_processes()
        if hasattr(self.app, "_processes_modal"):
            self.app._processes_modal.update_processes(processes)

    async def load_process_detail(self, pid: int) -> None:
        """Fetch stack trace and variables for a child process via its DAPClient."""
        if not hasattr(self.app, "_processes_modal"):
            return
        child = self.app.controller.get_child_client(pid)
        if child is None:
            # PIDs from /proc might not exactly match the controller's keys
            self.app._processes_modal.show_process_detail(pid, [], [], {})
            return
        frames: list = []
        scopes: list = []
        variables: dict = {}
        try:
            threads = await child.threads()
            if threads:
                frames = await child.stack_trace(threads[0].id)
                if frames:
                    top_frame = frames[0]
                    scopes = await child.scopes(top_frame.id)
                    for scope in scopes:
                        variables[scope.variables_reference] = (
                            await child.variables(scope.variables_reference)
                        )
        except Exception:
            log.debug("Failed to fetch detail for child process %d", pid)
        self.app._processes_modal.show_process_detail(pid, frames, scopes, variables)
