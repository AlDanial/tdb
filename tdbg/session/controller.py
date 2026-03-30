"""Debug session controller: bridges DAP client and TUI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from textual.message import Message

from tdbg.dap.client import DAPClient
from tdbg.dap.messages import Event
from tdbg.dap.types import SourceBreakpoint
from .state import DebugState

if TYPE_CHECKING:
    from tdbg.app import TdbgApp

log = logging.getLogger(__name__)


# --- Messages posted from DAP event handlers to the app ---
# These bridge the DAP read loop (asyncio task) into textual's message system.

class DapInitialized(Message):
    """debugpy sent the 'initialized' event."""
    pass

class DapStopped(Message):
    """debugpy sent a 'stopped' event."""
    def __init__(self, thread_id: int | None, reason: str) -> None:
        self.thread_id = thread_id
        self.reason = reason
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


class DebugController:
    """Orchestrates the debug session between DAP client and the TUI app."""

    def __init__(self, app: TdbgApp) -> None:
        self.app = app
        self.client = DAPClient()
        self.state = DebugState()

    def _setup_event_handlers(self) -> None:
        self.client.on_event("stopped", self._on_stopped)
        self.client.on_event("continued", self._on_continued)
        self.client.on_event("terminated", self._on_terminated)
        self.client.on_event("exited", self._on_exited)
        self.client.on_event("output", self._on_output)
        self.client.on_event("initialized", self._on_initialized)

    async def start(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        stop_on_entry: bool = False,
        just_my_code: bool = True,
        python: str | None = None,
    ) -> None:
        """Start the debug session.

        DAP sequence:
        1. initialize → response
        2. launch (fire, don't await — debugpy holds the response)
        3. debugpy sends 'initialized' event
        4. on_dap_initialized handler sends breakpoints + configurationDone
        5. debugpy finally sends launch response
        """
        self._setup_event_handlers()

        self._launch_params = {
            "program": program,
            "args": args,
            "cwd": cwd or str(Path(program).parent),
            "stop_on_entry": stop_on_entry,
            "just_my_code": just_my_code,
            "python": python,
        }

        await self.client.start(python=python)
        await self.client.initialize()

        # Send launch — don't await response (debugpy holds it until configurationDone)
        p = self._launch_params
        self._launch_future = await self.client.launch(
            program=p["program"],
            args=p["args"],
            cwd=p["cwd"],
            stop_on_entry=p["stop_on_entry"],
            just_my_code=p["just_my_code"],
            python=p["python"],
        )

    async def do_configure(self) -> None:
        """Called by app after 'initialized' event. Sends breakpoints + configurationDone.

        This unblocks the launch response from debugpy.
        """
        # Send breakpoints
        for source_path, bps in self.state.breakpoints.items():
            await self.client.set_breakpoints(source_path, bps)

        # Signal configuration complete — this unblocks the launch response
        await self.client.configuration_done()

        # Now await the launch response
        response = await asyncio.wait_for(self._launch_future, timeout=30.0)
        if not response.success:
            raise Exception(f"Launch failed: {response.message}")

        self.state.is_ready = True
        self.state.is_running = True

    async def stop(self) -> None:
        await self.client.disconnect(terminate=True)
        await self.client.stop()
        self.state.is_terminated = True

    # --- Debug actions ---

    async def continue_(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            await self.client.continue_(self.state.current_thread_id)

    async def step_over(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            await self.client.next(self.state.current_thread_id)

    async def step_in(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            await self.client.step_in(self.state.current_thread_id)

    async def step_out(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            await self.client.step_out(self.state.current_thread_id)

    async def pause(self) -> None:
        if self.state.current_thread_id is not None:
            await self.client.pause(self.state.current_thread_id)

    async def toggle_breakpoint(self, source_path: str, line: int) -> None:
        bps = self.state.breakpoints.get(source_path, [])
        existing = [bp for bp in bps if bp.line == line]
        if existing:
            bps = [bp for bp in bps if bp.line != line]
        else:
            bps.append(SourceBreakpoint(line=line))
        self.state.breakpoints[source_path] = bps

        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, bps)

    async def set_breakpoint_condition(
        self, source_path: str, line: int, condition: str | None
    ) -> None:
        bps = self.state.breakpoints.get(source_path, [])
        for bp in bps:
            if bp.line == line:
                bp.condition = condition
                break
        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, bps)

    async def evaluate(self, expression: str) -> str:
        try:
            result, _ = await self.client.evaluate(
                expression,
                frame_id=self.state.current_frame_id,
                context="repl",
            )
            return result
        except Exception as e:
            return str(e)

    async def select_frame(self, frame_id: int) -> None:
        self.state.current_frame_id = frame_id
        await self.fetch_scopes_and_variables(frame_id)

    async def fetch_stop_info(self) -> None:
        """After stopping, fetch threads, stack trace, scopes, and variables."""
        try:
            self.state.threads = await self.client.threads()

            if self.state.current_thread_id is not None:
                self.state.stack_frames = await self.client.stack_trace(
                    self.state.current_thread_id
                )
                if self.state.stack_frames:
                    top_frame = self.state.stack_frames[0]
                    self.state.current_frame_id = top_frame.id
                    await self.fetch_scopes_and_variables(top_frame.id)
        except Exception:
            log.exception("Error fetching stop info")

    async def fetch_scopes_and_variables(self, frame_id: int) -> None:
        self.state.scopes = await self.client.scopes(frame_id)
        self.state.variables.clear()
        for scope in self.state.scopes:
            variables = await self.client.variables(scope.variables_reference)
            self.state.variables[scope.variables_reference] = variables

    # --- DAP event handlers ---
    # Called synchronously from the DAP read loop.
    # They ONLY post textual Messages — all async work happens in app handlers.

    def _on_initialized(self, event: Event) -> None:
        self.app.post_message(DapInitialized())

    def _on_stopped(self, event: Event) -> None:
        thread_id = event.body.get("threadId")
        reason = event.body.get("reason", "unknown")
        self.app.post_message(DapStopped(thread_id, reason))

    def _on_continued(self, event: Event) -> None:
        self.app.post_message(DapContinued())

    def _on_terminated(self, event: Event) -> None:
        self.app.post_message(DapTerminated())

    def _on_exited(self, event: Event) -> None:
        exit_code = event.body.get("exitCode", 0)
        self.app.post_message(DapExited(exit_code))

    def _on_output(self, event: Event) -> None:
        text = event.body.get("output", "")
        category = event.body.get("category", "console")
        self.app.post_message(DapOutput(text, category))
