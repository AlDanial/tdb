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
