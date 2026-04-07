"""Debug session controller: bridges DAP client and event consumers."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from tdbg.dap.client import DAPClient
from tdbg.dap.messages import Event, Request
from tdbg.dap.types import SourceBreakpoint
from .event_bus import DebugEventHandler
from .state import DebugState

log = logging.getLogger(__name__)


# --- Terminal emulator detection and command building ---

# Terminals that directly fork the child process (reliable for runInTerminal).
# gnome-terminal is excluded: its D-Bus client/server architecture means the
# client exits immediately and the command may not inherit the environment.
_TERMINAL_CANDIDATES = [
    "xterm",
    "konsole",
    "xfce4-terminal",
    "kitty",
    "alacritty",
    "foot",
]

# Maps terminal basename to the flag(s) used before the command to execute.
# The flag must accept the remaining args as the command + arguments.
_TERMINAL_EXEC_FLAG: dict[str, list[str]] = {
    "xfce4-terminal": ["-x"],  # -x takes rest of args; -e takes single string
    "kitty": [],
    # Default for xterm, konsole, alacritty, foot: ["-e"]
}


def _find_terminal() -> str:
    """Find an available terminal emulator. Returns the full path."""
    # User override
    user_terminal = os.environ.get("TERMINAL")
    if user_terminal:
        path = shutil.which(user_terminal)
        if path:
            return path

    for name in _TERMINAL_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path

    raise RuntimeError(
        "No terminal emulator found. Install xterm, konsole, kitty, "
        "alacritty, or foot — or set the TERMINAL environment variable."
    )


def _build_terminal_cmd(terminal: str, args: list[str]) -> list[str]:
    """Build the command to launch a terminal running the given args."""
    basename = Path(terminal).name
    exec_flag = _TERMINAL_EXEC_FLAG.get(basename, ["-e"])
    return [terminal] + exec_flag + args


class DebugController:
    """Orchestrates the debug session between DAP client and event consumers."""

    def __init__(self, event_handler: DebugEventHandler) -> None:
        self.event_handler = event_handler
        self.client = DAPClient()
        self.state = DebugState()
        self._external_terminal = False
        self._lock = asyncio.Lock()

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
        external_terminal: bool = False,
    ) -> None:
        """Start the debug session.

        DAP sequence:
        1. initialize → response
        2. launch (fire, don't await — debugpy holds the response)
        3. debugpy sends 'initialized' event
        4. on_dap_initialized handler sends breakpoints + configurationDone
        5. debugpy finally sends launch response
        """
        self._external_terminal = external_terminal
        self._setup_event_handlers()

        if external_terminal:
            self.client.on_reverse_request("runInTerminal", self._handle_run_in_terminal)

        self._launch_params = {
            "program": program,
            "args": args,
            "cwd": cwd or str(Path(program).parent),
            "stop_on_entry": stop_on_entry,
            "just_my_code": just_my_code,
            "python": python,
        }

        await self.client.start(python=python)
        await self.client.initialize(support_run_in_terminal=external_terminal)

        # Send launch — don't await response (debugpy holds it until configurationDone)
        p = self._launch_params
        self._launch_future = await self.client.launch(
            program=p["program"],
            args=p["args"],
            cwd=p["cwd"],
            stop_on_entry=p["stop_on_entry"],
            just_my_code=p["just_my_code"],
            python=p["python"],
            console="externalTerminal" if external_terminal else "internalConsole",
        )

    async def _handle_run_in_terminal(self, request: Request) -> dict[str, Any]:
        """Handle the runInTerminal reverse request from debugpy."""
        cmd_args: list[str] = request.arguments.get("args", [])
        cwd = request.arguments.get("cwd")
        env = request.arguments.get("env")

        terminal = _find_terminal()
        full_cmd = _build_terminal_cmd(terminal, cmd_args)

        log.info("Launching external terminal: %s", full_cmd)

        # Merge request env into current env so the debuggee inherits
        # PATH, DISPLAY, etc. needed to connect back to debugpy.
        merged_env = {**os.environ, **(env or {})}

        await asyncio.create_subprocess_exec(
            *full_cmd,
            cwd=cwd,
            env=merged_env,
            start_new_session=True,
        )

        self.event_handler.on_external_terminal_started()

        # Don't return processId — for terminals that fork-and-exit (like
        # gnome-terminal), the PID is meaningless and confuses debugpy.
        return {}

    async def do_configure(self) -> None:
        """Called after 'initialized' event. Sends breakpoints + configurationDone.

        This unblocks the launch response from debugpy.
        """
        # Break on exceptions that crash the program or escape user code.
        # "userUnhandled" avoids spurious stops on internal exceptions like
        # GeneratorExit in traceback.walk_stack.
        await self.client.set_exception_breakpoints(["userUnhandled"])

        # Send breakpoints (skip if disabled)
        if not self.state.breakpoints_disabled:
            for source_path, bps in self.state.breakpoints.items():
                await self.client.set_breakpoints(source_path, bps)

        # Signal configuration complete — this unblocks the launch response
        await self.client.configuration_done()

        # Now await the launch response
        # External terminal needs more time: terminal startup + debuggee connect
        timeout = 60.0 if self._external_terminal else 30.0
        response = await asyncio.wait_for(self._launch_future, timeout=timeout)
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

    async def run_to_cursor(self, source_path: str, line: int) -> None:
        """Set a temporary breakpoint at line, continue, then remove it."""
        if self.state.current_thread_id is None:
            return
        # Add a temporary breakpoint
        bps = list(self.state.breakpoints.get(source_path, []))
        had_bp = any(bp.line == line for bp in bps)
        if not had_bp:
            bps.append(SourceBreakpoint(line=line))
            if self.state.is_ready and not self.state.is_terminated:
                await self.client.set_breakpoints(source_path, bps)
        # Continue execution
        self.state.is_running = True
        self.state.clear_frame_data()
        self._run_to_cursor_cleanup = (source_path, line) if not had_bp else None
        await self.client.continue_(self.state.current_thread_id)

    async def cleanup_run_to_cursor(self) -> None:
        """Remove the temporary breakpoint after stopping."""
        if not hasattr(self, "_run_to_cursor_cleanup") or self._run_to_cursor_cleanup is None:
            return
        source_path, line = self._run_to_cursor_cleanup
        self._run_to_cursor_cleanup = None
        bps = [bp for bp in self.state.breakpoints.get(source_path, []) if bp.line != line]
        self.state.breakpoints[source_path] = bps
        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, bps)

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

    async def add_breakpoint(
        self,
        source_path: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
    ) -> None:
        """Add a breakpoint (idempotent — updates condition if it already exists)."""
        bps = self.state.breakpoints.get(source_path, [])
        existing = next((bp for bp in bps if bp.line == line), None)
        if existing:
            existing.condition = condition
            existing.hit_condition = hit_condition
        else:
            bps.append(SourceBreakpoint(
                line=line, condition=condition, hit_condition=hit_condition,
            ))
            self.state.breakpoints[source_path] = bps

        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(
                source_path, self.state.breakpoints[source_path],
            )

    async def remove_breakpoint(self, source_path: str, line: int) -> None:
        """Remove a breakpoint at the given location (no-op if not set)."""
        bps = self.state.breakpoints.get(source_path, [])
        new_bps = [bp for bp in bps if bp.line != line]
        if len(new_bps) == len(bps):
            return  # nothing to remove
        self.state.breakpoints[source_path] = new_bps

        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, new_bps)

    async def set_breakpoint_condition(
        self,
        source_path: str,
        line: int,
        condition: str | None,
        hit_condition: str | None = None,
    ) -> None:
        bps = self.state.breakpoints.get(source_path, [])
        for bp in bps:
            if bp.line == line:
                bp.condition = condition
                bp.hit_condition = hit_condition
                break
        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, bps)

    async def disable_all_breakpoints(self) -> None:
        """Tell debugpy to remove all breakpoints without clearing them from state."""
        self.state.breakpoints_disabled = True
        if self.state.is_ready and not self.state.is_terminated:
            for source_path in self.state.breakpoints:
                await self.client.set_breakpoints(source_path, [])

    async def enable_all_breakpoints(self) -> None:
        """Re-send all breakpoints to debugpy."""
        self.state.breakpoints_disabled = False
        if self.state.is_ready and not self.state.is_terminated:
            for source_path, bps in self.state.breakpoints.items():
                await self.client.set_breakpoints(source_path, bps)

    async def clear_all_breakpoints(self) -> None:
        """Remove all breakpoints from state and debugpy."""
        if self.state.is_ready and not self.state.is_terminated:
            for source_path in self.state.breakpoints:
                await self.client.set_breakpoints(source_path, [])
        self.state.breakpoints.clear()
        self.state.breakpoints_disabled = False

    async def navigate_stack(self, up: bool) -> bool:
        """Move to the next/previous frame in the call stack.

        Returns True if navigation succeeded, False if at boundary.
        """
        frames = self.state.stack_frames
        if not frames or self.state.current_frame_id is None:
            return False
        idx = next(
            (i for i, f in enumerate(frames) if f.id == self.state.current_frame_id),
            None,
        )
        if idx is None:
            return False
        # up = toward caller (higher index), down = toward callee (lower index)
        new_idx = idx + 1 if up else idx - 1
        if not (0 <= new_idx < len(frames)):
            return False
        new_frame = frames[new_idx]
        self.state.current_frame_id = new_frame.id
        await self.fetch_scopes_and_variables(new_frame.id)
        return True

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
        except Exception:
            log.exception("Error fetching threads")

        if self.state.current_thread_id is not None:
            try:
                self.state.stack_frames = await self.client.stack_trace(
                    self.state.current_thread_id
                )
            except Exception:
                log.exception("Error fetching stack trace")

            if self.state.stack_frames:
                top = self.state.stack_frames[0]
                self.state.current_frame_id = top.id
                try:
                    await self.fetch_scopes_and_variables(top.id)
                except Exception:
                    log.exception("Error fetching scopes/variables")

    async def fetch_scopes_and_variables(self, frame_id: int) -> None:
        self.state.scopes = await self.client.scopes(frame_id)
        self.state.variables.clear()
        for scope in self.state.scopes:
            variables = await self.client.variables(scope.variables_reference)
            self.state.variables[scope.variables_reference] = variables

    # --- DAP event handlers ---
    # Called synchronously from the DAP read loop.
    # They delegate to the event_handler — all async work happens elsewhere.

    def _on_initialized(self, event: Event) -> None:
        self.event_handler.on_initialized()

    def _on_stopped(self, event: Event) -> None:
        thread_id = event.body.get("threadId")
        reason = event.body.get("reason", "unknown")
        description = event.body.get("description")
        text = event.body.get("text")
        self.event_handler.on_stopped(thread_id, reason, description, text)

    def _on_continued(self, event: Event) -> None:
        self.event_handler.on_continued()

    def _on_terminated(self, event: Event) -> None:
        self.event_handler.on_terminated()

    def _on_exited(self, event: Event) -> None:
        exit_code = event.body.get("exitCode", 0)
        self.event_handler.on_exited(exit_code)

    def _on_output(self, event: Event) -> None:
        text = event.body.get("output", "")
        category = event.body.get("category", "console")
        self.event_handler.on_output(text, category)
