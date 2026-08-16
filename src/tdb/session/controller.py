"""Debug session controller: bridges DAP client and event consumers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tdb.dap.client import DAPClient
from tdb.dap.messages import Event
from tdb.dap.types import DEFERRED_VERIFICATION_MESSAGE, Breakpoint, SourceBreakpoint
from .event_bus import DebugEventHandler
from .state import DebugState, SessionPhase

if TYPE_CHECKING:
    from tdb.languages.base import LanguageProfile

log = logging.getLogger(__name__)


# Terminal emulator launching lives in tdb.session.terminal — see
# TerminalLauncher there.


class DebugController:
    """Orchestrates the debug session between DAP client and event consumers."""

    def __init__(
        self,
        event_handler: DebugEventHandler,
        profile: "LanguageProfile | None" = None,
    ) -> None:
        if profile is None:
            from tdb.languages.python import PYTHON_PROFILE

            profile = PYTHON_PROFILE
        self.profile = profile
        self.event_handler = event_handler
        self.client = DAPClient(profile.adapter)
        self.state = DebugState()
        self._terminal: str | None = None
        self._lock = asyncio.Lock()
        # Child process debug sessions (pid → DAPClient) — owned by
        # ChildProcessManager in session/child_processes.py.
        from tdb.session.child_processes import ChildProcessManager

        self._children = ChildProcessManager(self)
        # Back-compat shims so external code that grabbed the raw dict /
        # lock keeps working (the attribute names match what the previous
        # in-controller fields exposed).
        self._child_clients = self._children._clients
        self._child_attach_lock = self._children._attach_lock
        # The "active" client is the one whose stop event we're currently
        # inspecting.  Defaults to the parent client.
        self._active_client: DAPClient = self.client
        # In remote-attach mode we do NOT own the debuggee; quitting tdb
        # must detach without terminating it (otherwise tdb.breakpoint()
        # and similar attach workflows would kill the user's program).
        self._is_remote_attach: bool = False
        # True for sessions handed to the TUI by run mode (`tdb --run`).
        # Restart is meaningless there: the run-mode loop owns the
        # process lifecycle, not the TUI episode.
        self.adopted_session: bool = False
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
        # Statement-granularity stepping (`n` skips through multi-line
        # statements as one step). See session/statement_stepper.py.
        from tdb.session.statement_stepper import StatementStepper

        self._stepper = StatementStepper(
            self.state,
            issue_step=self._issue_statement_step,
            ensure_stack_loaded=self.fetch_stop_info,
            compute_units=self.profile.capabilities.compute_step_units,
        )

    @property
    def step_mode(self) -> str:
        return self._stepper.mode

    @step_mode.setter
    def step_mode(self, mode: str) -> None:
        self._stepper.set_mode(mode)

    def set_step_mode(self, mode: str) -> None:
        """Switch step granularity. Clears any pending statement-step."""
        self._stepper.set_mode(mode)

    async def _issue_statement_step(self, kind: str) -> None:
        """Callback used by `StatementStepper` to fire one DAP step
        against the current active thread. Called from inside the
        stepper's continue-loop after it has decided we're still in
        the originating statement."""
        tid = self.state.current_thread_id
        if tid is None:
            return
        if kind == "next":
            await self._active_client.next(tid)
        else:
            await self._active_client.step_in(tid)

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
        return not (self._is_remote_attach or self.adopted_session)

    @property
    def session_lock(self) -> asyncio.Lock:
        """Serializes RPC actions so concurrent HTTP requests can't
        interleave a step + an evaluate. The TUI doesn't need this lock
        because its actions go through textual's single message loop.
        """
        return self._lock

    def get_child_client(self, pid: int) -> DAPClient | None:
        """Return the DAP client for a tracked child process, or None."""
        return self._children.get_client(pid)

    def has_child_clients(self) -> bool:
        return self._children.has_clients()

    @staticmethod
    def _enabled_bps(bps: list[SourceBreakpoint]) -> list[SourceBreakpoint]:
        """Return only the breakpoints that are enabled."""
        return [bp for bp in bps if bp.enabled]

    def _warn_unbound_breakpoints(
        self, source_path: str, result: list[Breakpoint]
    ) -> None:
        """A file whose breakpoints ALL failed to bind usually means the
        target has no debug info for it (native binary built without -g,
        or generated/stale source). Surface a hint on the console.

        Deliberately a no-op when `result` is empty (e.g. sending an
        empty breakpoint list to disable/clear) — there's nothing to
        warn about when no breakpoints were requested at all.

        Also a no-op when an unverified breakpoint carries
        DEFERRED_VERIFICATION_MESSAGE: the adapter has already explained
        that verification is merely deferred (e.g. perl's compile-phase
        breakpoint deferral), not failed, and will correct the state via
        a `breakpoint` "changed" event once it settles (see
        _on_breakpoint_event) — guessing "-g?" on top of that would be
        wrong.
        """
        if not result:
            return
        if any(bp.message == DEFERRED_VERIFICATION_MESSAGE for bp in result):
            return
        if all(not bp.verified for bp in result):
            self._emit_unbound_warning(source_path)

    def _emit_unbound_warning(self, source_path: str) -> None:
        self.event_handler.on_output(
            f"warning: no breakpoints bound in {source_path} — "
            f"was the program compiled with debug info (-g)?\n",
            "console",
        )

    async def _send_breakpoints(
        self, source_path: str, bps: list[SourceBreakpoint]
    ) -> list[Breakpoint]:
        """Transmit breakpoints for one file on the parent client and warn
        if none of them bound. Excludes run_to_cursor/cleanup_run_to_cursor,
        whose temporary breakpoint failing to bind is handled by that flow
        (and which also propagate to child-process clients, unlike the
        sites that route through here).
        """
        result = await self.client.set_breakpoints(source_path, bps)
        self._warn_unbound_breakpoints(source_path, result)
        return result

    def _setup_event_handlers(self) -> None:
        self.client.on_event("stopped", self._on_stopped)
        self.client.on_event("continued", self._on_continued)
        self.client.on_event("terminated", self._on_terminated)
        self.client.on_event("exited", self._on_exited)
        self.client.on_event("output", self._on_output)
        self.client.on_event("initialized", self._on_initialized)
        self.client.on_event("breakpoint", self._on_breakpoint_event)
        # Child process debugging: ChildProcessManager registers its
        # own debugpyAttach handler that fires _spawn_bg(_attach(...)).
        # This is a debugpy-proprietary mechanism (the `debugpyAttach`
        # reverse event); only wire it up when the profile opts in.
        if self.profile.capabilities.child_process_strategy == "debugpy":
            self._children.register_on(self.client)

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
            from tdb.session.terminal import TerminalLauncher

            self._terminal_launcher = TerminalLauncher(
                terminal,
                on_started=self.event_handler.on_external_terminal_started,
            )
            self.client.on_reverse_request(
                "runInTerminal",
                self._terminal_launcher.handle_run_in_terminal,
            )

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

    async def remote_attach(
        self,
        host: str,
        port: int,
        path_mappings: list[tuple[str, str]] | None = None,
    ) -> None:
        """Attach to a remote debugpy server listening on host:port.

        DAP sequence (same as launch, but with attach instead):
        1. connect via TCP to debugpy server
        2. initialize → response
        3. attach (fire, don't await — debugpy holds the response)
        4. debugpy sends 'initialized' event
        5. on_dap_initialized handler sends breakpoints + configurationDone
        6. debugpy sends attach response

        `path_mappings` is forwarded to debugpy's `pathMappings` attach
        arg — see `dap/client.py::attach`.
        """
        self._setup_event_handlers()
        self._launch_params = {}
        self._is_remote_attach = True

        if self.profile.adapter.quirks.attach_via_adapter:
            await self.client.start()
        else:
            await self.client.connect(host, port)
        await self.client.initialize()

        self._launch_future = await self.client.attach(
            host=host,
            port=port,
            path_mappings=path_mappings,
        )

    async def do_configure(self) -> None:
        """Called after 'initialized' event. Sends breakpoints + configurationDone.

        This unblocks the launch response from debugpy.
        """
        # Exception-breakpoint filters are adapter-specific; the spec
        # picks them from what the adapter advertised at initialize.
        filters = self.profile.adapter.pick_exception_filters(self.client.capabilities)
        if filters:
            await self.client.set_exception_breakpoints(filters)

        # Send breakpoints (skip if globally disabled; also filter per-bp enabled)
        if not self.state.breakpoints_disabled:
            for source_path, bps in self.state.breakpoints.items():
                await self._send_breakpoints(
                    source_path,
                    self._enabled_bps(bps),
                )

        # Pre-arm pause for remote-attach BEFORE configurationDone.
        # debugpy ignores `stopOnEntry` for attach (only honored for
        # launch — see pydevd_process_net_command_json.py:492). Sending
        # `pause` after configurationDone races the debuggee: short
        # programs (e.g. examples/pi_remote_attach.py) can run to
        # completion in the ~100ms before the pause request lands.
        # Sending pause BEFORE configurationDone queues the "suspend
        # at next traced line" flag inside debugpy; when configurationDone
        # unblocks wait_for_client(), the very first user-code statement
        # trips the flag and emits `stopped` deterministically.
        # Skipped if the debuggee is already paused (tdb.breakpoint() hook).
        if (
            self._is_remote_attach
            and self.profile.adapter.quirks.pre_arm_pause_on_attach
            and self.state.phase != SessionPhase.STOPPED
        ):
            try:
                threads = await self.client.threads()
                if threads:
                    # debugpy pauses the whole interpreter on any single
                    # thread (GIL makes per-thread pause effectively
                    # global). First thread is conventionally MainThread.
                    await self.client.pause(threads[0].id)
            except Exception:
                log.debug("Pre-arm pause on remote-attach failed", exc_info=True)

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
        # Child disconnects run in parallel with a short timeout: when
        # the parent's `disconnect(terminate=True)` lands, debugpy
        # cascades the termination to all subprocesses, so per-child
        # DAP disconnect is cleanup, not load-bearing. See
        # ChildProcessManager.close_all for the per-child loop.
        from tdb._timeouts import DAP_DISCONNECT_PARENT

        await self._children.close_all()
        try:
            await asyncio.wait_for(
                self.client.disconnect(terminate=not self._is_remote_attach),
                timeout=DAP_DISCONNECT_PARENT,
            )
        except (asyncio.TimeoutError, Exception):
            pass
        await self.client.stop()
        self.state.transition_to(SessionPhase.TERMINATED)
        # Remove any lingering Processes-modal snapshot file from this
        # tdb session so it doesn't get picked up by a future tdb that
        # happens to inherit the same PID.
        try:
            from tdb import processes_cache

            processes_cache.clear()
        except Exception:
            log.exception("processes_cache.clear failed in stop")

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
            await self._stepper.begin("next")
            self.state.transition_to(SessionPhase.RUNNING)
            self.state.clear_frame_data()
            await self._active_client.next(self.state.current_thread_id)

    async def step_in(self) -> None:
        if self.state.current_thread_id is not None:
            await self._stepper.begin("stepIn")
            self.state.transition_to(SessionPhase.RUNNING)
            self.state.clear_frame_data()
            await self._active_client.step_in(self.state.current_thread_id)

    async def step_out(self) -> None:
        if self.state.current_thread_id is not None:
            # Step-out crosses frames by definition, so statement-
            # granularity tracking doesn't apply.
            self._stepper.cancel()
            self.state.transition_to(SessionPhase.RUNNING)
            self.state.clear_frame_data()
            await self._active_client.step_out(self.state.current_thread_id)

    # Statement-granularity stepping lives in
    # session/statement_stepper.py. `_stepper` is built in __init__;
    # callers reach it via `step_mode` (property forwarder) and
    # `maybe_continue_statement_step` below.

    async def maybe_continue_statement_step(self) -> bool:
        """Called from the TUI's `on_stopped` handler after stack frames
        have been fetched. Delegates to `StatementStepper.maybe_continue`.
        """
        return await self._stepper.maybe_continue()

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
        if (
            not hasattr(self, "_run_to_cursor_cleanup")
            or self._run_to_cursor_cleanup is None
        ):
            return
        source_path, line = self._run_to_cursor_cleanup
        self._run_to_cursor_cleanup = None
        bps = [
            bp for bp in self.state.breakpoints.get(source_path, []) if bp.line != line
        ]
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
        thread_id = self.state.current_thread_id
        if thread_id is None:
            # Run mode: the debuggee has never stopped, so no stop event
            # ever recorded a thread id. Ask the adapter directly.
            try:
                threads = await self.client.threads()
            except Exception:
                log.exception("thread query for pause failed")
                return False
            if not threads:
                return False
            thread_id = threads[0].id
        # Clear before sending so a stale set() from a previous stop
        # doesn't make us return True instantly.
        self._stopped_event.clear()
        try:
            await self.client.pause(thread_id)
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

    async def push_all_breakpoints(self) -> None:
        """Install every enabled breakpoint over DAP.

        Used when the TUI adopts an already-configured session (run
        mode): `do_configure` ran long ago with an empty breakpoint
        map, so saved breakpoints loaded at adoption time must be sent
        explicitly.

        Parent-client only, like `_send_breakpoints`: any child
        processes already attached at adoption time (multiprocessing
        debuggees can attach children during the headless run-mode
        phase) do not get these breakpoints fanned out to them here.
        """
        if self.state.breakpoints_disabled:
            return
        for source_path, bps in self.state.breakpoints.items():
            await self._send_breakpoints(source_path, self._enabled_bps(bps))

    async def toggle_breakpoint(self, source_path: str, line: int) -> None:
        bps = self.state.breakpoints.get(source_path, [])
        existing = [bp for bp in bps if bp.line == line]
        if existing:
            bps = [bp for bp in bps if bp.line != line]
        else:
            bps.append(SourceBreakpoint(line=line))
        self.state.breakpoints[source_path] = bps

        if self.state.is_ready and not self.state.is_terminated:
            await self._send_breakpoints(source_path, self._enabled_bps(bps))

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
            await self._send_breakpoints(source_path, self._enabled_bps(bps))

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
            bps.append(
                SourceBreakpoint(
                    line=line,
                    condition=condition,
                    hit_condition=hit_condition,
                )
            )
            self.state.breakpoints[source_path] = bps

        if self.state.is_ready and not self.state.is_terminated:
            await self._send_breakpoints(
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
            await self._send_breakpoints(source_path, self._enabled_bps(new_bps))

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
            await self._send_breakpoints(source_path, self._enabled_bps(bps))

    async def disable_all_breakpoints(self) -> None:
        """Tell debugpy to remove all breakpoints without clearing them from state."""
        self.state.breakpoints_disabled = True
        if self.state.is_ready and not self.state.is_terminated:
            for source_path in self.state.breakpoints:
                await self._send_breakpoints(source_path, [])

    async def enable_all_breakpoints(self) -> None:
        """Re-send all breakpoints to debugpy."""
        self.state.breakpoints_disabled = False
        if self.state.is_ready and not self.state.is_terminated:
            for source_path, bps in self.state.breakpoints.items():
                await self._send_breakpoints(source_path, self._enabled_bps(bps))

    async def clear_all_breakpoints(self) -> None:
        """Remove all breakpoints from state and debugpy."""
        if self.state.is_ready and not self.state.is_terminated:
            for source_path in self.state.breakpoints:
                await self._send_breakpoints(source_path, [])
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

    async def resolve_evaluate_frame_id(self, client: DAPClient) -> int | None:
        """Return a real DAP frame_id usable for evaluate.

        Falls back to the active thread's top frame when the displayed
        stack is synthetic (async-task navigation, traceback parse).
        debugpy rejects unknown frame ids, so without this fallback the
        first evaluate after such a navigation would error out.
        """
        if not self.state.displayed_frames_are_synthetic:
            return self.state.current_frame_id
        tid = self.state.current_thread_id
        if tid is None:
            return None
        try:
            frames = await client.stack_trace(tid)
        except Exception:
            return None
        return frames[0].id if frames else None

    async def evaluate(self, expression: str) -> str:
        try:
            frame_id = await self.resolve_evaluate_frame_id(self._active_client)
            result, _ = await self._active_client.evaluate(
                expression,
                frame_id=frame_id,
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
                frame_id: int | None = None
                if self._active_client is self.client:
                    frame_id = await self.resolve_evaluate_frame_id(self.client)
                else:
                    threads = await self.client.threads()
                    if threads:
                        frames = await self.client.stack_trace(threads[0].id)
                        if frames:
                            frame_id = frames[0].id
                result, _ = await self.client.evaluate(
                    expression,
                    frame_id=frame_id,
                    context="repl",
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
        # Synthetic frames (async-task navigation, traceback parse)
        # aren't real DAP frames — don't try to fetch scopes for them.
        # Variables stay whatever the navigator left in place (typically
        # empty, with a "(no variables — task suspended)" hint).
        if self.state.displayed_frames_are_synthetic:
            return
        await self.fetch_scopes_and_variables(frame_id)

    async def switch_active_thread(self, thread_id: int) -> None:
        """Re-point the main views at the given thread on the parent client.

        Subsequent step / continue use `state.current_thread_id`, so this
        re-targets stepping as well as the views. Best-effort: on DAP
        failure, logs and leaves state unchanged.
        """
        try:
            self._active_client = self.client
            self.state.current_thread_id = thread_id
            frames = await self.client.stack_trace(thread_id)
            self.state.set_stack(frames)
            if frames:
                await self.fetch_scopes_and_variables(frames[0].id)
        except Exception:
            log.exception("switch_active_thread(%s) failed", thread_id)

    async def switch_active_process(self, pid: int) -> None:
        """Re-point `_active_client` at the child process for `pid`, then
        behave like `switch_active_thread` on that child's first thread.

        Matches the README's documented behavior for the Processes tab:
        Code View switches focus to that process, and subsequent step
        commands operate on it.
        """
        child = self.get_child_client(pid)
        if child is None:
            return
        try:
            self._active_client = child
            threads = await child.threads()
            if not threads:
                return
            # debugpy reports threads in creation order; index 0 is the
            # child's MainThread, which is what the user expects to land on.
            main_tid = threads[0].id
            self.state.current_thread_id = main_tid
            frames = await child.stack_trace(main_tid)
            self.state.set_stack(frames)
            if frames:
                await self.fetch_scopes_and_variables(frames[0].id)
        except Exception:
            log.exception("switch_active_process(pid=%s) failed", pid)

    def load_post_mortem(self, snapshot: dict) -> None:
        """Populate state from a snapshot produced by tdb.exception_hook.

        Thin delegator to `post_mortem_loader.load_post_mortem_into`.
        Kept on the controller so existing callers
        (cli.py post-mortem entry, tests) don't have to change.
        """
        from tdb.session.post_mortem_loader import load_post_mortem_into

        load_post_mortem_into(self.state, snapshot)

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
                frames = await ac.stack_trace(self.state.current_thread_id)
            except Exception:
                log.exception("Error fetching stack trace")
                frames = []
            self.state.set_stack(frames)
            if frames:
                try:
                    await self.fetch_scopes_and_variables(frames[0].id)
                except Exception:
                    log.exception("Error fetching scopes/variables")

    async def fetch_scopes_and_variables(self, frame_id: int) -> None:
        ac = self._active_client
        self.state.scopes = await ac.scopes(frame_id)
        self.state.variables.clear()
        for scope in self.state.scopes:
            variables = await ac.variables(scope.variables_reference)
            self.state.variables[scope.variables_reference] = variables

    async def fetch_source_content(self, source) -> str:
        """Ask the active adapter for a file's source via DAP `source`.

        Returns empty string on any failure (no live client, adapter
        rejects the request, post-mortem mode, etc.). Used by the App
        when a stack-frame's path isn't on the local filesystem — the
        canonical remote-attach case.
        """
        if self.state.is_post_mortem:
            return ""
        client = self._active_client
        if client is None:
            return ""
        return await client.source(source.source_reference, source)

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
        self.state.enter_stop(thread_id, reason)

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
        # The Processes-modal snapshot is keyed to one stopped episode;
        # any continue/step makes its frame ids and variables references
        # stale. Drop the file so the next modal open re-fetches.
        try:
            from tdb import processes_cache

            processes_cache.clear()
        except Exception:
            log.exception("processes_cache.clear failed in _on_continued")
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
        self.state.last_exit_code = exit_code
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

    def _on_breakpoint_event(self, event: Event) -> None:
        """Corrects a breakpoint's stored verified state after an adapter
        deferred it at setBreakpoints time (plan requirement 5's UI
        half). perl's compile-phase breakpoint deferral is the only
        current producer: it answers verified:false with
        DEFERRED_VERIFICATION_MESSAGE at setBreakpoints time (see
        _warn_unbound_breakpoints), then emits this "changed" event with
        the real, resolved verified state once the compile phase
        settles. Without this handler that correction was silently
        dropped and stale unverified state could never be told apart
        from a genuine bind failure after the fact.
        """
        body = event.body or {}
        if body.get("reason") != "changed":
            return
        bp = body.get("breakpoint", {})
        source_path = bp.get("source", {}).get("path")
        line = bp.get("line")
        if source_path is None or line is None:
            return
        bps = self.state.breakpoints.get(source_path)
        if not bps:
            return
        matched = [sbp for sbp in bps if sbp.line == line]
        if not matched:
            return
        for sbp in matched:
            sbp.verified = bool(bp.get("verified", False))
        enabled = self._enabled_bps(bps)
        if enabled and all(not sbp.verified for sbp in enabled):
            self._emit_unbound_warning(source_path)

    # --- Child process debugging ---
    # Per-child DAPClient lifecycle (attach, stopped-event handling,
    # close_all on quit) lives in session/child_processes.py.

    async def _pause_parent(self) -> None:
        """Pause the parent process and wait for it to stop.

        Called by ChildProcessManager.on_child_stopped to ensure the
        whole process tree is paused together — without this the user
        could see "child paused at breakpoint, parent still running"
        which is incoherent.
        """
        try:
            threads = await self.client.threads()
            if threads:
                await self.client.pause(threads[0].id)
        except Exception:
            pass

    async def _pause_children(self) -> None:
        """Delegate to ChildProcessManager (kept on controller for the
        couple of _spawn_bg callers that still reach for it)."""
        await self._children.pause_all()
