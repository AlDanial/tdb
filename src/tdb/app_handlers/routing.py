"""TdbApp message-routing mixin.

Textual auto-dispatches messages by method name: a `FooBar` message
emitted by a `BazWidget` calls `on_baz_widget_foo_bar` on the focused
widget chain ending at the App. Most of TdbApp's on_* handlers are
mechanical forwarders to the App's collaborator objects
(`InspectionWorkflows`, `DapEventCoordinator`) and add no logic. Keeping
them inline in `app.py` bloated that file with ~200 LOC of plumbing.

Moving them to a mixin keeps the routing greppable, type-checkable, and
unit-testable while shrinking `app.py` to genuine orchestration logic.

The mixin assumes the host class (TdbApp) defines:
  - ``self.controller``: DebugController
  - ``self._inspection``: InspectionWorkflows
  - ``self._dap``: DapEventCoordinator
  - ``self.panels``: UIPanels
  - ``self._sync_views_to_top_frame()``: navigates Code/Stack views
plus Textual's normal App surface (`screen`, `query_one`, etc.).

`@work`-decorated coroutines live here too because Textual's worker
manager is owned by the App; decorating a mixin method that becomes
bound at instantiation works the same as decorating it directly on the
App class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work

from tdb.session.messages import (
    DapContinued,
    DapExited,
    DapExternalTerminalStarted,
    DapInitialized,
    DapOutput,
    DapStopped,
    DapTerminated,
)
from tdb.widgets.async_tasks_modal import AsyncTasksModal
from tdb.widgets.code_view import CodeView
from tdb.widgets.processes_modal import ProcessesModal
from tdb.widgets.threads_modal import ThreadsModal

if TYPE_CHECKING:
    from tdb.app_handlers.dap_events import DapEventCoordinator
    from tdb.app_handlers.inspection import InspectionWorkflows
    from tdb.app_handlers.ui_panels import UIPanels
    from tdb.session.controller import DebugController


class _AppMessageRoutes:
    """Mixin providing TdbApp's on_<widget>_<message> message routing."""

    # Type-checker hints so references to host-class attrs type-check.
    if TYPE_CHECKING:
        _inspection: InspectionWorkflows
        _dap: DapEventCoordinator
        controller: DebugController
        panels: UIPanels

        def _sync_views_to_top_frame(self) -> None: ...

    # --- DAP event forwarding -----------------------------------------

    def on_dap_initialized(self, message: DapInitialized) -> None:
        self._do_configure_work()

    @work(exclusive=True, group="configure")
    async def _do_configure_work(self) -> None:
        await self._dap.do_configure()

    async def on_dap_stopped(self, message: DapStopped) -> None:
        await self._dap.on_stopped(message)

    def on_dap_continued(self, message: DapContinued) -> None:
        self._dap.on_continued()

    async def on_dap_terminated(self, message: DapTerminated) -> None:
        await self._dap.on_terminated()

    def on_dap_exited(self, message: DapExited) -> None:
        self._dap.on_exited(message.exit_code)

    def on_dap_external_terminal_started(
        self, message: DapExternalTerminalStarted,
    ) -> None:
        self._dap.on_external_terminal_started()

    def on_dap_output(self, message: DapOutput) -> None:
        self._dap.on_output(message)

    # --- Inspection workers + count badges ----------------------------
    # @work-decorated because they run async work the App's worker
    # manager needs to track for cancellation/exclusivity.

    @work(exclusive=True, group="async-tasks")
    async def _fetch_async_task_count(self) -> None:
        await self._inspection.fetch_async_task_count()

    @work(exclusive=True, group="async-tasks-open")
    async def _open_async_tasks(self) -> None:
        await self._inspection.open_async_tasks()

    def _update_thread_count(self) -> None:
        self._inspection.update_thread_count()

    @work(exclusive=True, group="threads-open")
    async def _open_threads(self) -> None:
        await self._inspection.open_threads()

    @work(exclusive=True, group="process-count")
    async def _fetch_process_count(self) -> None:
        await self._inspection.fetch_process_count()

    def _open_processes(self) -> None:
        # open_processes_modal is sync: it pushes the modal and tells us
        # whether the background fetch worker is needed (False ⇒ served
        # from cache, no DAP round-trip required).
        if self._inspection.open_processes_modal():
            self._open_processes_worker()

    @work(exclusive=True, group="processes-open")
    async def _open_processes_worker(self) -> None:
        await self._inspection.open_processes_worker()

    # --- Modal: AsyncTasks --------------------------------------------

    async def on_async_tasks_modal_load_task_variables(
        self, message: AsyncTasksModal.LoadTaskVariables,
    ) -> None:
        await self._inspection.load_task_variables(message.task_name)

    async def on_async_tasks_modal_select_task(
        self, message: AsyncTasksModal.SelectTask,
    ) -> None:
        if isinstance(self.screen, AsyncTasksModal):  # type: ignore[attr-defined]
            self.screen.dismiss(None)  # type: ignore[attr-defined]
        if self._inspection.navigate_to_task(message.task_name):
            self._sync_views_to_top_frame()
            self.query_one("#code-view", CodeView).focus()  # type: ignore[attr-defined]

    # --- Modal: Threads -----------------------------------------------

    async def on_threads_modal_load_thread_detail(
        self, message: ThreadsModal.LoadThreadDetail,
    ) -> None:
        await self._inspection.load_thread_detail(message.thread_id)

    async def on_threads_modal_refresh_threads(
        self, message: ThreadsModal.RefreshThreads,
    ) -> None:
        await self._inspection.refresh_threads()

    async def on_threads_modal_select_thread(
        self, message: ThreadsModal.SelectThread,
    ) -> None:
        if isinstance(self.screen, ThreadsModal):  # type: ignore[attr-defined]
            self.screen.dismiss(None)  # type: ignore[attr-defined]
        await self.controller.switch_active_thread(message.thread_id)
        self._sync_views_to_top_frame()
        self.query_one("#code-view", CodeView).focus()  # type: ignore[attr-defined]

    # --- Modal: Processes ---------------------------------------------

    async def on_processes_modal_refresh_processes(
        self, message: ProcessesModal.RefreshProcesses,
    ) -> None:
        await self._inspection.refresh_processes()

    async def on_processes_modal_load_process_detail(
        self, message: ProcessesModal.LoadProcessDetail,
    ) -> None:
        await self._inspection.load_process_detail(message.pid)

    async def on_processes_modal_select_process(
        self, message: ProcessesModal.SelectProcess,
    ) -> None:
        if isinstance(self.screen, ProcessesModal):  # type: ignore[attr-defined]
            self.screen.dismiss(None)  # type: ignore[attr-defined]
        await self.controller.switch_active_process(message.pid)
        self._sync_views_to_top_frame()
        self.query_one("#code-view", CodeView).focus()  # type: ignore[attr-defined]
