"""DAP client: spawns debugpy adapter and communicates via DAP over stdio."""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Awaitable, Callable

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
ReverseRequestHandler = Callable[[Request], Awaitable[dict[str, Any]]]


class DAPClient:
    """Async DAP client that communicates with a debugpy adapter.

    Supports two connection modes:
    - ``start()`` — spawn adapter subprocess, communicate over stdio pipes
    - ``connect()`` — connect to an adapter over TCP (for child processes)
    """

    def __init__(self) -> None:
        self._seq = 0
        self._pending: dict[int, asyncio.Future[Response]] = {}
        self._event_handlers: dict[str, list[EventHandler]] = {}
        self._reverse_request_handlers: dict[str, ReverseRequestHandler] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # Strong ref so the adapter-death watcher isn't GC'd mid-await.
        # asyncio only keeps weak refs to running tasks; without this
        # field the task could be collected before it logs anything,
        # leaving a silent hang on premature adapter exit.
        self._watch_task: asyncio.Task[None] | None = None
        self.capabilities = Capabilities()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def on_event(self, event_name: str, handler: EventHandler) -> None:
        self._event_handlers.setdefault(event_name, []).append(handler)

    def on_reverse_request(self, command: str, handler: ReverseRequestHandler) -> None:
        """Register a handler for a reverse request from the adapter."""
        self._reverse_request_handlers[command] = handler

    async def start(self) -> None:
        """Spawn the debugpy adapter subprocess.

        Always uses ``sys.executable`` (tdb's own Python, which has debugpy
        installed). The user's ``--python`` is for the *debuggee*, not the
        adapter — it's threaded through as ``debugLauncherPython`` in the
        launch request instead. Running the adapter on a Python without
        debugpy would make this subprocess die immediately with
        ``ModuleNotFoundError`` and tdb would hang waiting on stdout.
        """
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-Xfrozen_modules=off",
            "-m",
            "debugpy.adapter",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # isolate from terminal so it can't interfere with TUI
        )
        self._reader = self._process.stdout
        self._reader_task = asyncio.create_task(self._read_loop())
        # Watch for premature death — if the adapter exits before tdb
        # is done with it, surface stderr so the user sees an
        # actionable error instead of a silent hang. Strong ref via
        # `self._watch_task` matches the `_bg_tasks` pattern in
        # `DebugController` (asyncio holds only weak refs to tasks).
        self._watch_task = asyncio.create_task(self._watch_adapter_death())

    async def _watch_adapter_death(self) -> None:
        if self._process is None:
            return
        rc = await self._process.wait()
        stderr_bytes = b""
        if self._process.stderr is not None:
            try:
                stderr_bytes = await self._process.stderr.read()
            except Exception:
                pass
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        log.error(
            "debugpy adapter exited with code %s%s",
            rc,
            f"\nstderr:\n{stderr_text}" if stderr_text else "",
        )
        # Fail any still-pending requests so callers don't await forever.
        err = ConnectionError(
            f"debugpy adapter died (exit {rc}): "
            f"{stderr_text.splitlines()[-1] if stderr_text else 'no output'}"
        )
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def connect(self, host: str, port: int) -> None:
        """Connect to an already-running debugpy adapter over TCP."""
        self._reader, self._writer = await asyncio.open_connection(host, port)
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        """Shut down the connection."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None
        if self._writer:
            self._writer.close()
            self._writer = None
        if self._process:
            self._process.terminate()
            await self._process.wait()

    async def _read_loop(self) -> None:
        """Continuously read DAP messages from the adapter."""
        assert self._reader is not None
        reader = self._reader
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
                elif isinstance(msg, Request):
                    log.debug("Reverse request: %s %s", msg.command, msg.arguments)
                    await self._handle_reverse_request(msg)
        except (ConnectionError, asyncio.IncompleteReadError):
            log.debug("DAP stream closed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error in DAP read loop")

    async def _handle_reverse_request(self, request: Request) -> None:
        """Dispatch a reverse request to its registered handler and respond."""
        handler = self._reverse_request_handlers.get(request.command)
        if handler is None:
            log.warning("No handler for reverse request: %s", request.command)
            await self._send_reverse_response(
                request, success=False, message="Not supported"
            )
            return
        try:
            body = await handler(request)
            await self._send_reverse_response(request, success=True, body=body)
        except Exception as e:
            log.exception("Error handling reverse request: %s", request.command)
            await self._send_reverse_response(request, success=False, message=str(e))

    def _get_write_stream(self) -> asyncio.StreamWriter:
        """Return the stream used for writing DAP messages."""
        if self._writer:
            return self._writer
        if self._process and self._process.stdin:
            return self._process.stdin
        # Shutdown race: caller invoked disconnect/_send after the writer
        # was closed (or before connect). Surface as ConnectionError so the
        # normal disconnect error-handling path swallows it.
        raise ConnectionError("DAPClient has no active write stream")

    async def _send_reverse_response(
        self,
        request: Request,
        success: bool,
        body: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        """Send a response back to the adapter for a reverse request."""
        writer = self._get_write_stream()
        seq = self._next_seq()
        response_data: dict[str, Any] = {
            "seq": seq,
            "type": "response",
            "request_seq": request.seq,
            "command": request.command,
            "success": success,
        }
        if body:
            response_data["body"] = body
        if message:
            response_data["message"] = message
        writer.write(encode_message(response_data))
        await writer.drain()

    async def _send_raw(
        self, command: str, arguments: dict[str, Any] | None = None
    ) -> asyncio.Future[Response]:
        """Send a DAP request. Returns the Future for the response (not awaited)."""
        writer = self._get_write_stream()
        seq = self._next_seq()
        request = Request(seq=seq, command=command, arguments=arguments or {})
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        data = encode_message(request.to_dict())
        writer.write(data)
        await writer.drain()
        return future

    async def _send(
        self, command: str, arguments: dict[str, Any] | None = None
    ) -> Response:
        """Send a DAP request and wait for its response."""
        from tdb._timeouts import DAP_REQUEST

        future = await self._send_raw(command, arguments)
        response = await asyncio.wait_for(future, timeout=DAP_REQUEST)
        if not response.success:
            raise DAPError(command, response.message or "Unknown error", response.body)
        return response

    # --- High-level DAP commands ---

    async def initialize(self, support_run_in_terminal: bool = False) -> Capabilities:
        resp = await self._send(
            "initialize",
            {
                "clientID": "tdb",
                "clientName": "tdb",
                "adapterID": "debugpy",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsRunInTerminalRequest": support_run_in_terminal,
            },
        )
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
        console: str = "internalConsole",
        sub_process: bool = True,
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
            "console": console,
            "redirectOutput": console == "internalConsole",
            "justMyCode": just_my_code,
            "stopOnEntry": stop_on_entry,
            "subProcess": sub_process,
            "pythonArgs": ["-Xfrozen_modules=off"],
        }
        if env:
            arguments["env"] = env
        if python:
            # "python" sets the debuggee interpreter; the adapter defaults
            # "debugLauncherPython" from this (they're different components
            # but here we want them to match).
            arguments["python"] = python
        return await self._send_raw("launch", arguments)

    async def attach(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        sub_process_id: int | None = None,
        just_my_code: bool = True,
    ) -> asyncio.Future[Response]:
        """Send attach request. Returns a Future (same pattern as launch).

        Use subProcessId (not processId) to route to a child session
        without triggering ptrace injection.
        """
        arguments: dict[str, Any] = {
            "type": "debugpy",
            "request": "attach",
            "connect": {"host": host, "port": port},
            "justMyCode": just_my_code,
            "subProcess": True,
        }
        if sub_process_id is not None:
            arguments["subProcessId"] = sub_process_id
        return await self._send_raw("attach", arguments)

    async def configuration_done(self) -> None:
        await self._send("configurationDone")

    async def set_breakpoints(
        self,
        source_path: str,
        breakpoints: list[SourceBreakpoint],
    ) -> list[Breakpoint]:
        resp = await self._send(
            "setBreakpoints",
            {
                "source": {"path": source_path},
                "breakpoints": [bp.to_dict() for bp in breakpoints],
            },
        )
        return [Breakpoint.from_dict(bp) for bp in resp.body.get("breakpoints", [])]

    async def breakpoint_locations(
        self,
        source_path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> set[int]:
        """Query valid breakpoint lines for a source file.

        Returns a set of 1-based line numbers where breakpoints can be placed.
        """
        args: dict[str, Any] = {
            "source": {"path": source_path},
            "line": start_line,
        }
        if end_line is not None:
            args["endLine"] = end_line
        resp = await self._send("breakpointLocations", args)
        return {bp["line"] for bp in resp.body.get("breakpoints", [])}

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
        resp = await self._send(
            "stackTrace",
            {
                "threadId": thread_id,
                "startFrame": start_frame,
                "levels": levels,
            },
        )
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

    async def continue_nowait(self, thread_id: int) -> None:
        """Send continue without awaiting the response.

        debugpy does not respond to `continue` requests against a target
        that isn't currently stopped, so awaiting can block for the full
        `_send` timeout. This variant just writes the request and moves on;
        the orphaned response (if any) is dropped in the read loop.
        """
        await self._send_raw("continue", {"threadId": thread_id})

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
