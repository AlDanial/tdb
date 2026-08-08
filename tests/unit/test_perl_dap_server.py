import asyncio
import json

import pytest

import tdb.adapters.perl.server as server_mod
from tdb.adapters.perl.server import PerlDapServer
from tdb.adapters.perl.session import PerlProtocolError
from tdb.dap.messages import Request
from tdb.dap.protocol import encode_message


class SinkWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        pass


def _messages(writer: SinkWriter) -> list[dict]:
    blob = b"".join(writer.chunks)
    out = []
    while blob:
        header, _, rest = blob.partition(b"\r\n\r\n")
        length = int(header.split(b":")[1])
        out.append(json.loads(rest[:length]))
        blob = rest[length:]
    return out


async def _run_conversation(requests: list[dict]) -> list[dict]:
    reader = asyncio.StreamReader()
    for req in requests:
        reader.feed_data(encode_message(req))
    reader.feed_eof()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    await asyncio.wait_for(server.run(), timeout=5)
    return _messages(writer)


async def test_initialize_then_disconnect():
    out = await _run_conversation(
        [
            {
                "seq": 1,
                "type": "request",
                "command": "initialize",
                "arguments": {"adapterID": "perl-tdb"},
            },
            {"seq": 2, "type": "request", "command": "disconnect"},
        ]
    )
    init = out[0]
    assert init["type"] == "response" and init["command"] == "initialize"
    assert init["success"] is True
    assert init["body"]["supportsConfigurationDoneRequest"] is True
    assert init["body"]["supportsConditionalBreakpoints"] is True
    disc = [m for m in out if m.get("command") == "disconnect"][0]
    assert disc["success"] is True


async def test_unknown_command_errors_but_survives():
    out = await _run_conversation(
        [
            {"seq": 1, "type": "request", "command": "frobnicate"},
            {"seq": 2, "type": "request", "command": "disconnect"},
        ]
    )
    frob = [m for m in out if m.get("command") == "frobnicate"][0]
    assert frob["success"] is False
    assert "frobnicate" in frob["message"]
    assert any(m.get("command") == "disconnect" for m in out)


async def test_handler_exception_becomes_error_response():
    reader = asyncio.StreamReader()
    reader.feed_data(
        encode_message(
            {"seq": 1, "type": "request", "command": "initialize", "arguments": {}}
        )
    )
    reader.feed_data(
        encode_message({"seq": 2, "type": "request", "command": "disconnect"})
    )
    reader.feed_eof()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)

    async def boom(request):
        raise RuntimeError("kaput")

    server.handlers["initialize"] = boom
    await asyncio.wait_for(server.run(), timeout=5)
    init = [m for m in _messages(writer) if m.get("command") == "initialize"][0]
    assert init["success"] is False and "kaput" in init["message"]


async def test_launch_failure_tears_down_session(tmp_path, monkeypatch):
    program = tmp_path / "prog.pl"
    program.write_text("1;\n")

    stop_calls: list[bool] = []

    class StubSession:
        def __init__(self, on_output, on_stop) -> None:
            self.stopped = False

        async def launch(self, **kwargs):
            raise PerlProtocolError("boom")

        async def stop(self) -> None:
            stop_calls.append(True)

    monkeypatch.setattr(server_mod, "PerlSession", StubSession)

    # The failure under test is session.launch() raising AFTER the
    # perl-version preflight passed. Stub the preflight subprocess so
    # the test doesn't require a system perl (CI images may not have
    # one) -- without this, a perl-less machine takes the "perl not
    # usable" early-error path and the teardown never runs.
    class StubPreflight:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        return StubPreflight()

    monkeypatch.setattr(server_mod.asyncio, "create_subprocess_exec", fake_exec)

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)

    request = Request(
        seq=1,
        command="launch",
        arguments={"perl": "perl", "program": str(program)},
    )
    await server._on_launch(request)

    out = _messages(writer)
    launch_resp = [m for m in out if m.get("command") == "launch"][0]
    assert launch_resp["success"] is False
    assert server.session is None
    assert stop_calls == [True]


async def test_setBreakpoints_translates_to_remote_and_keys_by_remote_path():
    # Canonical keying: breakpoint_lines is keyed by REMOTE path (what
    # perl5db itself reports back via location()/stack()), not the
    # caller's local path.
    class StubSession:
        def __init__(self) -> None:
            self.stopped = True
            self.commands: list[str] = []

        async def command(self, text, timeout=20.0):
            self.commands.append(text)
            return []

        async def helper(self, expr, timeout=20.0):
            raise PerlProtocolError("no breakable info")

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    stub = StubSession()
    server.session = stub
    server._path_map = [("/local/src", "/srv/app")]

    original_path = "/local/src/x.pl"
    request = Request(
        seq=1,
        command="setBreakpoints",
        arguments={
            "source": {"path": original_path},
            "breakpoints": [{"line": 10}],
        },
    )
    await server._on_setBreakpoints(request)

    assert "b /srv/app/x.pl:10" in stub.commands
    # the incoming request's local path is left untouched
    assert request.arguments["source"]["path"] == original_path

    out = _messages(writer)
    resp = [m for m in out if m.get("command") == "setBreakpoints"][0]
    assert resp["success"] is True
    assert resp["body"]["breakpoints"] == [{"verified": True, "line": 10}]
    assert server.breakpoint_lines == {"/srv/app/x.pl": {10}}


async def test_setBreakpoints_defers_during_compile_phase():
    """Background 8 / requirement 5: a setBreakpoints request that
    arrives while the debuggee is still compiling (phase START) must
    defer -- never call b/breakable() on a partially-compiled file --
    and answer verified:false with DEFERRED_VERIFICATION_MESSAGE so
    controller.py's _warn_unbound_breakpoints knows this is a deferral,
    not a genuine bind failure (see final-review Important #2)."""

    class StubSession:
        def __init__(self) -> None:
            self.stopped = True
            self.commands: list[str] = []

        async def command(self, text, timeout=20.0):
            self.commands.append(text)
            return []

        async def helper(self, expr, timeout=20.0):
            assert "phase()" in expr
            return {"phase": "START"}

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    stub = StubSession()
    server.session = stub

    request = Request(
        seq=1,
        command="setBreakpoints",
        arguments={
            "source": {"path": "/local/src/x.pl"},
            "breakpoints": [{"line": 10}],
        },
    )
    await server._on_setBreakpoints(request)

    # No `b`/breakable command was issued against the still-compiling file.
    assert stub.commands == []
    assert "/local/src/x.pl" in server._pending_breakpoints
    out = _messages(writer)
    resp = [m for m in out if m.get("command") == "setBreakpoints"][0]
    assert resp["success"] is True
    assert resp["body"]["breakpoints"] == [
        {
            "verified": False,
            "line": 10,
            "message": server_mod.DEFERRED_VERIFICATION_MESSAGE,
        }
    ]


async def test_stackTrace_translates_remote_frame_paths_to_local():
    class StubSession:
        def __init__(self) -> None:
            self.stopped = True

        async def helper(self, expr, timeout=20.0):
            return {"frames": [{"sub": "main", "file": "/srv/app/x.pl", "line": 5}]}

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    server.session = StubSession()
    server._path_map = [("/local/src", "/srv/app")]

    request = Request(seq=1, command="stackTrace", arguments={})
    await server._on_stackTrace(request)

    out = _messages(writer)
    resp = [m for m in out if m.get("command") == "stackTrace"][0]
    assert resp["success"] is True
    assert resp["body"]["stackFrames"][0]["source"]["path"] == "/local/src/x.pl"


async def test_resume_rejected_while_classifying():
    class StubSession:
        def __init__(self) -> None:
            self.stopped = True
            self.resume_calls: list[str] = []

        def resume(self, cmd: str) -> None:
            self.resume_calls.append(cmd)

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    stub = StubSession()
    server.session = stub
    server._classifying = True

    request = Request(seq=1, command="continue")
    await server._on_continue(request)

    out = _messages(writer)
    resp = [m for m in out if m.get("command") == "continue"][0]
    assert resp["success"] is False
    assert stub.resume_calls == []


@pytest.mark.parametrize(
    "command,arguments",
    [
        ("setBreakpoints", {"source": {"path": "/x.pl"}, "breakpoints": []}),
        ("stackTrace", {}),
        ("scopes", {"frameId": 0}),
        ("variables", {"variablesReference": 1}),
        ("evaluate", {"expression": "1"}),
        ("source", {"source": {"path": "/x.pl"}}),
    ],
)
async def test_data_requests_rejected_while_classifying(command, arguments):
    class StubSession:
        def __init__(self) -> None:
            self.stopped = True

        async def command(self, text, timeout=20.0):
            raise AssertionError("should not reach the session while classifying")

        async def helper(self, expr, timeout=20.0):
            raise AssertionError("should not reach the session while classifying")

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    server.session = StubSession()
    server._classifying = True

    request = Request(seq=1, command=command, arguments=arguments)
    await server.handlers[command](request)

    out = _messages(writer)
    resp = [m for m in out if m.get("command") == command][0]
    assert resp["success"] is False


async def test_not_ready_reports_no_session():
    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    assert server._not_ready() is not None


async def test_on_unsolicited_stop_keeps_strong_reference_to_classify_task():
    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)

    class StubSession:
        stopped = True
        pid = None  # attach-like: "?" branch must not try to send `q`

        async def helper(self, expr, timeout=20.0):
            return {"file": "?", "line": 1}

    server.session = StubSession()
    server._on_unsolicited_stop()

    assert server._classify_task is not None
    assert not server._classify_task.done()


async def test_ended_location_in_launch_mode_sends_q_and_reports_real_exit_code():
    """launch mode (owned child, `pid` set): the "?" branch must force a
    real exit via `q` and report the resulting code, not hardcode 0."""

    class StubSession:
        stopped = True
        pid = 4242
        eof = True  # `q` genuinely closed the connection

        def __init__(self) -> None:
            self.commands: list[str] = []

        async def helper(self, expr, timeout=20.0):
            return {"file": "?", "line": 0}

        async def command(self, text, timeout=20.0):
            self.commands.append(text)
            raise PerlProtocolError("perl5db connection closed")

        async def wait_exit_code(self) -> int:
            return 7

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    stub = StubSession()
    server.session = stub
    server._on_unsolicited_stop()
    await server._classify_task

    assert stub.commands == ["q"]
    out = _messages(writer)
    exited = [m for m in out if m.get("event") == "exited"][0]
    assert exited["body"]["exitCode"] == 7
    assert any(m.get("event") == "terminated" for m in out)
    # Exactly one terminated/exited pair -- no duplicate from the `q`-
    # triggered EOF this scenario simulates being guarded against.
    assert len([m for m in out if m.get("event") == "terminated"]) == 1
    assert len([m for m in out if m.get("event") == "exited"]) == 1


async def test_ended_location_in_launch_mode_dedupes_against_forwarded_eof():
    """The `q` sent by the "?" branch closes the socket, which in the real
    adapter also drives `_forward_output("__eof__")` via the read loop.
    _eof_terminated must make that a no-op instead of a second
    terminated/exited pair."""

    class StubSession:
        stopped = True
        pid = 4242
        eof = True  # `q` genuinely closed the connection

        async def helper(self, expr, timeout=20.0):
            return {"file": "?", "line": 0}

        async def command(self, text, timeout=20.0):
            raise PerlProtocolError("perl5db connection closed")

        async def wait_exit_code(self) -> int:
            return 3

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    server.session = StubSession()
    server._on_unsolicited_stop()
    await server._classify_task
    # Simulate the read loop observing the socket close the `q` caused.
    server._forward_output("", "__eof__")

    out = _messages(writer)
    assert len([m for m in out if m.get("event") == "terminated"]) == 1
    assert len([m for m in out if m.get("event") == "exited"]) == 1
    assert server._eof_terminated is False  # consumed, ready for next time


async def test_location_error_in_launch_mode_does_not_send_q():
    """Finding 1 (task-4 review): `location()` raising PerlProtocolError
    (loc is None) covers timeouts / malformed JSON / mid-flight EOF -- NOT
    only genuine termination. The debuggee could still be legitimately
    stopped at a real breakpoint or pause, so this must NOT force-quit it
    via `q`. It must still report termination (unchanged pre-existing
    behavior for this branch) with exit code 0 (inconclusive)."""

    class StubSession:
        stopped = True
        pid = 4242
        eof = False

        def __init__(self) -> None:
            self.commands: list[str] = []

        async def helper(self, expr, timeout=20.0):
            raise PerlProtocolError("timed out waiting for prompt")

        async def command(self, text, timeout=20.0):
            self.commands.append(text)
            raise AssertionError("must not send a command when loc is None")

        async def wait_exit_code(self) -> int:
            raise AssertionError("must not be consulted when loc is None")

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    stub = StubSession()
    server.session = stub
    server._on_unsolicited_stop()
    await server._classify_task

    assert stub.commands == [], "loc is None must never trigger `q`"
    out = _messages(writer)
    assert len([m for m in out if m.get("event") == "terminated"]) == 1
    exited = [m for m in out if m.get("event") == "exited"]
    assert len(exited) == 1
    assert exited[0]["body"]["exitCode"] == 0


async def test_q_without_eof_does_not_leave_eof_terminated_dangling():
    """Finding 2 (task-4 review): if `command("q")` doesn't actually close
    the connection (times out waiting for a prompt, or perl5db replies
    without dying -- `session.eof` stays False either way), no `__eof__`
    is coming to consume `_eof_terminated`. Leaving it set would silently
    swallow the NEXT, genuinely unrelated `__eof__` termination (a
    zero-emission failure). It must self-heal so that later `__eof__`
    still emits exactly one terminated/exited pair."""

    class StubSession:
        stopped = True
        pid = 4242
        eof = False  # `q` never actually closed the connection

        async def helper(self, expr, timeout=20.0):
            return {"file": "?", "line": 0}

        async def command(self, text, timeout=20.0):
            raise PerlProtocolError("no prompt after command 'q'")  # timed out, no EOF

        async def wait_exit_code(self) -> int:
            return 0

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    server.session = StubSession()
    server._on_unsolicited_stop()
    await server._classify_task

    assert server._eof_terminated is False, "flag must not dangle after a non-EOF `q`"

    # A later, genuinely unrelated EOF must still be reported -- not
    # silently swallowed by a stale _eof_terminated flag.
    server._forward_output("", "__eof__")
    await server._eof_task

    out = _messages(writer)
    assert len([m for m in out if m.get("event") == "terminated"]) == 2
    assert len([m for m in out if m.get("event") == "exited"]) == 2


async def test_ended_location_in_attach_mode_reports_zero_without_sending_q():
    """attach mode (no owned child, `pid` is None): must never send `q`
    (that would kill the user's live remote process) and always reports 0,
    matching the pre-existing attach contract."""

    class StubSession:
        stopped = True
        pid = None

        def __init__(self) -> None:
            self.commands: list[str] = []

        async def helper(self, expr, timeout=20.0):
            return {"file": "?", "line": 0}

        async def command(self, text, timeout=20.0):
            self.commands.append(text)
            raise AssertionError("attach mode must never send a command here")

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    stub = StubSession()
    server.session = stub
    server._on_unsolicited_stop()
    await server._classify_task

    assert stub.commands == []
    out = _messages(writer)
    exited = [m for m in out if m.get("event") == "exited"][0]
    assert exited["body"]["exitCode"] == 0


async def test_eof_without_prior_q_reports_real_exit_code():
    """A genuinely unsolicited socket close (e.g. the debuggee hard-
    crashed) must still report the child's real exit code, not 0."""

    class StubSession:
        async def wait_exit_code(self) -> int:
            return 42

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    server.session = StubSession()
    server._forward_output("", "__eof__")
    await server._eof_task

    out = _messages(writer)
    exited = [m for m in out if m.get("event") == "exited"][0]
    assert exited["body"]["exitCode"] == 42
    assert any(m.get("event") == "terminated" for m in out)


async def test_pause_does_not_arm_flag_when_stop_wins_race():
    class StubSession:
        def __init__(self) -> None:
            self.stopped = False

        def interrupt(self) -> bool:
            # Simulate a natural stop (e.g. breakpoint) landing during the
            # signal window between our not-stopped check and this call
            # returning.
            self.stopped = True
            return True

    reader = asyncio.StreamReader()
    writer = SinkWriter()
    server = PerlDapServer(reader, writer)
    stub = StubSession()
    server.session = stub

    request = Request(seq=1, command="pause")
    await server._on_pause(request)

    out = _messages(writer)
    resp = [m for m in out if m.get("command") == "pause"][0]
    assert resp["success"] is True
    assert server._pause_pending is False
