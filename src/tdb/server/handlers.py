"""JSON-RPC handler methods, decoupled from FastAPI/uvicorn.

Each RPC action is an `action_<name>` method on `RpcHandlers`. The
class is constructed once with a `ControllerRef` (so it follows
controller swaps on restart) and a `ServerEventHandler` (which holds
the asyncio events the step/continue path awaits).

Splitting these out of `create_app` lets unit tests drive each handler
in-process — no uvicorn subprocess, no port allocation — which is the
fast path for verifying RPC behavior. `tdb.server.app.create_app`
becomes a thin wrapper that maps HTTP requests onto handler methods.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from tdb.inspection import (
    build_wait_graph,
    find_cycles,
)
from tdb.session.controller import DebugController
from tdb.session.inspect_service import InspectService, SessionGateError
from .event_handler import ServerEventHandler
from .rpc_types import RpcResponse

log = logging.getLogger(__name__)


class ControllerRef:
    """Mutable holder so handler closures always see the current controller.

    In dual mode (TUI + server), the TUI may replace the controller on
    restart. Every handler reads through this ref.
    """

    def __init__(self, controller: DebugController) -> None:
        self.ctrl = controller

    def set(self, controller: DebugController) -> None:
        self.ctrl = controller


class HandlerRef:
    """Mutable holder so RPC + SSE always see the current ServerEventHandler.

    Symmetric with `ControllerRef`. On restart the TUI calls `set(new)`
    instead of mutating private fields on the old handler — open SSE
    subscriber queues are migrated and a `session_restart` event is
    broadcast so connected clients can reset their UI before events
    from the new session start arriving.
    """

    def __init__(self, handler: ServerEventHandler) -> None:
        self.h = handler

    def set(self, new_handler: ServerEventHandler) -> None:
        old = self.h
        if new_handler is old:
            return
        # Migrate live SSE subscribers so /events connections opened
        # before the restart keep flowing.
        new_handler._sse_subscribers = old._sse_subscribers
        # Tell connected clients a new session has begun. Broadcast on
        # the new handler so the migrated subscribers see it.
        new_handler._broadcast_sse("session_restart", {})
        self.h = new_handler


def _parse_file_line(spec: str) -> tuple[str, int]:
    """Split 'file:line' into (resolved_path, line). Raises ValueError on bad input."""
    if ":" not in spec:
        raise ValueError("expected 'file:line'")
    file_part, line_part = spec.rsplit(":", 1)
    line = int(line_part)  # raises ValueError if not numeric
    return str(Path(file_part).resolve()), line


class RpcHandlers:
    """All JSON-RPC actions as methods. One instance per running server.

    Each action method takes `params: list[Any]` and returns
    `RpcResponse`. The mapping from RPC action name to method is
    declared in `ACTIONS` so the dispatcher can build it once.
    """

    # Action name → method-name suffix. Method is `self.action_<suffix>`.
    # Listed in the order the help endpoint surfaces them.
    ACTIONS: tuple[str, ...] = (
        "help",
        "set_breakpoint",
        "remove_breakpoint",
        "list_breakpoints",
        "next",
        "step_in",
        "step_out",
        "continue",
        "pause",
        "wait_for_stop",
        "inspect",
        "evaluate",
        "stack_up",
        "stack_down",
        "get_stack_trace",
        "get_output",
        "get_source",
        "list_threads",
        "inspect_thread",
        "list_processes",
        "inspect_process",
        "list_tasks",
        "inspect_task",
        "wait_graph",
        "rust_concurrency",
        "restart",
        "status",
        "quit",
    )

    ACTION_HELP: dict[str, str] = {
        "help": "params: []",
        "set_breakpoint": 'params: ["file:line"] or ["file:line", "condition", "hit_condition"]',
        "remove_breakpoint": 'params: ["file:line"]',
        "list_breakpoints": "params: []",
        "next": "params: [] or [timeout_s]",
        "step_in": "params: [] or [timeout_s]",
        "continue": "params: [] or [timeout_s]",
        "pause": "params: []  -- bypasses the dispatch lock; can interrupt a blocked step / continue / wait_for_stop",
        "wait_for_stop": "params: [] or [timeout_s]  -- wait for the next stop without issuing a step",
        "inspect": 'params: ["expr1", "expr2", ...]',
        "evaluate": 'params: ["expression"]',
        "stack_up": "params: []",
        "stack_down": "params: []",
        "get_stack_trace": "params: []",
        "get_output": "params: []  -- drain buffered stdout/stderr",
        "get_source": 'params: ["file_path"]  -- read source file contents',
        "list_threads": "params: []  -- list all threads",
        "inspect_thread": "params: [thread_id]  -- inspect a specific thread",
        "list_processes": "params: []  -- list child processes (multiprocessing)",
        "inspect_process": 'params: ["name_or_pid"]  -- inspect a specific child process',
        "list_tasks": "params: []  -- list all asyncio tasks",
        "inspect_task": 'params: ["task_name"]  -- inspect a specific asyncio task',
        "wait_graph": "params: []  -- show task wait-graph and any deadlock cycles",
        "rust_concurrency": "params: []  -- show Rust thread and synchronization findings",
        "restart": "params: []",
        "status": "params: []",
        "quit": "params: []",
        "/events": "GET endpoint (not an action) -- SSE stream of DAP events",
    }

    def __init__(
        self,
        controller_ref: ControllerRef,
        event_handler: ServerEventHandler | HandlerRef,
    ) -> None:
        self._controller_ref = controller_ref
        # Accept either a raw handler (tests, headless runner) or a
        # HandlerRef (TUI dual-mode, where the handler swaps on restart).
        # Always store as a ref so reads go through the indirection.
        if isinstance(event_handler, HandlerRef):
            self._handler_ref = event_handler
        else:
            self._handler_ref = HandlerRef(event_handler)
        # Shared fetch-and-parse workflows (tasks / threads / processes),
        # common with the TUI's InspectionWorkflows. Reads the controller
        # through the ref so it follows restart swaps.
        self._inspect = InspectService(lambda: self._controller_ref.ctrl)

    def _gate_error(self, e: SessionGateError, doing: str) -> RpcResponse:
        """Map a SessionGateError onto this API's established wording."""
        if e.reason == "unsupported":
            lang = self.controller.profile.display_name
            return RpcResponse.error(f"Not supported when debugging {lang}")
        if e.reason == "terminated":
            return RpcResponse.error("Program has terminated")
        return RpcResponse.error(f"Cannot {doing} while program is running")

    # --- Convenience accessors ------------------------------------------

    @property
    def controller(self) -> DebugController:
        return self._controller_ref.ctrl

    @property
    def event_handler(self) -> ServerEventHandler:
        return self._handler_ref.h

    @property
    def session_lock(self) -> asyncio.Lock:
        """Serializes RPC actions against each other. The dispatcher
        acquires this around every call so concurrent HTTP requests can't
        interleave a step + an evaluate."""
        return self._controller_ref.ctrl.session_lock

    def dispatch_table(self) -> dict[str, Callable[[list[Any]], Any]]:
        """Return name → bound-method map for the RPC endpoint."""
        return {
            "help": self.action_help,
            "set_breakpoint": self.action_set_breakpoint,
            "remove_breakpoint": self.action_remove_breakpoint,
            "list_breakpoints": self.action_list_breakpoints,
            "next": self.action_next,
            "step_in": self.action_step_in,
            "step_out": self.action_step_out,
            "continue": self.action_continue,
            "pause": self.action_pause,
            "wait_for_stop": self.action_wait_for_stop,
            "inspect": self.action_inspect,
            "evaluate": self.action_evaluate,
            "stack_up": self.action_stack_up,
            "stack_down": self.action_stack_down,
            "get_stack_trace": self.action_get_stack_trace,
            "get_output": self.action_get_output,
            "get_source": self.action_get_source,
            "list_threads": self.action_list_threads,
            "inspect_thread": self.action_inspect_thread,
            "list_processes": self.action_list_processes,
            "inspect_process": self.action_inspect_process,
            "list_tasks": self.action_list_tasks,
            "inspect_task": self.action_inspect_task,
            "wait_graph": self.action_wait_graph,
            "rust_concurrency": self.action_rust_concurrency,
            "restart": self.action_restart,
            "status": self.action_status,
            "quit": self.action_quit,
        }

    # --- Helpers --------------------------------------------------------

    # Returned (success) when a blocking step / wait expires before the
    # debuggee stops. MCP agents and scripted callers pivot on this exact
    # wording to decide whether to issue `pause` or call `wait_for_stop`
    # again — keep it stable.
    STILL_RUNNING_MSG = "still running — call pause or wait again"

    @staticmethod
    def _parse_timeout(params: list[Any], default: float | None) -> float | None:
        """Extract an optional timeout from params[0]. Falls back to default.

        Returning `None` means "use the handler's wait ceiling" — the four
        step actions pass `None` so the existing `RPC_STEP_WAIT` default
        applies when no timeout is supplied, preserving HTTP-API behavior.
        """
        if not params:
            return default
        try:
            return float(params[0])
        except (TypeError, ValueError):
            return default

    async def _step_action(
        self,
        action_fn: Callable[[], Any],
        timeout_s: float | None = None,
    ) -> RpcResponse:
        """Common handler for next / step_in / step_out / continue.

        State (is_running, stop_reason, current_thread_id, is_terminated)
        is updated synchronously by the controller's DAP event handlers
        (`_on_stopped`, `_on_continued`, `_on_terminated`, `_on_exited`),
        so by the time `wait_for_stop` returns, controller.state is
        already current — no sync step needed.

        `timeout_s` caps how long we block awaiting the next stop. On
        timeout, returns success with `STILL_RUNNING_MSG` so callers
        (especially agentic ones) can poll: either issue `pause` to
        interrupt or call `wait_for_stop` to keep waiting without
        re-issuing the step.
        """
        ctrl = self.controller
        if ctrl.state.is_terminated:
            return RpcResponse.error("Program has terminated")
        if ctrl.state.is_running:
            return RpcResponse.error("Program is already running")
        self.event_handler.reset_for_continue()
        await action_fn()
        return await self._await_stop_loop(timeout_s)

    async def _await_stop_loop(self, timeout_s: float | None) -> RpcResponse:
        """Wait for the next stop, honoring statement-step continuation.

        Shared by `_step_action` (after issuing a step) and
        `action_wait_for_stop` (no step issued — pure wait).
        """
        from tdb._timeouts import RPC_STEP_WAIT

        ctrl = self.controller
        # Long ceiling: after releasing all breakpoint hits in a multi-process
        # program, the remainder of the script can run for a while before the
        # `terminated` event wakes us. Callers wanting agent-friendly polling
        # pass a shorter `timeout_s`.
        wait = RPC_STEP_WAIT if timeout_s is None else timeout_s
        while True:
            stopped = await self.event_handler.wait_for_stop(timeout=wait)
            if not stopped:
                return RpcResponse.ok(self.STILL_RUNNING_MSG)
            if ctrl.state.is_terminated:
                output = self.event_handler.drain_output()
                return RpcResponse.ok(
                    output
                    or f"Program terminated (exit code {self.event_handler.exit_code})"
                )
            await ctrl.fetch_stop_info()
            # Statement-granularity step may keep stepping until we leave
            # the originating statement. The controller's safety cap bounds
            # the loop. Each iteration is a fresh DAP next/stepIn round-trip.
            if await ctrl.maybe_continue_statement_step():
                self.event_handler.reset_for_continue()
                continue
            break
        await ctrl.cleanup_run_to_cursor()
        loc, line = ctrl.state.get_stop_location()
        output = self.event_handler.drain_output()
        value = f"{loc}:{line}"
        if output:
            value = f"{value}\n{output}"
        return RpcResponse.ok(value)

    # --- Action methods -------------------------------------------------

    async def action_help(self, params: list[Any]) -> RpcResponse:
        lines = [f"  {action}: {args}" for action, args in self.ACTION_HELP.items()]
        return RpcResponse.ok("\n".join(lines))

    async def action_set_breakpoint(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be 'file:line'")
        try:
            source_path, line = _parse_file_line(str(params[0]))
        except ValueError:
            return RpcResponse.error("params[0] must be 'file:line'")
        condition = str(params[1]) if len(params) > 1 and params[1] else None
        hit_condition = str(params[2]) if len(params) > 2 and params[2] else None
        # Breakpoints land on logical statement starts. Snap if needed
        # and surface the move in the response so scripted callers know.
        from tdb.source_analysis import snap_breakpoint

        snapped = snap_breakpoint(source_path, line)
        if snapped is None:
            return RpcResponse.error(
                f"{source_path}:{line} has no logical statement at or "
                f"before that line; breakpoint not set",
            )
        original_line = line
        line = snapped
        await self.controller.add_breakpoint(
            source_path, line, condition, hit_condition
        )
        if line != original_line:
            return RpcResponse.ok(
                f"breakpoint set at {source_path}:{line} "
                f"(moved from line {original_line} — not a logical "
                f"statement start)",
            )
        return RpcResponse.ok()

    async def action_remove_breakpoint(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be 'file:line'")
        try:
            source_path, line = _parse_file_line(str(params[0]))
        except ValueError:
            return RpcResponse.error("params[0] must be 'file:line'")
        await self.controller.remove_breakpoint(source_path, line)
        return RpcResponse.ok()

    async def action_list_breakpoints(self, params: list[Any]) -> RpcResponse:
        bps = self.controller.state.breakpoints
        if not bps:
            return RpcResponse.ok("No breakpoints set")
        lines = []
        for source_path, bp_list in bps.items():
            for bp in bp_list:
                entry = f"{source_path}:{bp.line}"
                if bp.condition:
                    entry += f"  condition={bp.condition}"
                if bp.hit_condition:
                    entry += f"  hit_condition={bp.hit_condition}"
                lines.append(entry)
        return RpcResponse.ok("\n".join(lines))

    async def action_next(self, params: list[Any]) -> RpcResponse:
        return await self._step_action(
            self.controller.step_over,
            timeout_s=self._parse_timeout(params, None),
        )

    async def action_step_in(self, params: list[Any]) -> RpcResponse:
        return await self._step_action(
            self.controller.step_in,
            timeout_s=self._parse_timeout(params, None),
        )

    async def action_step_out(self, params: list[Any]) -> RpcResponse:
        return await self._step_action(
            self.controller.step_out,
            timeout_s=self._parse_timeout(params, None),
        )

    async def action_continue(self, params: list[Any]) -> RpcResponse:
        return await self._step_action(
            self.controller.continue_,
            timeout_s=self._parse_timeout(params, None),
        )

    async def action_wait_for_stop(self, params: list[Any]) -> RpcResponse:
        """Wait for the next stop without issuing a step.

        After a step / continue returns `STILL_RUNNING_MSG` on timeout, an
        agent that wants to keep waiting can't re-call `continue` (the
        `is_running` guard rejects it). This action re-enters the wait
        loop directly. `params[0]` is an optional timeout in seconds.
        """
        ctrl = self.controller
        if ctrl.state.is_terminated:
            return RpcResponse.error("Program has terminated")
        return await self._await_stop_loop(self._parse_timeout(params, None))

    async def action_pause(self, params: list[Any]) -> RpcResponse:
        if self.controller.state.is_terminated:
            return RpcResponse.error("Program has terminated")
        paused = await self.controller.pause()
        if not paused:
            return RpcResponse.error(
                "Pause didn't land within timeout — debuggee may have no "
                "Python frames running (e.g. fully-deadlocked asyncio loop). "
                "Try setting a breakpoint at a known reachable line."
            )
        return RpcResponse.ok()

    async def action_inspect(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params must contain variable names/expressions")
        if self.controller.state.is_running:
            return RpcResponse.error("Cannot inspect while program is running")
        results = []
        for expr in params:
            value = await self.controller.evaluate(str(expr))
            results.append(f"{expr} = {value}")
        return RpcResponse.ok("\n".join(results))

    async def action_evaluate(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be an expression")
        if self.controller.state.is_running:
            return RpcResponse.error("Cannot evaluate while program is running")
        result = await self.controller.evaluate(str(params[0]))
        return RpcResponse.ok(result)

    async def action_stack_up(self, params: list[Any]) -> RpcResponse:
        moved = await self.controller.navigate_stack(up=True)
        if not moved:
            return RpcResponse.error("Already at top of stack")
        loc, line = self.controller.state.get_stop_location()
        src = self.controller.state.get_current_source_path() or loc
        cur_line = self.controller.state.get_current_line() or line
        return RpcResponse.ok(f"{src}:{cur_line}")

    async def action_stack_down(self, params: list[Any]) -> RpcResponse:
        moved = await self.controller.navigate_stack(up=False)
        if not moved:
            return RpcResponse.error("Already at bottom of stack")
        loc, line = self.controller.state.get_stop_location()
        src = self.controller.state.get_current_source_path() or loc
        cur_line = self.controller.state.get_current_line() or line
        return RpcResponse.ok(f"{src}:{cur_line}")

    async def action_get_stack_trace(self, params: list[Any]) -> RpcResponse:
        ctrl = self.controller
        frames = ctrl.state.stack_frames
        if not frames:
            return RpcResponse.error("No stack trace available")
        lines = []
        for i, frame in enumerate(frames):
            src = (
                frame.source.path if frame.source and frame.source.path else "<unknown>"
            )
            marker = " *" if frame.id == ctrl.state.current_frame_id else ""
            lines.append(f"#{i} {frame.name} at {src}:{frame.line}{marker}")
        return RpcResponse.ok("\n".join(lines))

    async def action_restart(self, params: list[Any]) -> RpcResponse:
        ctrl = self.controller
        saved_breakpoints = dict(ctrl.state.breakpoints)
        try:
            await ctrl.stop()
        except Exception:
            log.exception("Error stopping session for restart")

        # Reinitialize the controller in-place. Must pass the profile's
        # adapter through — a bare `__init__()` silently reverts to
        # DAPClient's default (DebugpyAdapter), breaking restart for
        # non-Python sessions.
        ctrl.client.__init__(ctrl.profile.adapter)
        ctrl.state.__init__()
        ctrl.state.breakpoints = saved_breakpoints
        ctrl._terminal = None

        # Reset handler events
        eh = self.event_handler
        eh.initialized_event.clear()
        eh.stopped_event.clear()
        eh.terminated_event.clear()
        eh.exit_code = None
        eh.last_stop_thread_id = None
        eh.last_stop_reason = None
        eh.last_stop_description = None
        eh.last_stop_text = None
        eh._output_buffer.clear()

        p = ctrl._launch_params
        await ctrl.start(
            program=p["program"],
            args=p["args"],
            cwd=p["cwd"],
            stop_on_entry=p["stop_on_entry"],
            just_my_code=p["just_my_code"],
            python=p["python"],
        )

        # Wait for initialized then configure
        from tdb._timeouts import DAP_INITIALIZED, DAP_STOP_ON_ENTRY

        await asyncio.wait_for(eh.initialized_event.wait(), timeout=DAP_INITIALIZED)
        await ctrl.do_configure()

        # Mirror setup_headless_session: when the relaunched session stops
        # on entry, wait for that stop and sync stack/scope state before
        # returning — otherwise state.phase never leaves RUNNING and the
        # very next `continue` in a replay fails with "Program is already
        # running" even though the debuggee is, in fact, parked at entry.
        if p["stop_on_entry"]:
            stopped = await eh.wait_for_stop(timeout=DAP_STOP_ON_ENTRY)
            if not stopped:
                return RpcResponse.error(
                    f"Timed out waiting for stop-on-entry after restart ({DAP_STOP_ON_ENTRY:.0f}s)"
                )
            await ctrl.fetch_stop_info()
        return RpcResponse.ok("Session restarted")

    async def action_status(self, params: list[Any]) -> RpcResponse:
        state = self.controller.state
        if state.is_terminated:
            return RpcResponse.ok("terminated")
        if state.is_running:
            return RpcResponse.ok("running")
        loc, line = state.get_stop_location()
        reason = state.stop_reason or "stopped"
        return RpcResponse.ok(f"stopped ({reason}) at {loc}:{line}")

    async def action_quit(self, params: list[Any]) -> RpcResponse:
        try:
            await self.controller.stop()
        except Exception:
            log.exception("Error stopping controller on quit")
        return RpcResponse.ok("Server shutting down")

    async def action_get_output(self, params: list[Any]) -> RpcResponse:
        return RpcResponse.ok(self.event_handler.drain_output())

    async def action_get_source(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be a file path")
        file_path = str(Path(params[0]).resolve())
        if not Path(file_path).is_file():
            return RpcResponse.error(f"File not found: {params[0]}")
        try:
            text = Path(file_path).read_text()
        except Exception as e:
            return RpcResponse.error(f"Cannot read file: {e}")
        return RpcResponse.ok(text)

    async def action_list_tasks(self, params: list[Any]) -> RpcResponse:
        try:
            tasks = await self._inspect.collect_tasks()
        except SessionGateError as e:
            return self._gate_error(e, "list tasks")
        except Exception as e:
            return RpcResponse.error(f"Failed to collect tasks: {e}")
        if not tasks:
            return RpcResponse.ok("No asyncio tasks found")
        lines = []
        for t in tasks:
            extras = []
            if t.awaiting:
                extras.append(f"awaiting={t.awaiting}")
            if t.cancelling:
                extras.append(f"cancelling={t.cancelling}")
            extra_str = f"  ({', '.join(extras)})" if extras else ""
            lines.append(f"{t.name}  [{t.state}]{extra_str}  {t.coro}")
        return RpcResponse.ok("\n".join(lines))

    async def action_inspect_task(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be a task name")
        target_name = str(params[0])
        try:
            tasks = await self._inspect.collect_tasks()
        except SessionGateError as e:
            return self._gate_error(e, "inspect tasks")
        except Exception as e:
            return RpcResponse.error(f"Failed to collect tasks: {e}")
        task = next((t for t in tasks if t.name == target_name), None)
        if task is None:
            names = [t.name for t in tasks]
            return RpcResponse.error(
                f"Task '{target_name}' not found. Active tasks: {names}"
            )
        lines = [
            f"Name:  {task.name}",
            f"State: {task.state}",
        ]
        if task.awaiting:
            lines.append(f"Awaiting: {task.awaiting}")
        if task.cancelling:
            lines.append(f"Cancelling: {task.cancelling} pending request(s)")
        if task.cancel_message:
            lines.append(f"Cancel msg: {task.cancel_message}")
        lines.extend(
            [
                f"Coro:  {task.coro}",
                "",
                "Stack:",
            ]
        )
        if task.stack:
            for i, frame in enumerate(task.stack):
                lines.append(f"  #{i} {frame}")
        else:
            lines.append("  (no stack frames — task may be awaiting)")
        lines.append("")
        lines.append("Variables:")
        # Shared with the TUI's task-variables flow: evaluates against
        # the active client (parent or child) with a real frame id —
        # synthetic (negative) frame ids from task navigation are routed
        # around inside the service. The service re-gates on entry, so a
        # phase change between collect_tasks above and this call (e.g. a
        # `terminated` event landing mid-action) raises SessionGateError
        # here too — map it like the initial gate rather than letting it
        # escape to the dispatcher's generic handler.
        try:
            variables = await self._inspect.task_locals(target_name)
        except SessionGateError as e:
            return self._gate_error(e, "inspect tasks")
        if variables:
            for v in variables:
                type_str = f" ({v.type})" if v.type else ""
                lines.append(f"  {v.name}{type_str} = {v.value}")
        else:
            lines.append("  (no variables — frame not available)")
        return RpcResponse.ok("\n".join(lines))

    async def action_wait_graph(self, params: list[Any]) -> RpcResponse:
        try:
            tasks = await self._inspect.collect_tasks()
        except SessionGateError as e:
            return self._gate_error(e, "compute wait graph")
        except Exception as e:
            return RpcResponse.error(f"Failed to collect tasks: {e}")
        if not tasks:
            return RpcResponse.ok("No asyncio tasks found")

        graph = build_wait_graph(tasks)
        cycles = find_cycles(graph)
        blocked = [t for t in tasks if t.awaiting is not None]

        header = f"Wait graph: {len(tasks)} task(s), {len(blocked)} blocked"
        if cycles:
            header += f", {len(cycles)} deadlock cycle(s)"
        lines: list[str] = [header, ""]

        if blocked:
            lines.append("Blocked tasks:")
            for t in blocked:
                holders_str = (
                    ", ".join(t.holders) if t.holders else "(no holder identified)"
                )
                lines.append(f"  {t.name}  ->  {t.awaiting}  (holders: {holders_str})")
            lines.append("")
        else:
            lines.append("No blocked tasks")
            lines.append("")

        if cycles:
            lines.append("Deadlock cycles:")
            for cycle in cycles:
                if len(cycle) == 1:
                    lines.append(f"  - {cycle[0]} (self-cycle)")
                else:
                    lines.append(f"  - {' <-> '.join(cycle)}")

        return RpcResponse.ok("\n".join(lines).rstrip())

    async def action_rust_concurrency(self, params: list[Any]) -> RpcResponse:
        """Return the stopped Rust concurrency snapshot as structured JSON data."""
        if params:
            return RpcResponse.error("rust_concurrency does not accept parameters")
        try:
            snapshot = await self._inspect.collect_rust_concurrency()
        except SessionGateError as e:
            return self._gate_error(e, "inspect Rust concurrency")
        except Exception as e:
            return RpcResponse.error(f"Failed to collect Rust concurrency: {e}")
        return RpcResponse.ok_data(snapshot.to_dict())

    async def action_list_threads(self, params: list[Any]) -> RpcResponse:
        try:
            threads = await self._inspect.list_threads()
        except SessionGateError as e:
            return self._gate_error(e, "list threads")
        except Exception as e:
            return RpcResponse.error(f"Failed to fetch threads: {e}")
        if not threads:
            return RpcResponse.ok("No threads found")
        current_id = self.controller.state.current_thread_id
        lines = []
        for t in threads:
            marker = " *" if t.id == current_id else ""
            lines.append(f"{t.id}: {t.name}{marker}")
        return RpcResponse.ok("\n".join(lines))

    async def action_inspect_thread(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be a thread ID")
        try:
            thread_id = int(params[0])
        except (ValueError, TypeError):
            return RpcResponse.error(f"Invalid thread ID: {params[0]}")
        try:
            threads = await self._inspect.list_threads()
        except SessionGateError as e:
            return self._gate_error(e, "inspect threads")
        except Exception as e:
            return RpcResponse.error(f"Failed to fetch threads: {e}")
        thread = next((t for t in threads if t.id == thread_id), None)
        if thread is None:
            ids = [t.id for t in threads]
            return RpcResponse.error(
                f"Thread {thread_id} not found. Active threads: {ids}"
            )
        lines = [
            f"Thread ID: {thread.id}",
            f"Name:      {thread.name}",
            "",
            "Stack:",
        ]
        try:
            frames, scopes, variables = await self._inspect.thread_stack(thread_id)
        except SessionGateError as e:
            # Phase changed between list_threads above and the stack
            # fetch — report it as the gate condition it is, not as a
            # stack-trace failure.
            return self._gate_error(e, "inspect threads")
        except Exception:
            lines.append("  (failed to fetch stack trace)")
            return RpcResponse.ok("\n".join(lines))
        if frames:
            for i, frame in enumerate(frames):
                src = (
                    frame.source.path
                    if frame.source and frame.source.path
                    else "<unknown>"
                )
                lines.append(f"  #{i} {frame.name} at {src}:{frame.line}")
            lines.append("")
            lines.append("Variables:")
            for scope in scopes:
                for v in variables.get(scope.variables_reference, []):
                    type_str = f" ({v.type})" if v.type else ""
                    lines.append(f"  {v.name}{type_str} = {v.value}")
        else:
            lines.append("  (no stack frames)")
        return RpcResponse.ok("\n".join(lines))

    async def action_list_processes(self, params: list[Any]) -> RpcResponse:
        try:
            processes = await self._inspect.collect_processes()
        except SessionGateError as e:
            return self._gate_error(e, "list processes")
        except Exception as e:
            return RpcResponse.error(f"Failed to collect processes: {e}")
        if not processes:
            return RpcResponse.ok("No child processes found")
        lines = []
        for p in processes:
            status = "alive" if p.alive else f"exited({p.exitcode})"
            pid = str(p.pid) if p.pid is not None else "—"
            lines.append(f"{pid}: {p.name}  [{status}]  {p.target}")
        return RpcResponse.ok("\n".join(lines))

    async def action_inspect_process(self, params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be a process name or PID")
        target = str(params[0])
        try:
            processes = await self._inspect.collect_processes()
        except SessionGateError as e:
            return self._gate_error(e, "inspect processes")
        except Exception as e:
            return RpcResponse.error(f"Failed to collect processes: {e}")
        proc = None
        try:
            pid = int(target)
            proc = next((p for p in processes if p.pid == pid), None)
        except ValueError:
            proc = next((p for p in processes if p.name == target), None)
        if proc is None:
            names = [f"{p.pid}:{p.name}" for p in processes]
            return RpcResponse.error(f"Process '{target}' not found. Active: {names}")
        lines = [
            f"Name:    {proc.name}",
            f"PID:     {proc.pid if proc.pid is not None else 'not started'}",
            f"Status:  {'alive' if proc.alive else f'exited (code {proc.exitcode})'}",
            f"Daemon:  {proc.daemon}",
        ]
        if proc.start_method:
            lines.append(f"Method:  {proc.start_method}")
        lines.append(f"Target:  {proc.target}")
        lines.append(f"Args:    {proc.args}")
        if proc.kwargs and proc.kwargs != "{}":
            lines.append(f"Kwargs:  {proc.kwargs}")
        return RpcResponse.ok("\n".join(lines))
