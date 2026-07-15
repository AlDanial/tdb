"""Unit tests for tdb.dap.client.DAPClient.

The client is exercised against a fake in-process DAP adapter speaking
the real wire protocol (Content-Length framing over a loopback TCP
socket, via `connect()`). This covers the read loop, request/response
future pairing, event dispatch, reverse requests, and the argument
encoding / response parsing of every high-level command — without
spawning debugpy.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from tdb.dap.client import DAPClient, DAPError
from tdb.dap.messages import Request, Response, parse_message
from tdb.dap.protocol import encode_message, read_message
from tdb.dap.types import Source, SourceBreakpoint
from tdb.languages.base import AdapterQuirks


class FakeAdapter:
    """In-process DAP adapter on a loopback socket.

    Auto-responds success with an empty body unless the command is
    listed in `silent` (leave unanswered), `errors` (respond failure),
    or has a `responders` entry (respond with its return value as body).
    """

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.responses: asyncio.Queue[Response] = asyncio.Queue()
        self.responders: dict[str, Callable[[Request], dict]] = {}
        self.silent: set[str] = set()
        self.errors: dict[str, str] = {}
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._seq = 1000

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader, writer) -> None:
        self._writer = writer
        try:
            while True:
                msg = parse_message(await read_message(reader))
                if isinstance(msg, Response):
                    await self.responses.put(msg)
                    continue
                self.requests.append(msg)
                if msg.command in self.silent:
                    continue
                if msg.command in self.errors:
                    self.respond(msg, success=False, message=self.errors[msg.command])
                    continue
                responder = self.responders.get(msg.command)
                self.respond(msg, body=responder(msg) if responder else {})
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def respond(
        self,
        request: Request,
        success: bool = True,
        body: dict | None = None,
        message: str | None = None,
    ) -> None:
        assert self._writer is not None
        data: dict[str, Any] = {
            "seq": self._next_seq(),
            "type": "response",
            "request_seq": request.seq,
            "command": request.command,
            "success": success,
        }
        if body:
            data["body"] = body
        if message:
            data["message"] = message
        self._writer.write(encode_message(data))

    def send_event(self, event: str, body: dict | None = None) -> None:
        assert self._writer is not None
        self._writer.write(
            encode_message(
                {
                    "seq": self._next_seq(),
                    "type": "event",
                    "event": event,
                    "body": body or {},
                }
            )
        )

    def send_request(self, command: str, arguments: dict | None = None) -> None:
        assert self._writer is not None
        self._writer.write(
            encode_message(
                {
                    "seq": self._next_seq(),
                    "type": "request",
                    "command": command,
                    "arguments": arguments or {},
                }
            )
        )

    async def wait_for_requests(
        self, command: str, count: int = 1, timeout: float = 2.0
    ) -> list[Request]:
        async def _poll() -> list[Request]:
            while True:
                got = [r for r in self.requests if r.command == command]
                if len(got) >= count:
                    return got
                await asyncio.sleep(0.005)

        return await asyncio.wait_for(_poll(), timeout)

    def requests_for(self, command: str) -> list[Request]:
        """Synchronously return already-received requests for `command`.

        Unlike `wait_for_requests`, this doesn't poll/await — callers must
        ensure the request has already landed (e.g. by awaiting the call
        that sends it).
        """
        return [r for r in self.requests if r.command == command]


@pytest.fixture
async def dap():
    adapter = FakeAdapter()
    port = await adapter.start()
    client = DAPClient()
    await client.connect("127.0.0.1", port)
    yield client, adapter
    await client.stop()
    await adapter.close()


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    async def _poll():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout)


# --- Request/response plumbing ----------------------------------------


async def test_initialize_sends_client_info_and_parses_capabilities(dap):
    client, adapter = dap
    adapter.responders["initialize"] = lambda req: {
        "supportsConfigurationDoneRequest": True,
        "supportsCompletionsRequest": True,
    }
    caps = await client.initialize()
    assert caps.supports_configuration_done_request is True
    assert caps.supports_completions_request is True
    assert client.capabilities is caps
    (req,) = await adapter.wait_for_requests("initialize")
    assert req.arguments["clientID"] == "tdb"
    assert req.arguments["adapterID"] == "debugpy"
    assert req.arguments["linesStartAt1"] is True
    assert req.arguments["supportsRunInTerminalRequest"] is False


async def test_out_of_order_responses_pair_by_request_seq(dap):
    client, adapter = dap
    adapter.silent.add("evaluate")
    t1 = asyncio.create_task(client.evaluate("first"))
    t2 = asyncio.create_task(client.evaluate("second"))
    reqs = await adapter.wait_for_requests("evaluate", count=2)
    by_expr = {r.arguments["expression"]: r for r in reqs}
    # Answer in reverse order of sending.
    adapter.respond(by_expr["second"], body={"result": "2nd"})
    adapter.respond(by_expr["first"], body={"result": "1st"})
    assert await t1 == ("1st", 0)
    assert await t2 == ("2nd", 0)


async def test_error_response_raises_dap_error(dap):
    client, adapter = dap
    adapter.errors["evaluate"] = "name 'x' is not defined"
    with pytest.raises(DAPError, match="evaluate.*name 'x' is not defined"):
        await client.evaluate("x")


async def test_unanswered_request_times_out(dap, monkeypatch):
    client, adapter = dap
    monkeypatch.setattr("tdb._timeouts.DAP_REQUEST", 0.05)
    adapter.silent.add("threads")
    with pytest.raises(asyncio.TimeoutError):
        await client.threads()


async def test_send_without_stream_raises_connection_error():
    client = DAPClient()  # never connected
    with pytest.raises(ConnectionError, match="no active write stream"):
        await client.threads()


async def test_stop_closes_connection(dap):
    client, adapter = dap
    await client.stop()
    with pytest.raises(ConnectionError):
        await client.threads()


# --- Events ------------------------------------------------------------


async def test_events_dispatch_to_named_and_wildcard_handlers(dap):
    client, adapter = dap
    named, wildcard = [], []
    client.on_event("stopped", named.append)
    client.on_event("*", wildcard.append)
    adapter.send_event("stopped", {"reason": "breakpoint", "threadId": 1})
    adapter.send_event("output", {"output": "hi"})
    await _wait_until(lambda: len(wildcard) == 2)
    assert len(named) == 1
    assert named[0].body["reason"] == "breakpoint"
    assert {e.event for e in wildcard} == {"stopped", "output"}


async def test_orphaned_response_is_dropped_without_error(dap):
    client, adapter = dap
    await client.continue_nowait(thread_id=1)  # response intentionally not awaited
    (req,) = await adapter.wait_for_requests("continue")
    assert req.arguments == {"threadId": 1}
    # The auto-response for it arrives with no pending future by the
    # time we've moved on — the read loop must survive.
    adapter.send_event("continued", {})
    seen = []
    client.on_event("continued", seen.append)
    adapter.send_event("continued", {})
    await _wait_until(lambda: seen)


# --- Reverse requests ---------------------------------------------------


async def test_reverse_request_dispatches_to_handler(dap):
    client, adapter = dap

    async def handler(request: Request) -> dict:
        assert request.arguments["kind"] == "integrated"
        return {"processId": 4242}

    client.on_reverse_request("runInTerminal", handler)
    adapter.send_request("runInTerminal", {"kind": "integrated"})
    rsp = await asyncio.wait_for(adapter.responses.get(), 2.0)
    assert rsp.success is True
    assert rsp.body == {"processId": 4242}
    assert rsp.command == "runInTerminal"


async def test_reverse_request_handler_exception_reports_failure(dap):
    client, adapter = dap

    async def handler(request: Request) -> dict:
        raise RuntimeError("terminal exploded")

    client.on_reverse_request("runInTerminal", handler)
    adapter.send_request("runInTerminal", {})
    rsp = await asyncio.wait_for(adapter.responses.get(), 2.0)
    assert rsp.success is False
    assert "terminal exploded" in (rsp.message or "")


async def test_reverse_request_without_handler_reports_not_supported(dap):
    client, adapter = dap
    adapter.send_request("startDebugging", {})
    rsp = await asyncio.wait_for(adapter.responses.get(), 2.0)
    assert rsp.success is False
    assert rsp.message == "Not supported"


# --- launch / attach (deferred-response pattern) ------------------------


async def test_launch_returns_pending_future_and_encodes_arguments(dap):
    client, adapter = dap
    adapter.silent.add("launch")
    future = await client.launch(
        program="/abs/prog.py",
        args=["--flag"],
        cwd="/work",
        stop_on_entry=True,
        just_my_code=False,
        python="/venvs/x/bin/python",
        env={"TOKEN": "s3cret"},
        sub_process=False,
    )
    assert not future.done()  # debugpy holds the response
    (req,) = await adapter.wait_for_requests("launch")
    a = req.arguments
    assert a["program"] == "/abs/prog.py"
    assert a["args"] == ["--flag"]
    assert a["cwd"] == "/work"
    assert a["stopOnEntry"] is True
    assert a["justMyCode"] is False
    assert a["subProcess"] is False
    assert a["python"] == "/venvs/x/bin/python"
    assert a["env"] == {"TOKEN": "s3cret"}
    assert a["pythonArgs"] == ["-Xfrozen_modules=off"]
    adapter.respond(req)  # configurationDone finally releases it
    assert (await asyncio.wait_for(future, 2.0)).success is True


async def test_launch_omits_optional_arguments_by_default(dap):
    client, adapter = dap
    adapter.silent.add("launch")
    await client.launch(program="/abs/prog.py")
    (req,) = await adapter.wait_for_requests("launch")
    assert "python" not in req.arguments
    assert "env" not in req.arguments
    assert req.arguments["console"] == "internalConsole"
    assert req.arguments["redirectOutput"] is True


async def test_attach_encodes_connect_and_path_mappings(dap):
    client, adapter = dap
    adapter.silent.add("attach")
    await client.attach(
        host="10.0.0.5",
        port=5678,
        path_mappings=[("/local/src", "/remote/app")],
    )
    (req,) = await adapter.wait_for_requests("attach")
    assert req.arguments["connect"] == {"host": "10.0.0.5", "port": 5678}
    assert req.arguments["pathMappings"] == [
        {"localRoot": "/local/src", "remoteRoot": "/remote/app"}
    ]
    assert "subProcessId" not in req.arguments


async def test_attach_routes_to_child_via_sub_process_id(dap):
    client, adapter = dap
    adapter.silent.add("attach")
    await client.attach(port=5678, sub_process_id=999)
    (req,) = await adapter.wait_for_requests("attach")
    assert req.arguments["subProcessId"] == 999


# --- High-level commands: encoding + parsing ----------------------------


async def test_set_breakpoints_round_trip(dap):
    client, adapter = dap
    adapter.responders["setBreakpoints"] = lambda req: {
        "breakpoints": [{"id": 7, "verified": True, "line": 12}]
    }
    result = await client.set_breakpoints(
        "/abs/f.py",
        [SourceBreakpoint(line=12, condition="x > 3")],
    )
    (req,) = await adapter.wait_for_requests("setBreakpoints")
    assert req.arguments["source"] == {"path": "/abs/f.py"}
    assert req.arguments["breakpoints"][0]["line"] == 12
    assert req.arguments["breakpoints"][0]["condition"] == "x > 3"
    assert len(result) == 1
    assert result[0].verified is True and result[0].line == 12


async def test_breakpoint_locations_returns_line_set(dap):
    client, adapter = dap
    adapter.responders["breakpointLocations"] = lambda req: {
        "breakpoints": [{"line": 3}, {"line": 5}, {"line": 3}]
    }
    lines = await client.breakpoint_locations("/abs/f.py", start_line=1, end_line=20)
    assert lines == {3, 5}
    (req,) = await adapter.wait_for_requests("breakpointLocations")
    assert req.arguments["endLine"] == 20


async def test_threads_and_stack_trace_parse_typed_results(dap):
    client, adapter = dap
    adapter.responders["threads"] = lambda req: {
        "threads": [{"id": 1, "name": "MainThread"}]
    }
    adapter.responders["stackTrace"] = lambda req: {
        "stackFrames": [
            {"id": 11, "name": "main", "line": 4, "source": {"path": "/f.py"}}
        ]
    }
    threads = await client.threads()
    assert threads[0].id == 1 and threads[0].name == "MainThread"
    frames = await client.stack_trace(thread_id=1, start_frame=0, levels=5)
    assert frames[0].id == 11
    assert frames[0].source.path == "/f.py"
    (req,) = await adapter.wait_for_requests("stackTrace")
    assert req.arguments == {"threadId": 1, "startFrame": 0, "levels": 5}


async def test_scopes_and_variables_parse_and_paginate(dap):
    client, adapter = dap
    adapter.responders["scopes"] = lambda req: {
        "scopes": [{"name": "Locals", "variablesReference": 100}]
    }
    adapter.responders["variables"] = lambda req: {
        "variables": [{"name": "x", "value": "7", "type": "int"}]
    }
    scopes = await client.scopes(frame_id=11)
    assert scopes[0].variables_reference == 100
    plain = await client.variables(100)
    assert plain[0].name == "x" and plain[0].value == "7"
    await client.variables(100, start=500, count=500)
    reqs = await adapter.wait_for_requests("variables", count=2)
    assert "start" not in reqs[0].arguments and "count" not in reqs[0].arguments
    assert reqs[1].arguments["start"] == 500 and reqs[1].arguments["count"] == 500


async def test_source_returns_content(dap):
    client, adapter = dap
    adapter.responders["source"] = lambda req: {"content": "print('hi')\n"}
    text = await client.source(0, Source(path="/remote/f.py", name="f.py"))
    assert text == "print('hi')\n"
    (req,) = await adapter.wait_for_requests("source")
    assert req.arguments["source"]["path"] == "/remote/f.py"


async def test_source_failure_returns_empty_string(dap):
    client, adapter = dap
    adapter.errors["source"] = "Source unavailable"
    assert await client.source(42) == ""  # swallowed, no raise


async def test_evaluate_includes_frame_id_when_given(dap):
    client, adapter = dap
    adapter.responders["evaluate"] = lambda req: {
        "result": "'ok'",
        "variablesReference": 9,
    }
    result, ref = await client.evaluate("expr", frame_id=11, context="hover")
    assert (result, ref) == ("'ok'", 9)
    (req,) = await adapter.wait_for_requests("evaluate")
    assert req.arguments["frameId"] == 11
    assert req.arguments["context"] == "hover"


async def test_completions_parse_targets(dap):
    client, adapter = dap
    adapter.responders["completions"] = lambda req: {
        "targets": [{"label": "startswith", "type": "method"}]
    }
    items = await client.completions("s.start", column=8)
    assert items[0].label == "startswith"


@pytest.mark.parametrize(
    "method,command",
    [
        ("continue_", "continue"),
        ("next", "next"),
        ("step_in", "stepIn"),
        ("step_out", "stepOut"),
        ("pause", "pause"),
    ],
)
async def test_execution_commands_send_thread_id(dap, method, command):
    client, adapter = dap
    await getattr(client, method)(5)
    (req,) = await adapter.wait_for_requests(command)
    assert req.arguments == {"threadId": 5}


async def test_disconnect_sends_terminate_flag_and_swallows_errors(dap):
    client, adapter = dap
    adapter.errors["disconnect"] = "already gone"
    await client.disconnect(terminate=False)  # must not raise
    (req,) = await adapter.wait_for_requests("disconnect")
    assert req.arguments == {"terminateDebuggee": False}


async def test_set_exception_breakpoints_and_configuration_done(dap):
    client, adapter = dap
    await client.set_exception_breakpoints(["userUnhandled"])
    await client.configuration_done()
    (req,) = await adapter.wait_for_requests("setExceptionBreakpoints")
    assert req.arguments == {"filters": ["userUnhandled"]}
    await adapter.wait_for_requests("configurationDone")


# --- Adapter death ------------------------------------------------------


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProcess:
    def __init__(self, returncode: int, stderr: bytes) -> None:
        self.returncode = returncode
        self.stderr = _FakeStderr(stderr)

    async def wait(self) -> int:
        return self.returncode


async def test_adapter_death_fails_pending_requests():
    client = DAPClient()
    client._process = _FakeProcess(
        3, b"Traceback (most recent call last):\nModuleNotFoundError: debugpy\n"
    )
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    client._pending[1] = fut
    await client._watch_adapter_death()
    with pytest.raises(ConnectionError, match="exit 3.*ModuleNotFoundError"):
        fut.result()
    assert client._pending == {}


async def test_adapter_death_without_stderr_reports_no_output():
    client = DAPClient()
    client._process = _FakeProcess(1, b"")
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    client._pending[1] = fut
    await client._watch_adapter_death()
    with pytest.raises(ConnectionError, match="no output"):
        fut.result()


# --- AdapterSpec threading -----------------------------------------------


class _RecordingSpec:
    """Minimal AdapterSpec double that records what the client asked for."""

    id = "recording"
    quirks = AdapterQuirks()

    def __init__(self):
        self.launch_calls = []
        self.attach_calls = []

    def command(self):
        return ["true"]  # never actually spawned in these tests

    def launch_body(self, **kwargs):
        self.launch_calls.append(kwargs)
        return {"request": "launch", "program": kwargs["program"]}

    def attach_body(self, **kwargs):
        self.attach_calls.append(kwargs)
        return {"request": "attach", "port": kwargs["port"]}

    def pick_exception_filters(self, caps):
        return []


async def test_initialize_sends_adapter_id_from_spec(dap):
    client, adapter = dap
    client._adapter = _RecordingSpec()
    await client.initialize()
    req = adapter.requests_for("initialize")[-1]
    assert req.arguments["adapterID"] == "recording"


async def test_launch_body_comes_from_adapter_spec(dap):
    client, adapter = dap
    spec = _RecordingSpec()
    client._adapter = spec
    fut = await client.launch(
        program="p.py", args=["x"], stop_on_entry=True, just_my_code=False
    )
    assert spec.launch_calls == [
        {
            "program": "p.py",
            "args": ["x"],
            "cwd": ".",
            "env": None,
            "stop_on_entry": True,
            "console": "internalConsole",
            "opts": {"just_my_code": False},
        }
    ]
    (req,) = await adapter.wait_for_requests("launch")
    assert req.arguments == {"request": "launch", "program": "p.py"}
    fut.cancel()


async def test_attach_body_comes_from_adapter_spec(dap):
    client, adapter = dap
    spec = _RecordingSpec()
    client._adapter = spec
    fut = await client.attach(host="h", port=9, sub_process_id=3)
    assert spec.attach_calls == [
        {"host": "h", "port": 9, "opts": {"sub_process_id": 3}}
    ]
    fut.cancel()


def test_default_adapter_is_debugpy():
    from tdb.dap.client import DAPClient
    from tdb.languages.python import DebugpyAdapter

    assert isinstance(DAPClient()._adapter, DebugpyAdapter)


async def test_launch_normalizes_missing_args_and_cwd(dap):
    """Not in the brief: the client must still default args/cwd itself,
    since DebugpyAdapter.launch_body does not normalize them (Task 3
    reviewer flag)."""
    client, adapter = dap
    spec = _RecordingSpec()
    client._adapter = spec
    fut = await client.launch(program="p.py")
    assert spec.launch_calls[-1]["args"] == []
    assert spec.launch_calls[-1]["cwd"] == "."
    fut.cancel()
