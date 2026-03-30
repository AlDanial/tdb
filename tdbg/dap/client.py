"""DAP client: spawns debugpy adapter and communicates via DAP over stdio."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Callable

from .messages import Event, Request, Response, parse_message
from .protocol import encode_message, read_message
from .types import (
    Breakpoint,
    Capabilities,
    CompletionItem,
    Scope,
    SourceBreakpoint,
    StackFrame,
    Thread,
    Variable,
)

log = logging.getLogger(__name__)

EventHandler = Callable[[Event], Any]


class DAPClient:
    """Async DAP client that spawns and communicates with a debugpy adapter."""

    def __init__(self) -> None:
        self._seq = 0
        self._pending: dict[int, asyncio.Future[Response]] = {}
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self.capabilities = Capabilities()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def on_event(self, event_name: str, handler: EventHandler) -> None:
        self._event_handlers.setdefault(event_name, []).append(handler)

    async def start(self, python: str | None = None) -> None:
        """Spawn the debugpy adapter subprocess."""
        python = python or sys.executable
        self._process = await asyncio.create_subprocess_exec(
            python, "-m", "debugpy.adapter",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # isolate from terminal so it can't interfere with TUI
        )
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        """Shut down the adapter."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._process:
            self._process.terminate()
            await self._process.wait()

    async def _read_loop(self) -> None:
        """Continuously read DAP messages from adapter stdout."""
        assert self._process and self._process.stdout
        reader = self._process.stdout
        try:
            while True:
                data = await read_message(reader)
                msg = parse_message(data)
                if isinstance(msg, Response):
                    future = self._pending.pop(msg.request_seq, None)
                    if future and not future.done():
                        future.set_result(msg)
                elif isinstance(msg, Event):
                    log.debug("Event: %s %s", msg.event, msg.body)
                    for handler in self._event_handlers.get(msg.event, []):
                        handler(msg)
                    for handler in self._event_handlers.get("*", []):
                        handler(msg)
        except (ConnectionError, asyncio.IncompleteReadError):
            log.debug("DAP stream closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error in DAP read loop")

    async def _send_raw(self, command: str, arguments: dict[str, Any] | None = None) -> asyncio.Future[Response]:
        """Send a DAP request. Returns the Future for the response (not awaited)."""
        assert self._process and self._process.stdin
        seq = self._next_seq()
        request = Request(seq=seq, command=command, arguments=arguments or {})
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        data = encode_message(request.to_dict())
        self._process.stdin.write(data)
        await self._process.stdin.drain()
        return future

    async def _send(self, command: str, arguments: dict[str, Any] | None = None) -> Response:
        """Send a DAP request and wait for its response."""
        future = await self._send_raw(command, arguments)
        response = await asyncio.wait_for(future, timeout=30.0)
        if not response.success:
            raise DAPError(command, response.message or "Unknown error", response.body)
        return response

    # --- High-level DAP commands ---

    async def initialize(self) -> Capabilities:
        resp = await self._send("initialize", {
            "clientID": "tdbg",
            "clientName": "tdbg",
            "adapterID": "debugpy",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsRunInTerminalRequest": False,
        })
        self.capabilities = Capabilities.from_dict(resp.body)
        return self.capabilities

    async def launch(
        self,
        program: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stop_on_entry: bool = False,
        just_my_code: bool = True,
        python: str | None = None,
    ) -> asyncio.Future[Response]:
        """Send launch request. Returns a Future for the response.

        debugpy delays the launch response until after configurationDone,
        so callers must NOT await this directly — await the returned Future
        after sending configurationDone.
        """
        arguments: dict[str, Any] = {
            "type": "debugpy",
            "request": "launch",
            "program": program,
            "args": args or [],
            "cwd": cwd or ".",
            "console": "internalConsole",
            "redirectOutput": True,
            "justMyCode": just_my_code,
            "stopOnEntry": stop_on_entry,
        }
        if env:
            arguments["env"] = env
        if python:
            arguments["debugLauncherPython"] = python
        return await self._send_raw("launch", arguments)

    async def configuration_done(self) -> None:
        await self._send("configurationDone")

    async def set_breakpoints(
        self,
        source_path: str,
        breakpoints: list[SourceBreakpoint],
    ) -> list[Breakpoint]:
        resp = await self._send("setBreakpoints", {
            "source": {"path": source_path},
            "breakpoints": [bp.to_dict() for bp in breakpoints],
        })
        return [Breakpoint.from_dict(bp) for bp in resp.body.get("breakpoints", [])]

    async def set_exception_breakpoints(self, filters: list[str]) -> None:
        await self._send("setExceptionBreakpoints", {"filters": filters})

    async def threads(self) -> list[Thread]:
        resp = await self._send("threads")
        return [Thread.from_dict(t) for t in resp.body.get("threads", [])]

    async def stack_trace(
        self,
        thread_id: int,
        start_frame: int = 0,
        levels: int = 20,
    ) -> list[StackFrame]:
        resp = await self._send("stackTrace", {
            "threadId": thread_id,
            "startFrame": start_frame,
            "levels": levels,
        })
        return [StackFrame.from_dict(f) for f in resp.body.get("stackFrames", [])]

    async def scopes(self, frame_id: int) -> list[Scope]:
        resp = await self._send("scopes", {"frameId": frame_id})
        return [Scope.from_dict(s) for s in resp.body.get("scopes", [])]

    async def variables(
        self,
        variables_reference: int,
        start: int = 0,
        count: int = 0,
    ) -> list[Variable]:
        args: dict[str, Any] = {"variablesReference": variables_reference}
        if start:
            args["start"] = start
        if count:
            args["count"] = count
        resp = await self._send("variables", args)
        return [Variable.from_dict(v) for v in resp.body.get("variables", [])]

    async def evaluate(
        self,
        expression: str,
        frame_id: int | None = None,
        context: str = "repl",
    ) -> tuple[str, int]:
        """Evaluate expression. Returns (result_string, variables_reference)."""
        args: dict[str, Any] = {"expression": expression, "context": context}
        if frame_id is not None:
            args["frameId"] = frame_id
        resp = await self._send("evaluate", args)
        return resp.body.get("result", ""), resp.body.get("variablesReference", 0)

    async def completions(
        self,
        text: str,
        column: int,
        frame_id: int | None = None,
    ) -> list[CompletionItem]:
        args: dict[str, Any] = {"text": text, "column": column}
        if frame_id is not None:
            args["frameId"] = frame_id
        resp = await self._send("completions", args)
        return [CompletionItem.from_dict(c) for c in resp.body.get("targets", [])]

    async def continue_(self, thread_id: int) -> None:
        await self._send("continue", {"threadId": thread_id})

    async def next(self, thread_id: int) -> None:
        await self._send("next", {"threadId": thread_id})

    async def step_in(self, thread_id: int) -> None:
        await self._send("stepIn", {"threadId": thread_id})

    async def step_out(self, thread_id: int) -> None:
        await self._send("stepOut", {"threadId": thread_id})

    async def pause(self, thread_id: int) -> None:
        await self._send("pause", {"threadId": thread_id})

    async def disconnect(self, terminate: bool = True) -> None:
        try:
            await self._send("disconnect", {"terminateDebuggee": terminate})
        except (DAPError, asyncio.TimeoutError, ConnectionError):
            pass


class DAPError(Exception):
    def __init__(self, command: str, message: str, body: dict[str, Any] | None = None):
        self.command = command
        self.body = body or {}
        super().__init__(f"DAP {command} failed: {message}")
