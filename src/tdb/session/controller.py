"""Debug session controller: bridges DAP client and event consumers."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from tdb.dap.client import DAPClient
from tdb.dap.messages import Event, Request
from tdb.dap.types import SourceBreakpoint
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
        # Child process debug sessions (pid → DAPClient)
        self._child_clients: dict[int, DAPClient] = {}
        self._child_attach_lock = asyncio.Lock()
        # The "active" client is the one whose stop event we're currently
        # inspecting.  Defaults to the parent client.
        self._active_client: DAPClient = self.client

    @staticmethod
    def _enabled_bps(bps: list[SourceBreakpoint]) -> list[SourceBreakpoint]:
        """Return only the breakpoints that are enabled."""
        return [bp for bp in bps if bp.enabled]

    def _setup_event_handlers(self) -> None:
        self.client.on_event("stopped", self._on_stopped)
        self.client.on_event("continued", self._on_continued)
        self.client.on_event("terminated", self._on_terminated)
        self.client.on_event("exited", self._on_exited)
        self.client.on_event("output", self._on_output)
        self.client.on_event("initialized", self._on_initialized)
        # Child process debugging
        self.client.on_event("debugpyAttach", self._on_child_attach)

    async def start(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        stop_on_entry: bool = False,
        just_my_code: bool = True,
        python: str | None = None,
        external_terminal: bool = False,
        sub_process: bool = True,
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
            "sub_process": sub_process,
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
            sub_process=p["sub_process"],
        )

    async def remote_attach(self, host: str, port: int) -> None:
        """Attach to a remote debugpy server listening on host:port.

        DAP sequence (same as launch, but with attach instead):
        1. connect via TCP to debugpy server
        2. initialize → response
        3. attach (fire, don't await — debugpy holds the response)
        4. debugpy sends 'initialized' event
        5. on_dap_initialized handler sends breakpoints + configurationDone
        6. debugpy sends attach response
        """
        self._setup_event_handlers()
        self._launch_params = {}

        await self.client.connect(host, port)
        await self.client.initialize()

        self._launch_future = await self.client.attach(host=host, port=port)

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

        # Send breakpoints (skip if globally disabled; also filter per-bp enabled)
        if not self.state.breakpoints_disabled:
            for source_path, bps in self.state.breakpoints.items():
                await self.client.set_breakpoints(
                    source_path, self._enabled_bps(bps),
                )

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
        for pid, child in list(self._child_clients.items()):
            try:
                await child.disconnect(terminate=False)
                await child.stop()
            except Exception:
                pass
        self._child_clients.clear()
        await self.client.disconnect(terminate=True)
        await self.client.stop()
        self.state.is_terminated = True

    # --- Debug actions ---

    async def continue_(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            # Continue the active client (might be child or parent)
            ac = self._active_client
            try:
                await ac.continue_(self.state.current_thread_id)
            except Exception:
                pass
            # Also continue all other processes
            self._active_client = self.client
            if ac is not self.client:
                try:
                    threads = await self.client.threads()
                    if threads:
                        await self.client.continue_(threads[0].id)
                except Exception:
                    pass
            for pid, child in list(self._child_clients.items()):
                if child is ac:
                    continue
                try:
                    threads = await child.threads()
                    if threads:
                        await child.continue_(threads[0].id)
                except Exception:
                    pass

    async def step_over(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            await self._active_client.next(self.state.current_thread_id)

    async def step_in(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            await self._active_client.step_in(self.state.current_thread_id)

    async def step_out(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.is_running = True
            self.state.clear_frame_data()
            await self._active_client.step_out(self.state.current_thread_id)

    async def run_to_cursor(self, source_path: str, line: int) -> None:
        """Set a temporary breakpoint at line, continue, then remove it.

        Sets the breakpoint on the parent and all child clients so the
        breakpoint fires regardless of which process reaches the line.
        """
        if self.state.current_thread_id is None:
            return
        # Add a temporary breakpoint to the saved set
        bps = list(self.state.breakpoints.get(source_path, []))
        had_bp = any(bp.line == line for bp in bps)
        if not had_bp:
            bps.append(SourceBreakpoint(line=line))
            self.state.breakpoints[source_path] = bps
            if self.state.is_ready and not self.state.is_terminated:
                # Propagate to parent and all child clients
                sent = self._enabled_bps(bps)
                try:
                    await self.client.set_breakpoints(source_path, sent)
                except Exception:
                    pass
                for _pid, child in list(self._child_clients.items()):
                    try:
                        await child.set_breakpoints(source_path, sent)
                    except Exception:
                        pass
        self._run_to_cursor_cleanup = (source_path, line) if not had_bp else None
        # Use the same continue-all logic as the normal continue command
        await self.continue_()

    async def cleanup_run_to_cursor(self) -> None:
        """Remove the temporary breakpoint after stopping."""
        if not hasattr(self, "_run_to_cursor_cleanup") or self._run_to_cursor_cleanup is None:
            return
        source_path, line = self._run_to_cursor_cleanup
        self._run_to_cursor_cleanup = None
        bps = [bp for bp in self.state.breakpoints.get(source_path, []) if bp.line != line]
        self.state.breakpoints[source_path] = bps
        if self.state.is_ready and not self.state.is_terminated:
            sent = self._enabled_bps(bps)
            try:
                await self.client.set_breakpoints(source_path, sent)
            except Exception:
                pass
            for _pid, child in list(self._child_clients.items()):
                try:
                    await child.set_breakpoints(source_path, sent)
                except Exception:
                    pass

    async def pause(self) -> None:
        """Pause all processes (parent + children)."""
        if self.state.current_thread_id is not None:
            try:
                await self.client.pause(self.state.current_thread_id)
            except Exception:
                pass
        for pid, child in list(self._child_clients.items()):
            try:
                threads = await child.threads()
                if threads:
                    await child.pause(threads[0].id)
            except Exception:
                pass

    async def toggle_breakpoint(self, source_path: str, line: int) -> None:
        bps = self.state.breakpoints.get(source_path, [])
        existing = [bp for bp in bps if bp.line == line]
        if existing:
            bps = [bp for bp in bps if bp.line != line]
        else:
            bps.append(SourceBreakpoint(line=line))
        self.state.breakpoints[source_path] = bps

        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, self._enabled_bps(bps))

    async def toggle_breakpoint_enabled(self, source_path: str, line: int) -> None:
        """Toggle the enabled state of a single breakpoint.

        When disabled, the bp stays in the state (so it's visible in the UI
        and persisted) but is filtered out when sending to debugpy.
        """
        bps = self.state.breakpoints.get(source_path, [])
        for bp in bps:
            if bp.line == line:
                bp.enabled = not bp.enabled
                break
        else:
            return  # not found
        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, self._enabled_bps(bps))

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
                source_path,
                self._enabled_bps(self.state.breakpoints[source_path]),
            )

    async def remove_breakpoint(self, source_path: str, line: int) -> None:
        """Remove a breakpoint at the given location (no-op if not set)."""
        bps = self.state.breakpoints.get(source_path, [])
        new_bps = [bp for bp in bps if bp.line != line]
        if len(new_bps) == len(bps):
            return  # nothing to remove
        self.state.breakpoints[source_path] = new_bps

        if self.state.is_ready and not self.state.is_terminated:
            await self.client.set_breakpoints(source_path, self._enabled_bps(new_bps))

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
            await self.client.set_breakpoints(source_path, self._enabled_bps(bps))


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
                await self.client.set_breakpoints(source_path, self._enabled_bps(bps))

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
        if not self.state.is_terminated:
            await self.fetch_scopes_and_variables(new_frame.id)
        return True

    @property
    def active_client(self) -> DAPClient:
        """The client (parent or child) that is currently being inspected."""
        return self._active_client

    async def evaluate(self, expression: str) -> str:
        try:
            result, _ = await self._active_client.evaluate(
                expression,
                frame_id=self.state.current_frame_id,
                context="repl",
            )
            return result
        except Exception as e:
            return str(e)

    def get_child_pids(self) -> list[int]:
        """Return PIDs of tracked child processes."""
        return list(self._child_clients.keys())

    async def evaluate_on_parent(self, expression: str) -> str:
        """Evaluate an expression on the parent process (not the active child).

        Fetches a frame from the parent if needed so the evaluate has
        proper scope context.  Retries briefly if the parent isn't stopped yet.
        """
        from tdb.dap.client import DAPError

        for attempt in range(3):
            try:
                # Get a frame_id from the parent process
                frame_id = None
                if self._active_client is self.client:
                    frame_id = self.state.current_frame_id
                else:
                    threads = await self.client.threads()
                    if threads:
                        frames = await self.client.stack_trace(threads[0].id)
                        if frames:
                            frame_id = frames[0].id
                result, _ = await self.client.evaluate(
                    expression, frame_id=frame_id, context="repl",
                )
                return result
            except DAPError as e:
                if "Unable to find thread" in str(e) and attempt < 2:
                    # Parent may not be stopped yet — wait and retry
                    await asyncio.sleep(0.5)
                    continue
                return str(e)
            except Exception as e:
                return str(e)
        return ""

    async def select_frame(self, frame_id: int) -> None:
        self.state.current_frame_id = frame_id
        await self.fetch_scopes_and_variables(frame_id)

    async def fetch_stop_info(self) -> None:
        """After stopping, fetch threads, stack trace, scopes, and variables.

        Uses _active_client which points to whichever client (parent or
        child) most recently stopped at a breakpoint/exception.
        """
        ac = self._active_client
        try:
            self.state.threads = await ac.threads()
        except Exception:
            log.exception("Error fetching threads")

        if self.state.current_thread_id is not None:
            try:
                self.state.stack_frames = await ac.stack_trace(
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
        ac = self._active_client
        self.state.scopes = await ac.scopes(frame_id)
        self.state.variables.clear()
        for scope in self.state.scopes:
            variables = await ac.variables(scope.variables_reference)
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
        self._active_client = self.client
        self.event_handler.on_stopped(thread_id, reason, description, text)
        if self._child_clients and reason not in ("pause",):
            asyncio.get_event_loop().create_task(self._pause_children())

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
        # Drop DAP telemetry events (debugpy/ptvsd version info) — not program output
        if category == "telemetry":
            return
        # In external terminal mode, program stdout/stderr goes to the
        # terminal directly — don't duplicate it in the Console View.
        if self._external_terminal and category in ("stdout", "stderr"):
            return
        self.event_handler.on_output(text, category)

    # --- Child process debugging ---

    def _on_child_attach(self, event: Event) -> None:
        """debugpyAttach: a child process is ready for debugging."""
        body = event.body
        host = body.get("connect", {}).get("host", "127.0.0.1")
        port = body.get("connect", {}).get("port")
        pid = body.get("subProcessId") or body.get("processId")
        if not port:
            log.warning("debugpyAttach missing port: %s", body)
            return
        log.info("Child process detected: pid=%s port=%s", pid, port)
        asyncio.get_event_loop().create_task(
            self._attach_child(host, port, pid)
        )

    async def _attach_child(self, host: str, port: int, pid: int | None = None) -> None:
        """Connect to the adapter for a child process and configure breakpoints."""
        async with self._child_attach_lock:
            child = DAPClient()
            try:
                await child.connect(host, port)

                # Wait for 'initialized' event before configuring
                initialized = asyncio.Event()
                child.on_event("initialized", lambda e: initialized.set())

                # Forward stopped events: pause all others on breakpoint/exception
                def on_child_stopped(event: Event, _child: DAPClient = child) -> None:
                    reason = event.body.get("reason", "unknown")
                    self._active_client = _child
                    if reason not in ("pause",):
                        asyncio.get_event_loop().create_task(self._pause_parent())
                    thread_id = event.body.get("threadId")
                    description = event.body.get("description")
                    text = event.body.get("text")
                    self.event_handler.on_stopped(thread_id, reason, description, text)

                def on_child_terminated(event: Event) -> None:
                    # Find and remove this client
                    for pid, c in list(self._child_clients.items()):
                        if c is child:
                            self._child_clients.pop(pid, None)
                            break

                child.on_event("stopped", on_child_stopped)
                child.on_event("terminated", on_child_terminated)
                child.on_event("exited", on_child_terminated)
                child.on_event("output", self._on_output)

                await child.initialize()

                # Send attach with subProcessId (not processId) to route
                # to the right child without triggering ptrace injection.
                # Inherit parent's just_my_code setting.
                jmc = self._launch_params.get("just_my_code", True)
                attach_future = await child.attach(
                    host=host, port=port, sub_process_id=pid,
                    just_my_code=jmc,
                )

                await asyncio.wait_for(initialized.wait(), timeout=10.0)

                # Configure breakpoints
                await child.set_exception_breakpoints(["userUnhandled"])
                if not self.state.breakpoints_disabled:
                    for source_path, bps in self.state.breakpoints.items():
                        try:
                            await child.set_breakpoints(
                                source_path, self._enabled_bps(bps),
                            )
                        except Exception:
                            pass

                await child.configuration_done()
                await asyncio.wait_for(attach_future, timeout=30.0)

                key = pid or port
                self._child_clients[key] = child
                log.info("Attached to child process pid=%s (port %d)", pid, port)

            except Exception:
                log.exception("Failed to attach to child (port %d)", port)
                try:
                    await child.stop()
                except Exception:
                    pass

    async def _pause_parent(self) -> None:
        """Pause the parent process and wait for it to stop."""
        try:
            threads = await self.client.threads()
            if threads:
                await self.client.pause(threads[0].id)
        except Exception:
            pass

    async def _pause_children(self) -> None:
        """Pause all child processes."""
        for pid, child in list(self._child_clients.items()):
            try:
                threads = await child.threads()
                if threads:
                    await child.pause(threads[0].id)
            except Exception:
                pass


