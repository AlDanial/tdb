"""FastAPI application exposing a JSON-RPC endpoint for debug control."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from tdb.session.controller import DebugController
from .event_handler import ServerEventHandler
from .rpc_types import RpcRequest, RpcResponse

log = logging.getLogger(__name__)


class ControllerRef:
    """Mutable holder so FastAPI closures always see the current controller.

    In dual mode (TUI + server), the TUI may replace the controller on
    restart.  This wrapper lets the server follow along.
    """

    def __init__(self, controller: DebugController) -> None:
        self.ctrl = controller

    def set(self, controller: DebugController) -> None:
        self.ctrl = controller


def create_app(controller_ref: ControllerRef, handler: ServerEventHandler) -> FastAPI:
    """Build the FastAPI app wired to the given controller ref and event handler."""

    app = FastAPI(title="tdb debug server")

    # Convenience alias — every closure reads through controller_ref.ctrl
    def _ctrl() -> DebugController:
        return controller_ref.ctrl

    # ------------------------------------------------------------------
    # Action dispatch table
    # ------------------------------------------------------------------

    async def _set_breakpoint(params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be 'file:line'")
        location = str(params[0])
        if ":" not in location:
            return RpcResponse.error("params[0] must be 'file:line'")
        file_part, line_part = location.rsplit(":", 1)
        try:
            line = int(line_part)
        except ValueError:
            return RpcResponse.error(f"Invalid line number: {line_part}")
        source_path = str(Path(file_part).resolve())
        condition = str(params[1]) if len(params) > 1 and params[1] else None
        hit_condition = str(params[2]) if len(params) > 2 and params[2] else None
        await _ctrl().add_breakpoint(source_path, line, condition, hit_condition)
        return RpcResponse.ok()

    async def _remove_breakpoint(params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be 'file:line'")
        location = str(params[0])
        if ":" not in location:
            return RpcResponse.error("params[0] must be 'file:line'")
        file_part, line_part = location.rsplit(":", 1)
        try:
            line = int(line_part)
        except ValueError:
            return RpcResponse.error(f"Invalid line number: {line_part}")
        source_path = str(Path(file_part).resolve())
        await _ctrl().remove_breakpoint(source_path, line)
        return RpcResponse.ok()

    def _sync_state_from_stop() -> None:
        """Update controller state from the last stop event."""
        state = _ctrl().state
        state.is_running = False
        state.stop_reason = handler.last_stop_reason
        if handler.last_stop_thread_id is not None:
            state.current_thread_id = handler.last_stop_thread_id

    async def _step_action(action_fn: Any) -> RpcResponse:
        """Common handler for next/step_in/step_out/continue."""
        if _ctrl().state.is_terminated:
            return RpcResponse.error("Program has terminated")
        if _ctrl().state.is_running:
            return RpcResponse.error("Program is already running")
        handler.reset_for_continue()
        await action_fn()
        stopped = await handler.wait_for_stop(timeout=30.0)
        if not stopped:
            return RpcResponse.error("Timeout waiting for stop")
        _sync_state_from_stop()
        if _ctrl().state.is_terminated:
            output = handler.drain_output()
            return RpcResponse.ok(output or f"Program terminated (exit code {handler.exit_code})")
        await _ctrl().fetch_stop_info()
        await _ctrl().cleanup_run_to_cursor()
        loc, line = _ctrl().state.get_stop_location()
        output = handler.drain_output()
        value = f"{loc}:{line}"
        if output:
            value = f"{value}\n{output}"
        return RpcResponse.ok(value)

    async def _next(params: list[Any]) -> RpcResponse:
        return await _step_action(_ctrl().step_over)

    async def _step_in(params: list[Any]) -> RpcResponse:
        return await _step_action(_ctrl().step_in)

    async def _step_out(params: list[Any]) -> RpcResponse:
        return await _step_action(_ctrl().step_out)

    async def _continue(params: list[Any]) -> RpcResponse:
        return await _step_action(_ctrl().continue_)

    async def _pause(params: list[Any]) -> RpcResponse:
        if _ctrl().state.is_terminated:
            return RpcResponse.error("Program has terminated")
        await _ctrl().pause()
        return RpcResponse.ok()

    async def _inspect(params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params must contain variable names/expressions")
        if _ctrl().state.is_running:
            return RpcResponse.error("Cannot inspect while program is running")
        results = []
        for expr in params:
            value = await _ctrl().evaluate(str(expr))
            results.append(f"{expr} = {value}")
        return RpcResponse.ok("\n".join(results))

    async def _evaluate(params: list[Any]) -> RpcResponse:
        if not params:
            return RpcResponse.error("params[0] must be an expression")
        if _ctrl().state.is_running:
            return RpcResponse.error("Cannot evaluate while program is running")
        result = await _ctrl().evaluate(str(params[0]))
        return RpcResponse.ok(result)

    async def _stack_up(params: list[Any]) -> RpcResponse:
        moved = await _ctrl().navigate_stack(up=True)
        if not moved:
            return RpcResponse.error("Already at top of stack")
        loc, line = _ctrl().state.get_stop_location()
        src = _ctrl().state.get_current_source_path() or loc
        cur_line = _ctrl().state.get_current_line() or line
        return RpcResponse.ok(f"{src}:{cur_line}")

    async def _stack_down(params: list[Any]) -> RpcResponse:
        moved = await _ctrl().navigate_stack(up=False)
        if not moved:
            return RpcResponse.error("Already at bottom of stack")
        loc, line = _ctrl().state.get_stop_location()
        src = _ctrl().state.get_current_source_path() or loc
        cur_line = _ctrl().state.get_current_line() or line
        return RpcResponse.ok(f"{src}:{cur_line}")

    async def _get_stack_trace(params: list[Any]) -> RpcResponse:
        frames = _ctrl().state.stack_frames
        if not frames:
            return RpcResponse.error("No stack trace available")
        lines = []
        for i, frame in enumerate(frames):
            src = frame.source.path if frame.source and frame.source.path else "<unknown>"
            marker = " *" if frame.id == _ctrl().state.current_frame_id else ""
            lines.append(f"#{i} {frame.name} at {src}:{frame.line}{marker}")
        return RpcResponse.ok("\n".join(lines))

    async def _restart(params: list[Any]) -> RpcResponse:
        ctrl = _ctrl()
        saved_breakpoints = dict(ctrl.state.breakpoints)
        try:
            await ctrl.stop()
        except Exception:
            log.exception("Error stopping session for restart")

        # Reinitialize the controller in-place
        ctrl.client.__init__()
        ctrl.state.__init__()
        ctrl.state.breakpoints = saved_breakpoints
        ctrl._external_terminal = False

        # Reset handler events
        handler.initialized_event.clear()
        handler.stopped_event.clear()
        handler.terminated_event.clear()
        handler.exit_code = None
        handler.last_stop_thread_id = None
        handler.last_stop_reason = None
        handler.last_stop_description = None
        handler.last_stop_text = None
        handler._output_buffer.clear()

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
        await asyncio.wait_for(handler.initialized_event.wait(), timeout=10.0)
        await ctrl.do_configure()
        return RpcResponse.ok("Session restarted")

    async def _status(params: list[Any]) -> RpcResponse:
        state = _ctrl().state
        if state.is_terminated:
            return RpcResponse.ok("terminated")
        if state.is_running:
            return RpcResponse.ok("running")
        loc, line = state.get_stop_location()
        reason = state.stop_reason or "stopped"
        return RpcResponse.ok(f"stopped ({reason}) at {loc}:{line}")

    async def _quit(params: list[Any]) -> RpcResponse:
        try:
            await _ctrl().stop()
        except Exception:
            log.exception("Error stopping controller on quit")
        return RpcResponse.ok("Server shutting down")

    async def _get_output(params: list[Any]) -> RpcResponse:
        output = handler.drain_output()
        return RpcResponse.ok(output)

    async def _get_source(params: list[Any]) -> RpcResponse:
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

    async def _list_breakpoints(params: list[Any]) -> RpcResponse:
        bps = _ctrl().state.breakpoints
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

    _ACTION_HELP: dict[str, str] = {
        "help": "params: []",
        "set_breakpoint": 'params: ["file:line"] or ["file:line", "condition", "hit_condition"]',
        "remove_breakpoint": 'params: ["file:line"]',
        "list_breakpoints": "params: []",
        "next": "params: []",
        "step_in": "params: []",
        "step_out": "params: []",
        "continue": "params: []",
        "pause": "params: []",
        "inspect": 'params: ["expr1", "expr2", ...]',
        "evaluate": 'params: ["expression"]',
        "stack_up": "params: []",
        "stack_down": "params: []",
        "get_stack_trace": "params: []",
        "get_output": "params: []  -- drain buffered stdout/stderr",
        "get_source": 'params: ["file_path"]  -- read source file contents',
        "restart": "params: []",
        "status": "params: []",
        "quit": "params: []",
        "/events": "GET endpoint (not an action) -- SSE stream of DAP events",
    }

    async def _help(params: list[Any]) -> RpcResponse:
        lines = [f"  {action}: {args}" for action, args in _ACTION_HELP.items()]
        return RpcResponse.ok("\n".join(lines))

    # Map action names to handlers
    _actions: dict[str, Any] = {
        "help": _help,
        "set_breakpoint": _set_breakpoint,
        "remove_breakpoint": _remove_breakpoint,
        "list_breakpoints": _list_breakpoints,
        "next": _next,
        "step_in": _step_in,
        "step_out": _step_out,
        "continue": _continue,
        "pause": _pause,
        "inspect": _inspect,
        "evaluate": _evaluate,
        "stack_up": _stack_up,
        "stack_down": _stack_down,
        "get_stack_trace": _get_stack_trace,
        "get_output": _get_output,
        "get_source": _get_source,
        "restart": _restart,
        "status": _status,
        "quit": _quit,
    }

    # ------------------------------------------------------------------
    # Endpoint
    # ------------------------------------------------------------------

    @app.post("/rpc", response_model=RpcResponse)
    async def rpc_endpoint(request: RpcRequest) -> RpcResponse:
        action_fn = _actions.get(request.action)
        if action_fn is None:
            return RpcResponse.error(f"Unknown action: {request.action}")
        try:
            async with _ctrl()._lock:
                return await action_fn(request.params)
        except Exception as e:
            log.exception("RPC action '%s' failed", request.action)
            return RpcResponse.error(str(e))

    @app.get("/events")
    async def events_endpoint() -> StreamingResponse:
        """SSE endpoint streaming DAP events in real-time."""
        q = handler.subscribe_sse()

        async def event_stream():
            try:
                while True:
                    msg = await q.get()
                    data = json.dumps(msg, default=str)
                    yield f"event: {msg['event']}\ndata: {data}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                handler.unsubscribe_sse(q)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
