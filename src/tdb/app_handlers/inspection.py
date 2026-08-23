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
from typing import TYPE_CHECKING

from tdb.dap.types import Source, StackFrame
from tdb.inspection import ProcessInfo
from tdb.session.inspect_service import InspectService, SessionGateError
from tdb.widgets.async_tasks_modal import AsyncTasksModal
from tdb.widgets.menu_bar import MenuBar
from tdb.widgets.processes_modal import ProcessesModal
from tdb.widgets.rust_concurrency_modal import RustConcurrencyModal
from tdb.widgets.threads_modal import ThreadsModal

# Format of each AsyncTaskInfo.stack entry (see tdb/inspection.py
# TASK_COLLECT_EXPR): "<funcname> at <filepath>:<lineno>".
_TASK_FRAME_RE = re.compile(r"^(.+) at (.+):(\d+)$")

if TYPE_CHECKING:
    from tdb.app import TdbApp
    from tdb.dap.types import Thread
    from tdb.languages.base import ThreadDecoration

log = logging.getLogger(__name__)


class InspectionWorkflows:
    """Drives the threads/processes/async-tasks UI flows.

    Fetch-and-parse mechanics live in `tdb.session.inspect_service.
    InspectService` (shared with the JSON-RPC server); this collaborator
    holds only the TUI presentation around them — gates rendered as
    toasts, modal lifecycle, menu-bar labels.
    """

    def __init__(self, app: TdbApp) -> None:
        self.app = app
        self._svc = InspectService(lambda: self.app.controller)

    # --- Async tasks ----------------------------------------------------

    async def fetch_async_task_count(self) -> None:
        """Evaluate asyncio.all_tasks() count and update the menu bar label."""
        ctrl = self.app.controller
        # asyncio.all_tasks() is a Python-only expression; don't evaluate
        # it against a profile that doesn't support task inspection (e.g.
        # cpp) — it would just fail (or misbehave) on every stopped event.
        if not ctrl.profile.capabilities.task_inspection:
            return
        if ctrl.state.is_terminated or ctrl.state.is_running:
            return
        try:
            result = await ctrl.evaluate_on_parent(
                "len(__import__('asyncio').all_tasks())"
            )
            count = int(result)
            menu_bar = self.app.query_one("#menu-bar", MenuBar)
            menu_bar.update_action_label(
                "async-tasks-label",
                f"Async Tasks ({count})",
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
        try:
            tasks = await self._svc.collect_tasks()
        except SessionGateError as e:
            if e.reason == "unsupported":
                self.app.notify(
                    f"Not available for {self.app.controller.profile.display_name}",
                    title="Async Tasks",
                    severity="warning",
                )
            return  # phase changed between our gate and the fetch
        except Exception:
            log.exception("Error fetching async tasks")
            tasks = []

        if not tasks:
            self.app.notify(
                "No asyncio tasks found (program may not use asyncio)",
                title="Async Tasks",
            )
            return

        modal = AsyncTasksModal(tasks)
        self.app.panels.async_tasks = modal
        self.app.push_screen(modal, callback=self._on_async_tasks_dismissed)

    def _on_async_tasks_dismissed(self, _result: object) -> None:
        """Clear the registry slot once the modal is gone."""
        self.app.panels.async_tasks = None

    async def refresh_async_tasks(self) -> None:
        try:
            tasks = await self._svc.collect_tasks()
        except SessionGateError as e:
            if e.reason == "unsupported":
                self.app.notify(
                    f"Not available for {self.app.controller.profile.display_name}",
                    title="Async Tasks",
                    severity="warning",
                )
            return
        except Exception:
            log.exception("Error refreshing async tasks")
            return
        if self.app.panels.async_tasks is not None:
            self.app.panels.async_tasks.update_tasks(tasks)

    async def load_task_variables(self, task_name: str) -> None:
        """Fetch a task's local variables via DAP and populate the tree."""
        modal = self.app.panels.async_tasks
        if modal is None:
            return
        try:
            # Routes around synthetic frame ids and evaluates against the
            # active client — see InspectService.task_locals.
            variables = await self._svc.task_locals(task_name)
        except SessionGateError as e:
            if e.reason == "unsupported":
                self.app.notify(
                    f"Not available for {self.app.controller.profile.display_name}",
                    title="Async Tasks",
                    severity="warning",
                )
            return
        modal.show_task_variables(variables)

    def navigate_to_task(self, task_name: str) -> bool:
        """Populate `state.stack_frames` with synthetic frames built from
        the chosen task's captured coroutine stack.

        Synthetic because the task isn't a live DAP frame; the frames
        are flagged via `state.displayed_frames_are_synthetic` so that
        `controller.select_frame` and `resolve_evaluate_frame_id` know
        to skip / route around them. Returns True if frames were
        installed.
        """
        modal = self.app.panels.async_tasks
        if modal is None:
            return False
        task = next(
            (t for t in modal.items if t.name == task_name),
            None,
        )
        if task is None or not task.stack:
            return False
        synthetic: list[StackFrame] = []
        for i, line_str in enumerate(task.stack):
            m = _TASK_FRAME_RE.match(line_str)
            if not m:
                continue
            func, path, lineno = m.group(1), m.group(2), int(m.group(3))
            synthetic.append(
                StackFrame(
                    id=i,
                    name=func,
                    source=Source(path=path, name=os.path.basename(path)),
                    line=lineno,
                )
            )
        if not synthetic:
            return False
        # Atomic install: stack_frames + current_frame_id + clears
        # scopes/vars + sets displayed_frames_are_synthetic = True.
        self.app.controller.state.set_stack(synthetic, synthetic=True)
        return True

    # --- Threads --------------------------------------------------------

    def update_thread_count(self) -> None:
        """Update the Threads label with the current thread count."""
        threads = self.app.controller.state.threads
        menu_bar = self.app.query_one("#menu-bar", MenuBar)
        if len(threads) >= 2:
            menu_bar.update_action_label(
                "threads-label",
                f"Threads ({len(threads)})",
            )
        else:
            menu_bar.update_action_label("threads-label", "Threads")

    async def _classify_threads(
        self, threads: list[Thread]
    ) -> list[ThreadDecoration] | None:
        """Build ThreadDecorations for `threads` when the active profile
        supports classification (OCaml native domains today), else None.

        Fetches each thread's stack via `thread_frames` (stack only, no
        scopes/variables — cheap). Probe-verified: lldb-dap's thread list
        right after a stop can be briefly incomplete (~1-2s); the modal's
        `r` refresh re-fetches, so we don't poll here.
        """
        ctrl = self.app.controller
        classify = ctrl.profile.capabilities.classify_threads
        if classify is None:
            return None
        stacks = {}
        for t in threads:
            try:
                stacks[t.id] = await self._svc.thread_frames(t.id)
            except Exception:
                log.debug("stack fetch for thread %d failed", t.id)
        return classify(threads, stacks)

    async def open_threads(self) -> None:
        """Fetch threads and open the modal."""
        ctrl = self.app.controller
        if ctrl.state.is_terminated:
            self.app.notify("Program has terminated", title="Threads")
            return
        if ctrl.state.is_running:
            self.app.notify("Program is running — pause first", title="Threads")
            return
        if ctrl.profile.capabilities.concurrency_inspection == "rust":
            if await self.open_rust_concurrency():
                return
            # Snapshot collection failed (probe/collector error, not a
            # session gate): fall through to the generic thread list so a
            # Rust session never loses its threads view entirely.
        try:
            threads = await self._svc.list_threads()
        except SessionGateError:
            return
        except Exception:
            log.exception("Error fetching threads")
            self.app.notify("Failed to fetch threads", title="Threads")
            return
        if not threads:
            self.app.notify("No threads found", title="Threads")
            return
        decorations = await self._classify_threads(threads)
        modal = ThreadsModal(
            threads,
            ctrl.state.current_thread_id,
            frame_name=ctrl.profile.presentation.frame_name,
            decorations=decorations,
        )
        self.app.panels.threads = modal
        self.app.push_screen(modal, callback=self._on_threads_dismissed)

    def _on_threads_dismissed(self, _result: object) -> None:
        self.app.panels.threads = None

    async def open_rust_concurrency(self) -> bool:
        """Collect Rust's immutable wait-graph snapshot and open its workspace.

        Returns False only when snapshot collection itself failed — the
        caller then falls back to the generic Threads modal. Session-gate
        refusals return True: the generic list would be gated identically.
        """
        ctrl = self.app.controller
        try:
            snapshot = await self._svc.collect_rust_concurrency()
        except SessionGateError:
            return True
        except Exception:
            log.exception("Error collecting Rust concurrency")
            self.app.notify(
                "Rust concurrency snapshot failed — showing plain thread list",
                title="Threads",
            )
            return False
        modal = RustConcurrencyModal(snapshot, ctrl.state.current_thread_id)
        self.app.panels.rust_concurrency = modal
        self.app.push_screen(modal, callback=self._on_rust_concurrency_dismissed)
        return True

    def _on_rust_concurrency_dismissed(self, _result: object) -> None:
        self.app.panels.rust_concurrency = None

    async def refresh_rust_concurrency(self) -> None:
        """Replace every Rust workspace tab from one fresh service snapshot."""
        try:
            snapshot = await self._svc.collect_rust_concurrency()
        except SessionGateError:
            # Resume/termination is already closing the workspace; no
            # notification needed for the race.
            return
        except Exception:
            log.exception("Error refreshing Rust concurrency")
            self.app.notify(
                "Refresh failed — showing the previous snapshot",
                title="Rust Concurrency",
            )
            return
        modal = self.app.panels.rust_concurrency
        if modal is not None:
            modal.update_snapshot(snapshot)

    async def load_thread_detail(self, thread_id: int) -> None:
        """Fetch stack trace and variables for a thread."""
        modal = self.app.panels.threads
        if modal is None:
            return
        try:
            frames, scopes, variables = await self._svc.thread_stack(thread_id)
        except SessionGateError:
            return
        except Exception:
            log.debug("Failed to fetch stack trace for thread %d", thread_id)
            frames, scopes, variables = [], [], {}
        modal.show_thread_detail(thread_id, frames, scopes, variables)

    async def load_rust_thread_detail(self, thread_id: int) -> None:
        """Populate a Rust workspace's selected thread with live DAP detail."""
        modal = self.app.panels.rust_concurrency
        if modal is None:
            return
        try:
            frames, scopes, variables = await self._svc.thread_stack(thread_id)
        except SessionGateError:
            return
        except Exception:
            log.debug("Failed to fetch Rust thread detail for %d", thread_id)
            frames, scopes, variables = [], [], {}
        modal.show_thread_detail(thread_id, frames, scopes, variables)

    # --- Full-Contents (variable subtree pre-fetch) --------------------

    async def load_full_variable(self, ref: int, label: str):
        """BFS-materialize the subtree rooted at `ref` for the Full-Contents
        modal. Returns a `FullContentsNode`.

        In post-mortem mode we have no live DAP session — the entire
        variable graph already lives in `state.variables`. Mirror the
        special-case from `on_tdb_app_lazy_load_variables` and serve the
        BFS from that dict instead of the wire.
        """
        from tdb.dap.types import Variable
        from tdb.inspection_full import bfs_load_full

        ctrl = self.app.controller
        state = ctrl.state

        if state.is_post_mortem:

            async def fetch(r: int, start: int, count: int) -> list[Variable]:
                page = state.variables.get(r, [])
                end = start + count if count else len(page)
                return page[start:end]

            return await bfs_load_full(
                fetch,
                root_ref=ref,
                root_label=label,
                cache_writer=None,
            )

        client = ctrl.active_client

        async def fetch(r: int, start: int, count: int) -> list[Variable]:
            return await client.variables(r, start=start, count=count)

        return await bfs_load_full(
            fetch,
            root_ref=ref,
            root_label=label,
            cache_writer=state.variables.__setitem__,
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
        if self.app.panels.threads is not None:
            decorations = await self._classify_threads(threads)
            self.app.panels.threads.update_threads(
                threads,
                ctrl.state.current_thread_id,
                decorations=decorations,
            )

    # --- Processes ------------------------------------------------------

    async def get_processes(self) -> list[ProcessInfo] | None:
        """Get child process info, trying eval on parent first, then /proc.

        Returns `None` for the "unsupported" gate reason — the warning
        toast here already explains why nothing is shown, so callers
        must not layer their own "nothing found" notification on top.
        Any other gate failure (phase changed mid-flight) maps to an
        empty list: callers treat "nothing to show" and "can't look
        right now" the same way in that case.
        """
        try:
            return await self._svc.collect_processes()
        except SessionGateError as e:
            if e.reason == "unsupported":
                self.app.notify(
                    f"Not available for {self.app.controller.profile.display_name}",
                    title="Processes",
                    severity="warning",
                )
                return None
            return []

    async def fetch_process_count(self) -> None:
        """Update the Processes label with child process count."""
        ctrl = self.app.controller
        # The multiprocessing.active_children() eval fallback below is a
        # Python-only expression; don't fire it for profiles that don't
        # support task/process inspection (e.g. cpp).
        if not ctrl.profile.capabilities.task_inspection:
            return
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
                "processes-label",
                f"Processes ({count})",
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
            modal = ProcessesModal(
                cached["processes"],
                detail_cache=cached["details"],
                current_pid=cached["current_pid"],
            )
            self.app.panels.processes = modal
            self.app.push_screen(modal, callback=self._on_processes_dismissed)
            # Worker is unnecessary — the modal already has everything.
            return False

        modal = ProcessesModal([])
        self.app.panels.processes = modal
        self.app.push_screen(modal, callback=self._on_processes_dismissed)
        return True

    def _on_processes_dismissed(self, _result: object) -> None:
        """Push-screen callback: persist the modal's accumulated state to
        the cache file, then drop the registry reference.

        Cache write is skipped when the modal was dismissed before
        populate (no processes) or when the program has since stepped/
        continued — the controller already cleared the file in that
        case, and a fresh write here would resurrect a stale snapshot.
        """
        from tdb import processes_cache

        modal = self.app.panels.processes
        if modal is not None and modal.has_items and self.app.controller.state.can_step:
            processes, details, current_pid = modal.cache_snapshot()
            processes_cache.save(processes, details, current_pid)
        self.app.panels.processes = None

    async def open_processes_worker(self) -> None:
        """Background: fetch processes and populate the already-open modal,
        or dismiss it and toast when there are none."""
        processes = await self.get_processes()
        modal = self.app.panels.processes
        if modal is None:
            return
        if processes is None:
            # Unsupported for this language — get_processes() already
            # explained why via its own warning toast. Dismiss quietly
            # instead of piling on a contradictory "No extra processes".
            modal.dismiss(None)
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
        if processes is None:
            # Unsupported for this language — get_processes() already
            # toasted the reason; nothing to refresh the modal with.
            return
        if self.app.panels.processes is not None:
            self.app.panels.processes.update_processes(processes)

    async def load_process_detail(self, pid: int) -> None:
        """Fetch stack trace and variables for a child process via its DAPClient."""
        modal = self.app.panels.processes
        if modal is None:
            return
        detail = await self._svc.process_stack(pid)
        if detail is None:
            # PIDs from /proc might not exactly match the controller's keys
            modal.show_process_detail(pid, [], [], {})
            return
        frames, scopes, variables = detail
        modal.show_process_detail(pid, frames, scopes, variables)
