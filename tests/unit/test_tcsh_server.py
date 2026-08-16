from __future__ import annotations

import asyncio
import gc
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

import pytest

from tdb.adapters.tcsh import cli
from tdb.adapters.tcsh.breakpoints import BoundBreakpoint
from tdb.adapters.tcsh.inspect import Scope, Variable
from tdb.adapters.tcsh.models import StackFrame, ThreadInfo
from tdb.adapters.tcsh.protocol import ProtocolError, encode_message, read_message
from tdb.adapters.tcsh.server import DAPServer
from tdb.adapters.tcsh.session import (
    EvaluationResult,
    LaunchConfig,
    SessionEvent,
)
from tdb.adapters.tcsh.transport import TransportError
from tests.integration.tcsh_dap_client import DAPClient

EventSink = Callable[[SessionEvent], Awaitable[None]]


class FakeSession:
    def __init__(self, config: LaunchConfig, event_sink: EventSink) -> None:
        self.config = config
        self.event_sink = event_sink
        self.calls: list[object] = []
        self.terminated = 0
        self.detached = 0
        self.configured_detach = False
        self.continue_error: Exception | None = None
        self.wait_started = asyncio.Event()
        self.finish_wait = asyncio.Event()
        self.block_terminate = False
        self.terminate_started = asyncio.Event()
        self.finish_terminate = asyncio.Event()
        self.terminate_error: Exception | None = None
        self.terminate_event: SessionEvent | None = None
        self.start_error: Exception | None = None

    async def prepare(self) -> None:
        self.calls.append("prepare")

    async def start(self) -> None:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error

    def set_breakpoints(
        self, path: Path, lines: tuple[int, ...]
    ) -> tuple[BoundBreakpoint, ...]:
        self.calls.append(("set_breakpoints", path, lines))
        return (
            BoundBreakpoint(lines[0], True, lines[0] + 1, 7, None),
            BoundBreakpoint(lines[1], False, None, None, "No safe statement"),
        )

    def threads(self) -> tuple[ThreadInfo, ...]:
        self.calls.append("threads")
        return (ThreadInfo(1, "tcsh"),)

    def stack_trace(self) -> tuple[StackFrame, ...]:
        self.calls.append("stack_trace")
        return (StackFrame(4, "demo.csh", Path("/work/demo.csh"), 8, 10),)

    def scopes(self, frame_id: int) -> tuple[Scope, ...]:
        self.calls.append(("scopes", frame_id))
        return (Scope("Shell Variables", 11),)

    async def variables(self, reference: int) -> tuple[Variable, ...]:
        self.calls.append(("variables", reference))
        return (Variable("answer", "42"),)

    async def evaluate(self, expression: str, frame_id: int | None) -> EvaluationResult:
        self.calls.append(("evaluate", expression, frame_id))
        return EvaluationResult("value\n", 0)

    async def continue_(self) -> None:
        self.calls.append("continue")
        if self.continue_error is not None:
            raise self.continue_error

    async def next(self) -> None:
        self.calls.append("next")

    async def step_in(self) -> None:
        self.calls.append("step_in")

    async def step_out(self) -> None:
        self.calls.append("step_out")

    async def terminate(self) -> None:
        self.calls.append("terminate")
        self.terminated += 1
        self.terminate_started.set()
        if self.terminate_event is not None:
            await self.event_sink(self.terminate_event)
        if self.block_terminate:
            await self.finish_terminate.wait()
        if self.terminate_error is not None:
            raise self.terminate_error

    async def detach(self) -> None:
        self.calls.append("detach")
        self.detached += 1
        if self.configured_detach:
            await self.event_sink(SessionEvent("terminated", {}))
            self.finish_wait.set()

    async def wait(self) -> None:
        self.calls.append("wait")
        self.wait_started.set()
        await self.finish_wait.wait()


class FakeFactory:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def __call__(self, config: LaunchConfig, event_sink: EventSink) -> FakeSession:
        session = FakeSession(config, event_sink)
        self.sessions.append(session)
        return session


class PipeWriter:
    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.in_drain = False
        self.concurrent_write = False

    def write(self, data: bytes) -> None:
        if self.in_drain:
            self.concurrent_write = True
        self.reader.feed_data(data)

    async def drain(self) -> None:
        self.in_drain = True
        await asyncio.sleep(0)
        self.in_drain = False


class InMemoryClient:
    def __init__(
        self,
        request_reader: asyncio.StreamReader,
        response_reader: asyncio.StreamReader,
    ) -> None:
        self.request_reader = request_reader
        self.response_reader = response_reader
        self.next_seq = 1
        self.messages: list[dict[str, object]] = []

    async def request(
        self,
        command: str,
        arguments: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        sequence = self.next_seq
        self.next_seq += 1
        request: dict[str, object] = {
            "seq": sequence,
            "type": "request",
            "command": command,
        }
        if arguments is not None:
            request["arguments"] = dict(arguments)
        self.request_reader.feed_data(encode_message(request))
        while True:
            message = await asyncio.wait_for(
                read_message(self.response_reader), timeout=1
            )
            self.messages.append(message)
            if (
                message.get("type") == "response"
                and message.get("request_seq") == sequence
            ):
                return message

    async def next_message(self) -> dict[str, object]:
        message = await asyncio.wait_for(read_message(self.response_reader), timeout=1)
        self.messages.append(message)
        return message


@pytest.fixture
async def server_client() -> object:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    writer = PipeWriter(response_reader)
    factory = FakeFactory()
    server = DAPServer(request_reader, writer, factory)
    task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    client.writer = writer  # type: ignore[attr-defined]
    client.server = server  # type: ignore[attr-defined]
    try:
        yield client
    finally:
        request_reader.feed_eof()
        await asyncio.wait_for(task, timeout=1)


async def initialize_and_launch(client: InMemoryClient) -> FakeSession:
    assert (await client.request("initialize", {"adapterID": "tcsh"}))[
        "success"
    ] is True
    initialized = await client.next_message()
    assert initialized["event"] == "initialized"
    assert (
        await client.request(
            "launch",
            {
                "program": "/work/demo.csh",
                "args": ["one"],
                "cwd": "/work",
                "env": {"MODE": "test"},
                "tcshPath": "/mock/tcsh",
                "stopOnEntry": False,
            },
        )
    )["success"] is True
    factory = client.factory  # type: ignore[attr-defined]
    return factory.sessions[0]


@pytest.mark.asyncio
async def test_initialize_advertises_only_supported_features(
    server_client: object,
) -> None:
    client = server_client
    response = await client.request("initialize", {"adapterID": "tcsh"})  # type: ignore[attr-defined]
    assert response["success"] is True
    body = response["body"]
    assert body == {
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
    event = await client.next_message()  # type: ignore[attr-defined]
    assert event["event"] == "initialized"
    assert response["seq"] < event["seq"]


@pytest.mark.asyncio
async def test_launch_applies_schema_defaults(
    server_client: object,
    tcsh_path: Path,
) -> None:
    client = server_client
    await client.request("initialize", {"adapterID": "tcsh"})  # type: ignore[attr-defined]
    await client.next_message()  # type: ignore[attr-defined]
    response = await client.request("launch", {"program": "/tmp/tool.csh"})  # type: ignore[attr-defined]
    assert response["success"] is True
    session = client.factory.sessions[0]  # type: ignore[attr-defined]
    assert session.config == LaunchConfig(
        program=Path("/tmp/tool.csh"),
        args=(),
        cwd=Path("/tmp"),
        env={},
        tcsh_path=tcsh_path,
        stop_on_entry=True,
    )
    assert session.calls == ["prepare"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({}, "program"),
        ({"program": 4}, "program"),
        ({"program": "x", "args": "bad"}, "args"),
        ({"program": "x", "args": [1]}, "args"),
        ({"program": "x", "cwd": 3}, "cwd"),
        ({"program": "x", "env": []}, "env"),
        ({"program": "x", "env": {"X": 3}}, "env"),
        ({"program": "x", "tcshPath": False}, "tcshPath"),
        ({"program": "x", "stopOnEntry": 1}, "stopOnEntry"),
    ],
)
async def test_launch_rejects_malformed_arguments(
    server_client: object,
    arguments: dict[str, object],
    fragment: str,
) -> None:
    client = server_client
    await client.request("initialize", {"adapterID": "tcsh"})  # type: ignore[attr-defined]
    await client.next_message()  # type: ignore[attr-defined]
    response = await client.request("launch", arguments)  # type: ignore[attr-defined]
    assert response["success"] is False
    assert fragment in response["message"]
    assert client.factory.sessions == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_supported_requests_dispatch_and_project_dap_bodies(
    server_client: object,
) -> None:
    client = server_client
    session = await initialize_and_launch(client)  # type: ignore[arg-type]
    source = {"path": "/work/demo.csh"}
    breakpoints = await client.request(  # type: ignore[attr-defined]
        "setBreakpoints",
        {"source": source, "breakpoints": [{"line": 7}, {"line": 100}]},
    )
    assert breakpoints["body"] == {
        "breakpoints": [
            {"verified": True, "line": 8, "source": source},
            {"verified": False, "message": "No safe statement", "source": source},
        ]
    }
    assert (await client.request("configurationDone", {}))["body"] == {}  # type: ignore[attr-defined]
    assert (await client.request("threads", {}))["body"] == {  # type: ignore[attr-defined]
        "threads": [{"id": 1, "name": "tcsh"}]
    }
    assert (await client.request("stackTrace", {"threadId": 1}))["body"] == {  # type: ignore[attr-defined]
        "stackFrames": [
            {
                "id": 4,
                "name": "demo.csh",
                "source": {"name": "demo.csh", "path": "/work/demo.csh"},
                "line": 8,
                "column": 1,
                "endLine": 10,
                "endColumn": 1,
            }
        ],
        "totalFrames": 1,
    }
    assert (await client.request("scopes", {"frameId": 4}))["body"] == {  # type: ignore[attr-defined]
        "scopes": [
            {"name": "Shell Variables", "variablesReference": 11, "expensive": False}
        ]
    }
    assert (await client.request("variables", {"variablesReference": 11}))["body"] == {  # type: ignore[attr-defined]
        "variables": [{"name": "answer", "value": "42", "variablesReference": 0}]
    }
    assert (
        await client.request("evaluate", {"expression": "echo value", "frameId": 4})  # type: ignore[attr-defined]
    )["body"] == {"result": "value\n", "variablesReference": 0}
    assert (await client.request("continue", {"threadId": 1}))["body"] == {  # type: ignore[attr-defined]
        "allThreadsContinued": True
    }
    for command in ("next", "stepIn", "stepOut"):
        assert (await client.request(command, {"threadId": 1}))["body"] == {}  # type: ignore[attr-defined]
    assert session.calls == [
        "prepare",
        ("set_breakpoints", Path("/work/demo.csh"), (7, 100)),
        "start",
        "threads",
        "stack_trace",
        ("scopes", 4),
        ("variables", 11),
        ("evaluate", "echo value", 4),
        "continue",
        "next",
        "step_in",
        "step_out",
    ]


@pytest.mark.asyncio
async def test_disconnect_and_terminate_follow_owned_lifecycle(
    server_client: object,
) -> None:
    client = server_client
    session = await initialize_and_launch(client)  # type: ignore[arg-type]
    response = await client.request("disconnect", {"terminateDebuggee": False})  # type: ignore[attr-defined]
    assert response["success"] is True
    assert session.terminated == 0
    response = await client.request("terminate", {})  # type: ignore[attr-defined]
    assert response["success"] is True
    assert session.terminated == 1


@pytest.mark.asyncio
async def test_initialize_records_run_in_terminal_capability(
    server_client: object,
) -> None:
    client = server_client
    server = client.server  # type: ignore[attr-defined]
    assert server._client_supports_run_in_terminal is False
    response = await client.request(  # type: ignore[attr-defined]
        "initialize",
        {"adapterID": "tcsh", "supportsRunInTerminalRequest": True},
    )
    assert response["success"] is True
    assert server._client_supports_run_in_terminal is True


@pytest.mark.asyncio
async def test_external_terminal_launch_without_capability_fails_exact_message(
    server_client: object,
) -> None:
    client = server_client
    await client.request("initialize", {"adapterID": "tcsh"})  # type: ignore[attr-defined]
    await client.next_message()  # type: ignore[attr-defined]
    response = await client.request(  # type: ignore[attr-defined]
        "launch",
        {"program": "/work/demo.csh", "console": "externalTerminal"},
    )
    assert response["success"] is False
    assert response["message"] == (
        "externalTerminal launch requires a client that supports "
        "the runInTerminal reverse request"
    )
    assert client.factory.sessions == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_terminal_background_start_failure_uses_console_category(
    server_client: object,
) -> None:
    """A background-start failure in --terminal mode must reach the user.

    session.start()'s failure inside _finish_terminal_start is reported
    via an `output` event since, by the time it runs, configurationDone
    has already answered successfully -- there's no pending request left
    to fail. The controller drops stdout/stderr `output` events in
    --terminal mode (program output goes to the external terminal
    instead), so this event must use category "console", not "stderr",
    or the failure would be silently swallowed.
    """
    client = server_client
    await client.request(  # type: ignore[attr-defined]
        "initialize",
        {"adapterID": "tcsh", "supportsRunInTerminalRequest": True},
    )
    await client.next_message()  # type: ignore[attr-defined]
    launch_response = await client.request(  # type: ignore[attr-defined]
        "launch",
        {"program": "/work/demo.csh", "console": "externalTerminal"},
    )
    assert launch_response["success"] is True
    factory = client.factory  # type: ignore[attr-defined]
    session = factory.sessions[0]
    session.start_error = RuntimeError("guardian exited before arming startup")

    response = await client.request("configurationDone", {})  # type: ignore[attr-defined]
    assert response["success"] is True

    output_event = await client.next_message()  # type: ignore[attr-defined]
    assert output_event["event"] == "output"
    assert output_event["body"]["category"] == "console"
    assert "guardian exited before arming startup" in output_event["body"]["output"]

    terminated_event = await client.next_message()  # type: ignore[attr-defined]
    assert terminated_event["event"] == "terminated"


@pytest.mark.asyncio
async def test_stray_reverse_response_is_consumed_without_unsupported_command_error(
    server_client: object,
) -> None:
    client = server_client
    await client.request("initialize", {"adapterID": "tcsh"})  # type: ignore[attr-defined]
    await client.next_message()  # type: ignore[attr-defined]
    stray_response: dict[str, object] = {
        "seq": 999,
        "type": "response",
        "request_seq": 12345,
        "command": "runInTerminal",
        "success": True,
        "body": {},
    }
    client.request_reader.feed_data(encode_message(stray_response))
    # A follow-up real request must still get its OWN response -- proves
    # the stray "response" message above produced no reply of its own (an
    # "unsupported command" error would otherwise show up here, ahead of
    # or instead of this one) and didn't wedge the read loop.
    response = await client.request("threads", {})  # type: ignore[attr-defined]
    assert response["success"] is False
    assert "launch must be requested first" in response["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "arguments", "fragment"),
    [
        ("setBreakpoints", {"source": {}, "breakpoints": []}, "source.path"),
        (
            "setBreakpoints",
            {"source": {"path": "x"}, "breakpoints": [{"line": True}]},
            "line",
        ),
        ("stackTrace", {"threadId": "1"}, "threadId"),
        ("scopes", {"frameId": True}, "frameId"),
        ("variables", {"variablesReference": "1"}, "variablesReference"),
        ("evaluate", {"expression": 7}, "expression"),
        ("evaluate", {"expression": "x", "frameId": "4"}, "frameId"),
        ("continue", {"threadId": 2}, "threadId"),
        ("next", {}, "threadId"),
    ],
)
async def test_supported_requests_validate_arguments_strictly(
    server_client: object,
    command: str,
    arguments: dict[str, object],
    fragment: str,
) -> None:
    client = server_client
    await initialize_and_launch(client)  # type: ignore[arg-type]
    response = await client.request(command, arguments)  # type: ignore[attr-defined]
    assert response["success"] is False
    assert fragment in response["message"]


@pytest.mark.asyncio
async def test_unknown_and_domain_errors_get_one_failed_response_and_server_survives(
    server_client: object,
) -> None:
    client = server_client
    session = await initialize_and_launch(client)  # type: ignore[arg-type]
    unknown = await client.request("restartFrame", {"threadId": 1})  # type: ignore[attr-defined]
    assert unknown["success"] is False
    assert "not supported" in unknown["message"]
    session.continue_error = TransportError("control channel closed")
    failed = await client.request("continue", {"threadId": 1})  # type: ignore[attr-defined]
    assert failed["success"] is False
    assert failed["message"] == "control channel closed"
    healthy = await client.request("threads", {})  # type: ignore[attr-defined]
    assert healthy["success"] is True
    responses = [
        message for message in client.messages if message["type"] == "response"
    ]  # type: ignore[attr-defined]
    assert [message["request_seq"] for message in responses[-3:]] == [3, 4, 5]
    assert len({message["request_seq"] for message in responses}) == len(responses)
    assert [message["seq"] for message in client.messages] == list(  # type: ignore[attr-defined]
        range(1, len(client.messages) + 1)  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_lifecycle_and_duplicate_requests_fail_without_replacing_session(
    server_client: object,
) -> None:
    client = server_client
    assert (await client.request("threads", {}))["success"] is False  # type: ignore[attr-defined]
    assert (await client.request("launch", {"program": "x"}))["success"] is False  # type: ignore[attr-defined]
    await client.request("initialize", {"adapterID": "tcsh"})  # type: ignore[attr-defined]
    await client.next_message()  # type: ignore[attr-defined]
    assert (await client.request("initialize", {"adapterID": "tcsh"}))[
        "success"
    ] is False  # type: ignore[attr-defined]
    assert (await client.request("configurationDone", {}))["success"] is False  # type: ignore[attr-defined]
    assert (await client.request("launch", {"program": "x"}))["success"] is True  # type: ignore[attr-defined]
    original = client.factory.sessions[0]  # type: ignore[attr-defined]
    assert (await client.request("launch", {"program": "y"}))["success"] is False  # type: ignore[attr-defined]
    assert client.factory.sessions == [original]  # type: ignore[attr-defined]
    assert (await client.request("configurationDone", {}))["success"] is True  # type: ignore[attr-defined]
    assert (await client.request("configurationDone", {}))["success"] is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_events_and_responses_share_serialized_sequence(
    server_client: object,
) -> None:
    client = server_client
    session = await initialize_and_launch(client)  # type: ignore[arg-type]
    event_task = asyncio.create_task(
        session.event_sink(
            SessionEvent("output", {"category": "stdout", "output": "hi\n"})
        )
    )
    response = await client.request("threads", {})  # type: ignore[attr-defined]
    await event_task
    if not any(message.get("event") == "output" for message in client.messages):  # type: ignore[attr-defined]
        await client.next_message()  # type: ignore[attr-defined]
    assert response["success"] is True
    assert client.writer.concurrent_write is False  # type: ignore[attr-defined]
    assert [message["seq"] for message in client.messages] == list(  # type: ignore[attr-defined]
        range(1, len(client.messages) + 1)  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_eof_terminates_active_session() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    writer = PipeWriter(response_reader)
    factory = FakeFactory()
    server = DAPServer(request_reader, writer, factory)
    task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    request_reader.feed_eof()
    await asyncio.wait_for(task, timeout=1)
    assert session.terminated == 1


@pytest.mark.asyncio
async def test_concurrent_stop_callers_share_one_termination_attempt() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    run_task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    session.block_terminate = True

    first = asyncio.create_task(server.stop())
    await asyncio.wait_for(session.terminate_started.wait(), timeout=0.1)
    second = asyncio.create_task(server.stop())
    await asyncio.sleep(0)

    concurrent_invocations = session.terminated
    session.finish_terminate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=0.1)
    await asyncio.wait_for(run_task, timeout=0.1)
    assert concurrent_invocations == 1
    assert session.terminated == 1


@pytest.mark.asyncio
async def test_termination_failure_is_shared_and_next_call_retries_once() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    run_task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    failure = RuntimeError("terminate failed")
    session.block_terminate = True
    session.terminate_error = failure

    first = asyncio.create_task(server.stop())
    await asyncio.wait_for(session.terminate_started.wait(), timeout=0.1)
    second = asyncio.create_task(server.stop())
    await asyncio.sleep(0)
    session.finish_terminate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert results == [failure, failure]
    assert session.terminated == 1
    session.block_terminate = False
    session.terminate_error = None
    await server.stop()
    await asyncio.wait_for(run_task, timeout=0.1)
    assert session.terminated == 2


@pytest.mark.asyncio
async def test_cancelling_one_stop_waiter_does_not_cancel_shared_termination() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    run_task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    session.block_terminate = True

    cancelled = asyncio.create_task(server.stop())
    await asyncio.wait_for(session.terminate_started.wait(), timeout=0.1)
    survivor = asyncio.create_task(server.stop())
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    concurrent_invocations = session.terminated
    session.finish_terminate.set()
    await asyncio.wait_for(survivor, timeout=0.1)
    await asyncio.wait_for(run_task, timeout=0.1)
    assert concurrent_invocations == 1
    assert session.terminated == 1


@pytest.mark.asyncio
async def test_signal_stop_and_run_finally_share_reentrant_event_termination() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    run_task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    session.block_terminate = True
    session.terminate_event = SessionEvent("terminated", {})

    stop_task = asyncio.create_task(server.stop())
    await asyncio.wait_for(session.terminate_started.wait(), timeout=0.1)
    request_reader.feed_eof()
    await asyncio.sleep(0)
    concurrent_invocations = session.terminated
    session.finish_terminate.set()

    await asyncio.wait_for(asyncio.gather(stop_task, run_task), timeout=0.1)
    terminal = await client.next_message()
    assert concurrent_invocations == 1
    assert session.terminated == 1
    assert terminal["event"] == "terminated"


@pytest.mark.asyncio
async def test_termination_event_writer_can_reentrantly_await_server_stop() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    server: DAPServer | None = None

    class ReentrantStopWriter(PipeWriter):
        def __init__(self, reader: asyncio.StreamReader) -> None:
            super().__init__(reader)
            self.armed = False
            self.termination_state_during_reentry: list[bool] = []

        async def drain(self) -> None:
            await super().drain()
            if self.armed:
                self.armed = False
                assert server is not None
                await server.stop()
                self.termination_state_during_reentry.append(server._session_terminated)

    writer = ReentrantStopWriter(response_reader)
    factory = FakeFactory()
    server = DAPServer(request_reader, writer, factory)
    run_task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    await asyncio.sleep(0)
    session.terminate_event = SessionEvent("terminated", {})
    writer.armed = True

    try:
        await asyncio.wait_for(server.stop(), timeout=0.1)
        await asyncio.wait_for(run_task, timeout=0.1)
    finally:
        task = server._termination_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert writer.termination_state_during_reentry == [False]
    assert session.terminated == 1
    assert (await client.next_message())["event"] == "terminated"


@pytest.mark.asyncio
async def test_cancelled_only_stop_waiter_retrieves_later_termination_failure() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    server = DAPServer(request_reader, PipeWriter(response_reader), FakeFactory())

    async def discard_event(event: SessionEvent) -> None:
        del event

    config = LaunchConfig(
        Path("/work/demo.csh"),
        (),
        Path("/work"),
        {},
        Path("/mock/tcsh"),
        True,
    )
    session = FakeSession(config, discard_event)
    failure = RuntimeError("unobserved terminate failure")
    session.block_terminate = True
    session.terminate_error = failure
    server._session = session  # type: ignore[assignment]
    contexts: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda unused_loop, context: contexts.append(context))
    waiter = asyncio.create_task(server.stop())
    termination_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(session.terminate_started.wait(), timeout=0.1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        session.finish_terminate.set()
        while server._termination_task is None or not server._termination_task.done():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        termination_task = server._termination_task
        assert termination_task._log_traceback is False

        session.block_terminate = False
        session.terminate_error = None
        await server.stop()
        gc.collect()
        await asyncio.sleep(0)

        assert session.terminated == 2
        assert contexts == []
    finally:
        loop.set_exception_handler(previous_handler)
        if termination_task is not None and termination_task._log_traceback:
            termination_task.exception()


@pytest.mark.asyncio
async def test_disconnect_without_termination_supervises_detached_session_on_eof() -> (
    None
):
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)

    response = await client.request("disconnect", {"terminateDebuggee": False})
    request_reader.feed_eof()

    assert response["success"] is True
    await asyncio.wait_for(session.wait_started.wait(), timeout=0.1)
    assert session.detached == 1
    assert session.terminated == 0
    assert not task.done()
    session.finish_wait.set()
    await asyncio.wait_for(task, timeout=0.1)
    assert session.terminated == 0


@pytest.mark.asyncio
async def test_configured_disconnect_without_termination_responds_before_terminal_event() -> (
    None
):
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    session.configured_detach = True

    response = await client.request("disconnect", {"terminateDebuggee": False})
    terminal = await client.next_message()
    request_reader.feed_eof()
    await asyncio.wait_for(task, timeout=0.1)

    assert response["success"] is True
    assert response["seq"] < terminal["seq"]
    assert session.detached == 1
    assert session.terminated == 0


@pytest.mark.asyncio
async def test_cancelling_detached_wait_reclaims_session_ownership() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    assert (await client.request("disconnect", {"terminateDebuggee": False}))[
        "success"
    ] is True
    request_reader.feed_eof()
    await asyncio.wait_for(session.wait_started.wait(), timeout=0.1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.terminated == 1
    await server.stop()
    assert session.terminated == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["protocol_error", "signal_stop"])
async def test_detach_does_not_suppress_fatal_or_signal_cleanup(shutdown: str) -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    factory = FakeFactory()
    server = DAPServer(request_reader, PipeWriter(response_reader), factory)
    task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    assert (await client.request("disconnect", {"terminateDebuggee": False}))[
        "success"
    ] is True

    if shutdown == "protocol_error":
        request_reader.feed_data(b"Missing-Colon\r\n\r\n")
        with pytest.raises(ProtocolError, match="Malformed header"):
            await asyncio.wait_for(task, timeout=0.1)
    else:
        await server.stop()
        await asyncio.wait_for(task, timeout=0.1)

    assert session.terminated == 1
    assert session.wait_started.is_set() is False


@pytest.mark.asyncio
async def test_framing_corruption_is_fatal_and_cleans_up_session() -> None:
    request_reader = asyncio.StreamReader()
    response_reader = asyncio.StreamReader()
    writer = PipeWriter(response_reader)
    factory = FakeFactory()
    server = DAPServer(request_reader, writer, factory)
    task = asyncio.create_task(server.run())
    client = InMemoryClient(request_reader, response_reader)
    client.factory = factory  # type: ignore[attr-defined]
    session = await initialize_and_launch(client)
    request_reader.feed_data(b"Missing-Colon\r\n\r\n")
    with pytest.raises(ProtocolError, match="Malformed header"):
        await asyncio.wait_for(task, timeout=1)
    assert session.terminated == 1


@pytest.mark.asyncio
async def test_black_box_client_observes_initialized_response_order() -> None:
    client = await DAPClient.start()
    try:
        response = await client.request("initialize", {"adapterID": "tcsh"})
        initialized = await client.wait_for_event("initialized")
        assert response["success"] is True
        assert response["seq"] < initialized["seq"]
        assert client.stderr == ""
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_black_box_configured_disconnect_without_termination(
    tmp_path: Path,
    tcsh_path: Path,
) -> None:
    program = tmp_path / "prepared-only.csh"
    program.write_text("echo never-started\n")
    client = await DAPClient.start(shutdown_timeout=0.2)
    try:
        assert (await client.request("initialize", {"adapterID": "tcsh"}))[
            "success"
        ] is True
        await client.wait_for_event("initialized")
        assert (
            await client.request(
                "launch",
                {"program": str(program), "tcshPath": str(tcsh_path)},
            )
        )["success"] is True

        response = await client.request("disconnect", {"terminateDebuggee": False})

        assert response["success"] is True
        terminal = await client.wait_for_event("terminated", timeout=0.2)
        assert response["seq"] < terminal["seq"]
        assert not any(message.get("event") == "exited" for message in client.messages)
    finally:
        await client.close()


def test_black_box_protocol_corruption_exits_two_without_stdout() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "tdb.adapters.tcsh"],
        input=b"Missing-Colon\r\n\r\n",
        capture_output=True,
        env=environment,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"protocol" in completed.stderr.lower()


def test_black_box_truncated_body_exits_two_without_stdout() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "tdb.adapters.tcsh"],
        input=b"Content-Length: 5\r\n\r\n",
        capture_output=True,
        env=environment,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"truncated message body" in completed.stderr.lower()


def test_black_box_devnull_eof_exits_cleanly_without_workspace() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    before = set(Path(tempfile.gettempdir()).glob("tcsh-dap-*"))

    with Path(os.devnull).open("rb") as stdin:
        completed = subprocess.run(
            [sys.executable, "-m", "tdb.adapters.tcsh"],
            stdin=stdin,
            capture_output=True,
            env=environment,
            check=False,
            timeout=1,
        )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
    assert set(Path(tempfile.gettempdir()).glob("tcsh-dap-*")) == before


@pytest.mark.asyncio
async def test_black_box_client_close_kills_child_that_ignores_sigterm() -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import signal,sys,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('ready', flush=True);"
            "time.sleep(30)"
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert await process.stdout.readline() == b"ready\n"
    client = DAPClient(process, shutdown_timeout=0.05)

    await asyncio.wait_for(client.close(), timeout=0.5)

    assert process.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_stdout_writer_does_not_block_event_loop_on_pipe_backpressure() -> None:
    reader_descriptor, writer_descriptor = os.pipe()
    os.set_blocking(writer_descriptor, False)
    try:
        while True:
            try:
                os.write(writer_descriptor, b"x" * (64 * 1024))
            except BlockingIOError:
                break
        os.set_blocking(writer_descriptor, True)
        stream = os.fdopen(writer_descriptor, "wb", buffering=0, closefd=False)
        writer = cli._StdoutWriter(stream)  # type: ignore[attr-defined]
        reader_released_capacity = threading.Event()

        def release_capacity() -> None:
            reader_released_capacity.set()
            os.read(reader_descriptor, 64 * 1024)

        release = threading.Timer(0.2, release_capacity)
        release.start()
        try:
            writer.write(b"framed message")
            assert not reader_released_capacity.is_set()

            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            drain = asyncio.create_task(writer.drain())
            await asyncio.wait_for(heartbeat.wait(), timeout=0.05)
            await asyncio.wait_for(drain, timeout=1)
        finally:
            release.join(timeout=1)
            close = getattr(writer, "close", None)
            if close is not None:
                await close()
            stream.close()
    finally:
        os.close(reader_descriptor)
        os.close(writer_descriptor)


@pytest.mark.asyncio
async def test_cancelled_stdout_drain_preserves_serialized_byte_order() -> None:
    reader_descriptor, writer_descriptor = os.pipe()
    os.set_blocking(writer_descriptor, False)
    filled = 0
    try:
        while True:
            try:
                filled += os.write(writer_descriptor, b"x" * (64 * 1024))
            except BlockingIOError:
                break
        stream = os.fdopen(writer_descriptor, "wb", buffering=0, closefd=False)
        writer = cli._StdoutWriter(stream)  # type: ignore[attr-defined]
        first_frame = b"first-frame" * 4096
        second_frame = b"second-frame" * 4096
        writer.write(first_frame)
        first_drain = asyncio.create_task(writer.drain())
        await asyncio.sleep(0)
        assert not first_drain.done()
        first_drain.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_drain

        writer.write(second_frame)
        received = bytearray()
        expected_length = filled + len(first_frame) + len(second_frame)

        def read_everything() -> None:
            while len(received) < expected_length:
                received.extend(
                    os.read(reader_descriptor, expected_length - len(received))
                )

        reader = threading.Thread(target=read_everything)
        reader.start()
        try:
            await asyncio.wait_for(writer.drain(), timeout=1)
            reader.join(timeout=1)
            assert not reader.is_alive()
            assert bytes(received[filled:]) == first_frame + second_frame
        finally:
            if reader.is_alive():
                os.close(writer_descriptor)
                reader.join(timeout=1)
            await writer.close()
            stream.close()
    finally:
        os.close(reader_descriptor)
        try:
            os.close(writer_descriptor)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_stdout_writer_close_cancels_backpressure_and_restores_mode() -> None:
    reader_descriptor, writer_descriptor = os.pipe()
    os.set_blocking(writer_descriptor, False)
    try:
        while True:
            try:
                os.write(writer_descriptor, b"x" * (64 * 1024))
            except BlockingIOError:
                break
        os.set_blocking(writer_descriptor, True)
        stream = os.fdopen(writer_descriptor, "wb", buffering=0, closefd=False)
        writer = cli._StdoutWriter(stream)  # type: ignore[attr-defined]
        writer.write(b"blocked frame")
        drain = asyncio.create_task(writer.drain())
        await asyncio.sleep(0)
        assert not drain.done()

        await asyncio.wait_for(writer.close(), timeout=0.1)
        await asyncio.gather(drain, return_exceptions=True)

        assert drain.cancelled()
        assert os.get_blocking(writer_descriptor) is True
        stream.close()
    finally:
        os.close(reader_descriptor)
        os.close(writer_descriptor)


@pytest.mark.asyncio
async def test_cancelled_stdout_writer_close_still_restores_descriptor_mode() -> None:
    reader_descriptor, writer_descriptor = os.pipe()
    os.set_blocking(writer_descriptor, True)
    stream = os.fdopen(writer_descriptor, "wb", buffering=0, closefd=False)
    writer = cli._StdoutWriter(stream)  # type: ignore[attr-defined]
    cleanup_started = asyncio.Event()

    async def delayed_cancel(data: bytes) -> None:
        del data
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleanup_started.set()
            await asyncio.Future()

    writer._write_all = delayed_cancel  # type: ignore[method-assign]
    writer.write(b"frame")
    drain = asyncio.create_task(writer.drain())
    await asyncio.sleep(0)
    close = asyncio.create_task(writer.close())
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.1)

    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close

    try:
        assert os.get_blocking(writer_descriptor) is True
    finally:
        await asyncio.gather(drain, return_exceptions=True)
        stream.close()
        os.close(reader_descriptor)
        os.close(writer_descriptor)


@pytest.mark.parametrize(
    ("error", "exit_code", "diagnostic"),
    [
        (ProtocolError("bad frame"), 2, "bad frame"),
        (RuntimeError("adapter exploded"), 1, "Traceback"),
    ],
)
def test_cli_maps_adapter_failures_to_stderr_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    exit_code: int,
    diagnostic: str,
) -> None:
    monkeypatch.setattr(cli, "_run_adapter", lambda: None, raising=False)

    def fail_run(awaitable: object) -> None:
        del awaitable
        raise error

    monkeypatch.setattr(asyncio, "run", fail_run)
    assert cli.main() == exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    assert diagnostic in captured.err
