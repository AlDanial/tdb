"""Debug Adapter Protocol facade for one tcsh debug session."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tdb.adapters.tcsh.inspect import InspectionError
from tdb.adapters.tcsh.protocol import (
    EndOfStream,
    ProtocolError,
    encode_message,
    read_message,
)
from tdb.adapters.tcsh.session import (
    DebugSession,
    EvaluationError,
    EventSink,
    InvalidStateError,
    LaunchConfig,
    LaunchError,
    SessionEvent,
)
from tdb.adapters.tcsh.transport import TransportError


class AsyncWriter(Protocol):
    """The small asyncio writer surface used by the protocol server."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...


SessionFactory = Callable[[LaunchConfig, EventSink], DebugSession]
Handler = Callable[[Mapping[str, object]], Awaitable[Mapping[str, object]]]


class RequestError(Exception):
    """A framed DAP request has invalid arguments or lifecycle ordering."""


@dataclass(frozen=True, slots=True)
class _HandlerResult:
    body: Mapping[str, object]
    events: tuple[SessionEvent, ...] = ()


_CAPABILITIES: dict[str, bool] = {
    "supportsConfigurationDoneRequest": True,
    "supportsTerminateRequest": True,
    "supportsEvaluateForHovers": True,
    "supportsSetVariable": False,
    "supportsConditionalBreakpoints": False,
    "supportsFunctionBreakpoints": False,
    "supportsHitConditionalBreakpoints": False,
    "supportsLogPoints": False,
    "supportsRestartRequest": False,
    "supportsStepBack": False,
    "supportsCompletionsRequest": False,
    "supportsModulesRequest": False,
    "supportsExceptionInfoRequest": False,
    "supportsReadMemoryRequest": False,
    "supportsDisassembleRequest": False,
    "supportsCancelRequest": False,
    "supportsBreakpointLocationsRequest": False,
    "supportsLoadedSourcesRequest": False,
    "supportsTerminateThreadsRequest": False,
}

_SESSION_EVENT_KINDS = frozenset(
    {"initialized", "output", "stopped", "continued", "exited", "terminated"}
)
_DOMAIN_ERRORS = (
    RequestError,
    LaunchError,
    InvalidStateError,
    EvaluationError,
    InspectionError,
    TransportError,
)


class DAPServer:
    """Read framed requests, dispatch them explicitly, and serialize all output."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: AsyncWriter,
        session_factory: SessionFactory = DebugSession,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._session_factory = session_factory
        self._session: DebugSession | None = None
        self._initialized = False
        self._configuration_done = False
        self._session_terminated = False
        self._session_detached = False
        self._termination_lock = asyncio.Lock()
        self._termination_task: asyncio.Task[None] | None = None
        self._termination_error: BaseException | None = None
        self._outbound_seq = 1
        self._write_lock = asyncio.Lock()
        self._deferred_events: list[SessionEvent] | None = None
        self._client_supports_run_in_terminal = False
        self._reverse_pending: dict[int, asyncio.Future[Mapping[str, object]]] = {}
        # A reverse-request response can arrive (and be routed by
        # _handle_request) before _send_reverse_request gets around to
        # registering its future in _reverse_pending -- see that method's
        # own comment. Responses that find no pending future are buffered
        # here, keyed by request_seq, so _send_reverse_request can pick
        # them up right after registering instead of hanging forever.
        self._unmatched_responses: dict[int, Mapping[str, object]] = {}
        # Strong ref for the same reason as the codebase's other
        # background-task attributes (asyncio only holds a weak reference
        # to a bare ensure_future() task): an externalTerminal launch's
        # session.start() must run off of configurationDone's own request
        # handling so this server's run() loop can keep reading and route
        # the runInTerminal reverse-request's eventual response (see
        # _configuration_done_request/_finish_terminal_start).
        self._start_task: asyncio.Future[None] | None = None
        self._dispatch: dict[str, Handler] = {
            "initialize": self._initialize,
            "launch": self._launch,
            "setBreakpoints": self._set_breakpoints,
            "configurationDone": self._configuration_done_request,
            "threads": self._threads,
            "stackTrace": self._stack_trace,
            "scopes": self._scopes,
            "variables": self._variables,
            "evaluate": self._evaluate,
            "continue": self._continue,
            "pause": self._pause,
            "next": self._next,
            "stepIn": self._step_in,
            "stepOut": self._step_out,
            "disconnect": self._disconnect,
            "terminate": self._terminate,
        }

    async def run(self) -> None:
        """Serve requests through EOF; framing corruption remains fatal."""

        try:
            while True:
                try:
                    request = await read_message(self._reader)
                except EndOfStream:
                    break
                await self._handle_request(request)
            if self._session_detached:
                await self._wait_for_detached_session()
            else:
                await self._terminate_active_session()
        except BaseException as error:
            try:
                await self._terminate_active_session()
            except BaseException as termination_error:  # noqa: BLE001
                error.add_note(f"session termination also failed: {termination_error}")
            raise

    async def stop(self) -> None:
        """Stop the owned session and wake a run loop blocked on adapter input."""

        if self._termination_task is asyncio.current_task():
            return
        self._session_detached = False
        await self._terminate_active_session()
        if not self._reader.at_eof():
            self._reader.feed_eof()

    async def _handle_request(self, request: Mapping[str, object]) -> None:
        request_seq = request.get("seq")
        command = request.get("command")
        if not _is_int(request_seq):
            raise ProtocolError("Request seq must be an integer")
        if request.get("type") == "response":
            raw_request_seq = request.get("request_seq")
            seq_value = raw_request_seq if _is_int(raw_request_seq) else -1
            future = self._reverse_pending.pop(seq_value, None)
            if future is not None:
                if not future.done():
                    future.set_result(request)
            else:
                self._unmatched_responses[seq_value] = request
            return
        if request.get("type") != "request":
            await self._send_response(
                request_seq,
                _command_name(command),
                RequestError("type must be 'request'"),
            )
            return
        if not isinstance(command, str) or not command:
            await self._send_response(
                request_seq, "", RequestError("command must be a non-empty string")
            )
            return
        raw_arguments = request.get("arguments", {})
        if not isinstance(raw_arguments, dict):
            await self._send_response(
                request_seq, command, RequestError("arguments must be an object")
            )
            return
        handler = self._dispatch.get(command)
        if handler is None:
            await self._send_response(
                request_seq,
                command,
                RequestError(f"Request {command!r} is not supported"),
            )
            return
        self._deferred_events = []
        try:
            result = await handler(raw_arguments)
        except _DOMAIN_ERRORS as error:
            await self._send_response(request_seq, command, error)
            await self._flush_deferred_events()
            return
        except BaseException:
            self._deferred_events = None
            raise
        await self._send_response(request_seq, command, None, result)
        await self._flush_deferred_events()
        if isinstance(result, _HandlerResult):
            for event in result.events:
                await self._emit_session_event(event)

    async def _send_response(
        self,
        request_seq: int,
        command: str,
        error: Exception | None,
        result: Mapping[str, object] | None = None,
    ) -> None:
        response: dict[str, object] = {
            "type": "response",
            "request_seq": request_seq,
            "success": error is None,
            "command": command,
        }
        if error is None:
            body = result.body if isinstance(result, _HandlerResult) else result
            response["body"] = dict(body or {})
        else:
            response["message"] = str(error) or type(error).__name__
        await self._send(response)

    async def _emit_session_event(self, event: SessionEvent) -> None:
        if event.kind not in _SESSION_EVENT_KINDS:
            raise RuntimeError(f"Unsupported session event: {event.kind}")
        if self._deferred_events is not None:
            self._deferred_events.append(event)
            return
        await self._send(
            {"type": "event", "event": event.kind, "body": dict(event.body)}
        )

    async def _flush_deferred_events(self) -> None:
        events = self._deferred_events or []
        self._deferred_events = None
        for event in events:
            await self._emit_session_event(event)

    async def _send(self, message: Mapping[str, object]) -> int:
        async with self._write_lock:
            framed = dict(message)
            framed["seq"] = self._outbound_seq
            self._outbound_seq += 1
            self._writer.write(encode_message(framed))
            await self._writer.drain()
            return framed["seq"]

    async def _send_reverse_request(
        self, command: str, arguments: Mapping[str, object]
    ) -> None:
        """Send a reverse (adapter -> client) request and await its reply.

        `_handle_request` routes the client's eventual "response" message
        back here by request_seq (see its `type == "response"` branch).
        The future is registered in `self._reverse_pending` only AFTER
        `_send` returns -- but `_send`'s own `await self._writer.drain()`
        can yield the event loop to run()'s read loop, which could read
        and route the client's reply before this coroutine gets back to
        registering the future. `_handle_request` buffers such an
        unmatched reply in `self._unmatched_responses` instead of
        dropping it; check that buffer immediately after registering, so
        a reply that won the race is still picked up rather than making
        this hang until the 30s timeout.
        """
        future: asyncio.Future[Mapping[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        seq = await self._send(
            {"type": "request", "command": command, "arguments": dict(arguments)}
        )
        self._reverse_pending[seq] = future
        buffered = self._unmatched_responses.pop(seq, None)
        if buffered is not None and not future.done():
            future.set_result(buffered)
        try:
            response = await asyncio.wait_for(future, 30.0)
        finally:
            self._reverse_pending.pop(seq, None)
        if not response.get("success"):
            raise LaunchError(str(response.get("message") or f"{command} was refused"))

    async def _initialize(self, arguments: Mapping[str, object]) -> _HandlerResult:
        if self._initialized:
            raise RequestError("initialize has already been requested")
        adapter_id = arguments.get("adapterID")
        if not isinstance(adapter_id, str) or not adapter_id:
            raise RequestError("adapterID must be a non-empty string")
        self._client_supports_run_in_terminal = bool(
            arguments.get("supportsRunInTerminalRequest")
        )
        self._initialized = True
        return _HandlerResult(dict(_CAPABILITIES), (SessionEvent("initialized", {}),))

    async def _launch(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        self._require_initialized()
        if self._session is not None:
            raise RequestError("launch has already been requested")
        config = self._launch_config(arguments)
        session = self._session_factory(config, self._emit_session_event)
        if config.external_terminal:
            # Setting the attribute post-construction (rather than adding
            # a run_in_terminal parameter to SessionFactory) keeps the
            # factory signature -- and every existing test factory -- as
            # is; DebugSession.__init__ already accepts the keyword too,
            # for callers that construct it directly.
            async def run_in_terminal(
                cmd: list[str], cwd: str, env: dict[str, str]
            ) -> None:
                await self._send_reverse_request(
                    "runInTerminal",
                    {
                        "kind": "external",
                        "title": "tdb tcsh debuggee",
                        "cwd": cwd,
                        "args": cmd,
                        "env": env,
                    },
                )

            session._run_in_terminal = run_in_terminal  # type: ignore[attr-defined]
        await session.prepare()
        self._session = session
        return {}

    async def _set_breakpoints(
        self, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        session = self._require_session()
        source = _require_mapping(arguments, "source")
        path_text = _require_string(source, "path", display="source.path")
        requested = _require_sequence(arguments, "breakpoints")
        lines: list[int] = []
        for index, item in enumerate(requested):
            if not isinstance(item, dict):
                raise RequestError(f"breakpoints[{index}] must be an object")
            lines.append(
                _require_int(
                    item, "line", display=f"breakpoints[{index}].line", positive=True
                )
            )
        bound = session.set_breakpoints(Path(path_text), tuple(lines))
        dap_breakpoints: list[dict[str, object]] = []
        dap_source = {"path": path_text}
        for item in bound:
            projected: dict[str, object] = {
                "verified": item.verified,
                "source": dap_source,
            }
            if item.line is not None:
                projected["line"] = item.line
            if item.message is not None:
                projected["message"] = item.message
            dap_breakpoints.append(projected)
        return {"breakpoints": dap_breakpoints}

    async def _configuration_done_request(
        self, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        del arguments
        session = self._require_session()
        if self._configuration_done:
            raise RequestError("configurationDone has already been requested")
        self._configuration_done = True
        if session.config.external_terminal:
            # session.start() awaits self._run_in_terminal(...), which
            # awaits _send_reverse_request(), whose reply can only be
            # read/routed by THIS coroutine's own run() loop -- but run()
            # is what's calling this handler, and it awaits handler(msg)
            # synchronously before reading the next message. Awaiting
            # start() inline here would deadlock: the reply could never
            # arrive because nothing is reading stdin anymore. Run the
            # rest of the launch as a background task (strong ref, per
            # the repo's task-GC pitfall) so this handler returns
            # immediately, letting run() go back to reading -- including
            # the eventual runInTerminal response, which _handle_request's
            # own `type == "response"` routing handles directly, without
            # going through a command handler.
            self._start_task = asyncio.ensure_future(
                self._finish_terminal_start(session)
            )
            return {}
        await session.start()
        return {}

    async def _finish_terminal_start(self, session: DebugSession) -> None:
        """Run a terminal-mode session.start() off of configurationDone's
        own request handling (see _configuration_done_request).

        By the time this runs, configurationDone has already answered
        with a successful `{}` response -- there is no pending request
        left to attach a failure to. On failure, the only way anything
        user-visible reaches the client is via events: an `output`
        (stderr) event describing what went wrong, followed by
        `terminated` so the client stops treating the session as live.
        session.start()'s own failure path already tears the session down
        (cleanup, workspace removal, state -> TERMINATED) but -- unlike
        the fd-mode path, where a raised LaunchError becomes an error
        response to configurationDone itself -- it does not emit any DAP
        event on its own, so that step still has to happen here.
        """
        try:
            await session.start()
        except asyncio.CancelledError:
            # _cancel_start_task (disconnect/terminate tearing down an
            # in-flight launch) cancelling this, not a launch failure --
            # session.start() already did its own cleanup for this case.
            raise
        except BaseException as error:  # noqa: BLE001
            # Deliberately broad: session.start() can raise LaunchError
            # (the expected failure) but also e.g. a bare
            # asyncio.TimeoutError from _send_reverse_request's own
            # wait_for, or an OSError setting up the guardian FIFOs. This
            # background task is not awaited inline by run() -- there is
            # no other except-Exception net standing behind this one --
            # so anything narrower here would silently strand the
            # exception on self._start_task with nothing user-visible
            # ever reaching the client (this exact class of bug happened
            # once already, with a bare TimeoutError going unreported).
            message = str(error) or type(error).__name__
            try:
                await self._emit_session_event(
                    SessionEvent(
                        "output",
                        # "console" (not "stderr"): the controller drops
                        # stdout/stderr output events in --terminal mode
                        # (program output goes to the external terminal
                        # instead), which would silently swallow this
                        # failure message. "console" is DAP-standard and
                        # always rendered in the Console View.
                        {"category": "console", "output": f"{message}\n"},
                    )
                )
                await self._emit_session_event(SessionEvent("terminated", {}))
            except BaseException:  # noqa: BLE001
                pass

    async def _threads(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        session = self._require_session()
        return {
            "threads": [
                {"id": item.id, "name": item.name} for item in session.threads()
            ]
        }

    async def _stack_trace(
        self, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        session = self._require_session()
        _require_thread(arguments)
        frames = session.stack_trace()
        return {
            "stackFrames": [
                {
                    "id": frame.id,
                    "name": frame.name,
                    "source": {"name": frame.path.name, "path": str(frame.path)},
                    "line": frame.line,
                    "column": 1,
                    "endLine": frame.end_line,
                    "endColumn": 1,
                }
                for frame in frames
            ],
            "totalFrames": len(frames),
        }

    async def _scopes(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        frame_id = _require_int(arguments, "frameId")
        return {
            "scopes": [
                {
                    "name": scope.name,
                    "variablesReference": scope.variables_reference,
                    "expensive": scope.expensive,
                }
                for scope in session.scopes(frame_id)
            ]
        }

    async def _variables(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        reference = _require_int(arguments, "variablesReference")
        variables = await session.variables(reference)
        return {
            "variables": [
                {
                    "name": variable.name,
                    "value": variable.value,
                    "variablesReference": variable.variables_reference,
                }
                for variable in variables
            ]
        }

    async def _evaluate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        expression = _require_string(arguments, "expression")
        frame_id_value = arguments.get("frameId")
        frame_id = None
        if frame_id_value is not None:
            if not _is_int(frame_id_value):
                raise RequestError("frameId must be an integer")
            frame_id = frame_id_value
        context = arguments.get("context")
        if context is not None and not isinstance(context, str):
            raise RequestError("context must be a string")
        result = await session.evaluate(expression, frame_id)
        return {
            "result": result.result,
            "variablesReference": result.variables_reference,
        }

    async def _continue(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        _require_thread(arguments)
        await session.continue_()
        return {"allThreadsContinued": True}

    async def _pause(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        session.request_pause()
        return {}

    async def _next(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        _require_thread(arguments)
        await session.next()
        return {}

    async def _step_in(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        _require_thread(arguments)
        await session.step_in()
        return {}

    async def _step_out(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        session = self._require_session()
        _require_thread(arguments)
        await session.step_out()
        return {}

    async def _disconnect(
        self, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        session = self._require_session()
        terminate = arguments.get("terminateDebuggee", True)
        if not isinstance(terminate, bool):
            raise RequestError("terminateDebuggee must be a boolean")
        # An in-flight terminal-mode start (see _configuration_done_request)
        # runs concurrently with request handling -- unlike fd-mode, where
        # session.start() is awaited inline by configurationDone, so run()
        # can never dispatch a later request until it's done. Cancel and
        # await it first so it can't still be mutating session state (or,
        # for detach, assigning `session._run_in_terminal`'s in-flight
        # effects) underneath whatever this request does next.
        await self._cancel_start_task()
        if terminate:
            await self._terminate_active_session()
        else:
            await session.detach()
            self._session_detached = True
        return {}

    async def _terminate(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        del arguments
        self._require_session()
        await self._cancel_start_task()
        await self._terminate_active_session()
        return {}

    async def _cancel_start_task(self) -> None:
        """Cancel and await an in-flight terminal-mode `_finish_terminal_start`
        background task before disconnect/terminate/EOF tear the session
        down.

        Without this, an in-flight session.start() could still be running
        concurrently with (and racing) whatever teardown happens next --
        e.g. setting session.state out from under start()'s own handshake,
        or a second, overlapping guardian-termination attempt. Cancelling
        it here drives start()'s own CancelledError branch (which does
        its own full cleanup and re-raises), so by the time this returns
        the session is in a stable, inert state for whatever runs next.
        """
        task, self._start_task = self._start_task, None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    def _launch_config(self, arguments: Mapping[str, object]) -> LaunchConfig:
        program_text = _require_string(arguments, "program")
        program = Path(program_text)
        raw_args = arguments.get("args", [])
        if not isinstance(raw_args, list) or any(
            not isinstance(item, str) for item in raw_args
        ):
            raise RequestError("args must be an array of strings")
        cwd_value = arguments.get("cwd")
        if cwd_value is None:
            cwd = program.parent
        elif isinstance(cwd_value, str) and cwd_value:
            cwd = Path(cwd_value)
        else:
            raise RequestError("cwd must be a non-empty string")
        raw_env = arguments.get("env", {})
        if not isinstance(raw_env, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_env.items()
        ):
            raise RequestError("env must be an object with string values")
        tcsh_value = arguments.get("tcshPath")
        if tcsh_value is None:
            discovered = shutil.which("tcsh")
            if discovered is None:
                raise LaunchError("Could not find tcsh on PATH")
            tcsh_path = Path(discovered)
        elif isinstance(tcsh_value, str) and tcsh_value:
            tcsh_path = Path(tcsh_value)
        else:
            raise RequestError("tcshPath must be a non-empty string")
        stop_on_entry = arguments.get("stopOnEntry", True)
        if not isinstance(stop_on_entry, bool):
            raise RequestError("stopOnEntry must be a boolean")
        console_value = arguments.get("console")
        if console_value is not None and not isinstance(console_value, str):
            raise RequestError("console must be a string")
        external_terminal = console_value == "externalTerminal"
        if external_terminal and not self._client_supports_run_in_terminal:
            raise RequestError(
                "externalTerminal launch requires a client that supports "
                "the runInTerminal reverse request"
            )
        return LaunchConfig(
            program,
            tuple(raw_args),
            cwd,
            dict(raw_env),
            tcsh_path,
            stop_on_entry,
            external_terminal,
        )

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RequestError("initialize must be requested before launch")

    def _require_session(self) -> DebugSession:
        if self._session is None:
            raise RequestError("launch must be requested first")
        return self._session

    async def _terminate_active_session(self) -> None:
        # Covers callers that don't already cancel it themselves (run()'s
        # own EOF/exception-path teardown, stop()) -- _disconnect/_terminate
        # call this too but cancel _start_task before they get here; the
        # extra call is a no-op once it's already gone. See
        # _cancel_start_task's own docstring for why this must happen
        # before session.terminate() can safely run.
        await self._cancel_start_task()
        session = self._session
        if session is None:
            return
        self._session_detached = False
        async with self._termination_lock:
            if self._session_terminated:
                return
            task = self._termination_task
            if task is asyncio.current_task():
                return
            if task is not None and task.done():
                self._record_termination_outcome(task)
                if self._termination_error is None:
                    return
                self._termination_task = None
                self._termination_error = None
                task = None
            if task is None:
                task = asyncio.create_task(session.terminate())
                self._termination_task = task
                self._termination_error = None
                task.add_done_callback(self._record_termination_outcome)

        waiter = asyncio.shield(task)
        waiter.add_done_callback(_consume_future_exception)
        await asyncio.wait((waiter,))
        await waiter

        async with self._termination_lock:
            if self._termination_task is task:
                self._record_termination_outcome(task)

    def _record_termination_outcome(self, task: asyncio.Task[None]) -> None:
        try:
            error = task.exception()
        except asyncio.CancelledError as cancelled:
            error = cancelled
        if self._termination_task is not task:
            return
        self._termination_error = error
        if error is None:
            self._session_terminated = True

    async def _wait_for_detached_session(self) -> None:
        session = self._session
        if session is None or self._session_terminated:
            return
        await session.wait()
        self._session_terminated = True


def _command_name(value: object) -> str:
    return value if isinstance(value, str) else ""


def _consume_future_exception(future: asyncio.Future[object]) -> None:
    if not future.cancelled():
        future.exception()


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(arguments: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = arguments.get(key)
    if not isinstance(value, dict):
        raise RequestError(f"{key} must be an object")
    return value


def _require_sequence(arguments: Mapping[str, object], key: str) -> Sequence[object]:
    value = arguments.get(key)
    if not isinstance(value, list):
        raise RequestError(f"{key} must be an array")
    return value


def _require_string(
    arguments: Mapping[str, object],
    key: str,
    *,
    display: str | None = None,
) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise RequestError(f"{display or key} must be a non-empty string")
    return value


def _require_int(
    arguments: Mapping[str, object],
    key: str,
    *,
    display: str | None = None,
    positive: bool = False,
) -> int:
    value = arguments.get(key)
    if not _is_int(value) or (positive and value <= 0):
        qualifier = "a positive integer" if positive else "an integer"
        raise RequestError(f"{display or key} must be {qualifier}")
    return value


def _require_thread(arguments: Mapping[str, object]) -> None:
    thread_id = _require_int(arguments, "threadId")
    if thread_id != 1:
        raise RequestError("threadId must be 1")
