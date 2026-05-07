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
from .state import DebugState, SessionPhase

log = logging.getLogger(__name__)


# --- Terminal emulator command building ---

# Maps the CLI --terminal choice to (executable name, flags before the command).
# The flags accept the remaining args as the command + arguments.
_TERMINAL_SPECS: dict[str, tuple[str, list[str]]] = {
    "xterm":          ("xterm",          ["-e"]),
    "konsole":        ("konsole",        ["-e"]),
    "gnome-terminal": ("gnome-terminal", ["--wait", "--"]),
    "ghostty":        ("ghostty",        ["-e"]),
    "kitty":          ("kitty",          []),
    "iterm2":         ("iterm2",         ["-e"]),
    "warp":           ("warp",           ["-e"]),
    "wezterm":        ("wezterm",        ["start", "--"]),
    "terminator":     ("terminator",     ["-x"]),
}


def _resolve_terminal(choice: str) -> str:
    """Resolve a --terminal choice to a full executable path."""
    spec = _TERMINAL_SPECS.get(choice)
    if spec is None:
        raise RuntimeError(f"Unknown terminal choice: {choice}")
    exe, _ = spec
    path = shutil.which(exe)
    if not path:
        raise RuntimeError(
            f"Terminal '{choice}' not found on PATH. Install it or pick a different --terminal.",
        )
    return path


def _build_terminal_cmd(choice: str, args: list[str]) -> list[str]:
    """Build the command to launch the chosen terminal running the given args."""
    path = _resolve_terminal(choice)
    _, exec_flag = _TERMINAL_SPECS[choice]
    return [path] + exec_flag + args


class DebugController:
    """Orchestrates the debug session between DAP client and event consumers."""

    def __init__(self, event_handler: DebugEventHandler) -> None:
        self.event_handler = event_handler
        self.client = DAPClient()
        self.state = DebugState()
        self._terminal: str | None = None
        self._lock = asyncio.Lock()
        # Child process debug sessions (pid → DAPClient)
        self._child_clients: dict[int, DAPClient] = {}
        self._child_attach_lock = asyncio.Lock()
        # The "active" client is the one whose stop event we're currently
        # inspecting.  Defaults to the parent client.
        self._active_client: DAPClient = self.client
        # In remote-attach mode we do NOT own the debuggee; quitting tdb
        # must detach without terminating it (otherwise tdb.breakpoint()
        # and similar attach workflows would kill the user's program).
        self._is_remote_attach: bool = False
        # Strong references to fire-and-forget tasks so they aren't
        # garbage-collected mid-execution (asyncio holds only weak refs).
        self._bg_tasks: set[asyncio.Task] = set()
        # Set whenever the debuggee enters a stopped/terminated state,
        # cleared on continue. `pause()` awaits this with a timeout so
        # the caller can detect when a pause request never lands (the
        # classic failure mode is a fully-deadlocked asyncio program
        # whose main thread is blocked in epoll_wait — debugpy can't
        # deliver pause without a Python frame to trace).
        self._stopped_event = asyncio.Event()

    # --- Public capability surface --------------------------------------
    # These properties are how external code (TUI, server, future entry
    # modes) asks "what can I do with this session?". Reaching into
    # `_is_remote_attach` / `_child_clients` from the outside is a layer
    # leak — go through this surface instead.

    @property
    def is_remote_attach(self) -> bool:
        """True when the controller is attached to a debugpy server we
        don't own (the `tdb -r ...` and `tdb.breakpoint()` cases).
        """
        return self._is_remote_attach

    @property
    def supports_restart(self) -> bool:
        """True when `_restart_session` can meaningfully relaunch.

        Remote-attach sessions can't — there's no `program` to launch and
        the debugpy server is owned by the user's process.
        """
        return not self._is_remote_attach

    @property
    def session_lock(self) -> asyncio.Lock:
        """Serializes RPC actions so concurrent HTTP requests can't
        interleave a step + an evaluate. The TUI doesn't need this lock
        because its actions go through textual's single message loop.
        """
        return self._lock

    def get_child_client(self, pid: int) -> DAPClient | None:
        """Return the DAP client for a tracked child process, or None."""
        return self._child_clients.get(pid)

    def has_child_clients(self) -> bool:
        return bool(self._child_clients)

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
        terminal: str | None = None,
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
        self._terminal = terminal
        self._setup_event_handlers()

        if terminal is not None:
            self.client.on_reverse_request("runInTerminal", self._handle_run_in_terminal)

        self._launch_params = {
            "program": program,
            "args": args,
            # Default to the directory tdb was launched from, NOT the
            # program's parent — a debuggee invoked as `tdb examples/x.py`
            # should inherit the user's current working directory so
            # relative paths (data files, imports) resolve the way they
            # did at the shell. `--cwd` overrides.
            "cwd": cwd or str(Path.cwd()),
            "stop_on_entry": stop_on_entry,
            "just_my_code": just_my_code,
            "python": python,
            "sub_process": sub_process,
        }

        await self.client.start()
        await self.client.initialize(support_run_in_terminal=terminal is not None)

        # Send launch — don't await response (debugpy holds it until configurationDone)
        p = self._launch_params
        self._launch_future = await self.client.launch(
            program=p["program"],
            args=p["args"],
            cwd=p["cwd"],
            stop_on_entry=p["stop_on_entry"],
            just_my_code=p["just_my_code"],
            python=p["python"],
            console="externalTerminal" if terminal is not None else "internalConsole",
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
        self._is_remote_attach = True

        await self.client.connect(host, port)
        await self.client.initialize()

        self._launch_future = await self.client.attach(host=host, port=port)

    async def _handle_run_in_terminal(self, request: Request) -> dict[str, Any]:
        """Handle the runInTerminal reverse request from debugpy."""
        cmd_args: list[str] = request.arguments.get("args", [])
        cwd = request.arguments.get("cwd")
        env = request.arguments.get("env")

        if self._terminal is None:
            raise RuntimeError("runInTerminal requested but no --terminal was set")
        full_cmd = _build_terminal_cmd(self._terminal, cmd_args)

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
        timeout = 60.0 if self._terminal is not None else 30.0
        response = await asyncio.wait_for(self._launch_future, timeout=timeout)
        if not response.success:
            raise Exception(f"Launch failed: {response.message}")

        # Don't clobber STOPPED: in remote-attach (tdb.breakpoint()) the
        # debuggee may already be paused at the hook by the time configuration
        # finishes, and `_on_stopped` will have set phase=STOPPED before this
        # line runs.
        if self.state.phase == SessionPhase.NOT_STARTED:
            self.state.transition_to(SessionPhase.RUNNING)

    async def stop(self) -> None:
        for pid, child in list(self._child_clients.items()):
            try:
                await child.disconnect(terminate=False)
                await child.stop()
            except Exception:
                pass
        self._child_clients.clear()
        await self.client.disconnect(terminate=not self._is_remote_attach)
        await self.client.stop()
        self.state.transition_to(SessionPhase.TERMINATED)

    # --- Debug actions ---

    async def continue_(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.transition_to(SessionPhase.RUNNING)
            self.state.clear_frame_data()
            # Fire-and-forget continue to every known client. We don't await
            # responses: debugpy does not reply to `continue` when the target
            # isn't currently stopped (e.g. parent blocked in p.join(), or a
            # child already resumed by a prior continue), so awaiting would
            # stall for the full DAP timeout and cascade on subsequent calls.
            # The next stopped event from any process is what wakes the caller.
            ac = self._active_client
            self._active_client = self.client
            try:
                await ac.continue_nowait(self.state.current_thread_id)
            except Exception:
                pass
            if ac is not self.client:
                self._spawn_bg(self._resume_client(self.client))
            for _pid, child in list(self._child_clients.items()):
                if child is ac:
                    continue
                self._spawn_bg(self._resume_client(child))

    def _spawn_bg(self, coro: Any) -> None:
        """Schedule a fire-and-forget task with a strong reference.

        asyncio's event loop only keeps weak refs to tasks, so without this
        the task can be garbage-collected before it ever runs.
        """
        task = asyncio.get_event_loop().create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _resume_client(self, client: DAPClient) -> None:
        """Best-effort continue on a client that may or may not be stopped."""
        try:
            threads = await client.threads()
        except Exception:
            return
        if not threads:
            return
        try:
            await client.continue_nowait(threads[0].id)
        except Exception:
            pass

    async def step_over(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.transition_to(SessionPhase.RUNNING)
            self.state.clear_frame_data()
            await self._active_client.next(self.state.current_thread_id)

    async def step_in(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.transition_to(SessionPhase.RUNNING)
            self.state.clear_frame_data()
            await self._active_client.step_in(self.state.current_thread_id)

    async def step_out(self) -> None:
        if self.state.current_thread_id is not None:
            self.state.transition_to(SessionPhase.RUNNING)
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

    async def pause(self, timeout: float = 2.0) -> bool:
        """Pause all processes (parent + children).

        Returns True if a stop event arrived within `timeout`, False
        otherwise. The False case is most often a fully-deadlocked
        asyncio program: debugpy queues the pause but cannot deliver
        it without a Python frame to trace, so the user gets no
        visible response. Callers should surface a notification on
        False so the keypress isn't silently swallowed.
        """
        if self.state.is_terminated:
            return False
        if self.state.phase == SessionPhase.STOPPED:
            return True  # already stopped — nothing to wait for
        if self.state.current_thread_id is None:
            return False
        # Clear before sending so a stale set() from a previous stop
        # doesn't make us return True instantly.
        self._stopped_event.clear()
        try:
            await self.client.pause(self.state.current_thread_id)
        except Exception:
            log.exception("DAP pause request failed for parent")
            return False
        for pid, child in list(self._child_clients.items()):
            try:
                threads = await child.threads()
                if threads:
                    await child.pause(threads[0].id)
            except Exception:
                log.exception("DAP pause request failed for child pid=%s", pid)
        try:
            await asyncio.wait_for(self._stopped_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

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
        if self.state.is_post_mortem:
            # All data is pre-populated; just swap in the selected frame's scopes.
            self.state.scopes = self.state.frame_scopes.get(frame_id, [])
            return
        await self.fetch_scopes_and_variables(frame_id)

    def load_post_mortem(self, snapshot: dict) -> None:
        """Populate state from a snapshot produced by tdb.exception_hook.

        After this call the controller holds a frozen view of the crash:
        stack_frames, scopes, and variables are all set, no DAP session is
        active, and is_post_mortem / is_terminated are True.
        """
        from tdb.dap.types import Scope, Source, StackFrame, Variable

        self.state.transition_to(SessionPhase.POST_MORTEM)
        self.state.stop_reason = "exception"

        frames_data = snapshot.get("frames", [])
        vars_data: dict[str, list[dict]] = snapshot.get("variables", {})

        # Rebuild variables keyed by int reference.
        self.state.variables = {}
        for ref_str, entries in vars_data.items():
            ref = int(ref_str)
            self.state.variables[ref] = [
                Variable(
                    name=e.get("name", ""),
                    value=e.get("value", ""),
                    type=e.get("type", ""),
                    variables_reference=e.get("variablesReference", 0),
                )
                for e in entries
            ]

        # Rebuild frames + per-frame scopes.
        self.state.stack_frames = []
        self.state.frame_scopes = {}
        for fd in frames_data:
            fid = fd["id"]
            path = fd.get("filename", "")
            frame = StackFrame(
                id=fid,
                name=fd.get("funcname", "<frame>"),
                source=Source(path=path, name=os.path.basename(path) if path else None),
                line=fd.get("lineno", 0),
            )
            self.state.stack_frames.append(frame)
            self.state.frame_scopes[fid] = [
                Scope(name=s["name"], variables_reference=s["variablesReference"])
                for s in fd.get("scopes", [])
            ]

        if self.state.stack_frames:
            top = self.state.stack_frames[0]
            self.state.current_frame_id = top.id
            self.state.scopes = self.state.frame_scopes.get(top.id, [])

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

        # Single state authority: every downstream consumer sees consistent
        # values synchronously from the DAP read loop. Previously the TUI
        # and the headless RPC server each duplicated these assignments,
        # which is how the `is_terminated` propagation bug crept in for the
        # `_on_terminated` sibling event.
        self.state.transition_to(SessionPhase.STOPPED)
        self.state.stop_reason = reason
        if thread_id is not None:
            self.state.current_thread_id = thread_id

        self._active_client = self.client
        self._stopped_event.set()
        self.event_handler.on_stopped(thread_id, reason, description, text)
        if self._child_clients and reason not in ("pause",):
            self._spawn_bg(self._pause_children())

    def _on_continued(self, event: Event) -> None:
        # Mirror of _on_stopped: producer-side state mutation so consumers
        # don't have to remember to do it themselves.
        self.state.transition_to(SessionPhase.RUNNING)
        self.state.clear_frame_data()
        self._stopped_event.clear()
        self.event_handler.on_continued()

    def _on_terminated(self, event: Event) -> None:
        # Update state synchronously so every downstream consumer (server
        # RPC guards, controller breakpoint-skip guards, TUI handlers) sees
        # is_terminated immediately. Previously only the TUI's async handler
        # set this, so headless mode left state.is_terminated stuck at False
        # after the program ended naturally.
        self.state.transition_to(SessionPhase.TERMINATED)
        # Wake any pause()-style waiters so they unblock on termination
        # rather than running out the clock — they'll see is_terminated.
        self._stopped_event.set()
        self.event_handler.on_terminated()

    def _on_exited(self, event: Event) -> None:
        exit_code = event.body.get("exitCode", 0)
        # `exited` and `terminated` usually arrive as a pair, but debugpy can
        # emit either one independently on edge cases (hard crashes, abrupt
        # disconnects). Transition to TERMINATED on both so the guards
        # engage no matter which event we get.
        self.state.transition_to(SessionPhase.TERMINATED)
        self._stopped_event.set()
        self.event_handler.on_exited(exit_code)

    def _on_output(self, event: Event) -> None:
        text = event.body.get("output", "")
        category = event.body.get("category", "console")
        # Drop DAP telemetry events (debugpy/ptvsd version info) — not program output
        if category == "telemetry":
            return
        # In external terminal mode, program stdout/stderr goes to the
        # terminal directly — don't duplicate it in the Console View.
        if self._terminal is not None and category in ("stdout", "stderr"):
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
        self._spawn_bg(self._attach_child(host, port, pid))

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
                    thread_id = event.body.get("threadId")
                    reason = event.body.get("reason", "unknown")
                    description = event.body.get("description")
                    text = event.body.get("text")

                    # Same state-authority contract as _on_stopped above —
                    # whether the active stop is on parent or child, state
                    # gets updated from one place synchronously.
                    self.state.transition_to(SessionPhase.STOPPED)
                    self.state.stop_reason = reason
                    if thread_id is not None:
                        self.state.current_thread_id = thread_id

                    self._active_client = _child
                    if reason not in ("pause",):
                        self._spawn_bg(self._pause_parent())
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


