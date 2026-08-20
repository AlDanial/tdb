"""Unit tests for the Ruby stdio–TCP DAP bridge (`tdb.adapters.ruby.server`)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from tdb.adapters.ruby.server import RubyDapBridge, _read_remote_message
from tdb.dap.protocol import encode_message


class SinkWriter:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def _messages(writer: SinkWriter) -> list[dict]:
    blob = b"".join(writer.chunks)
    out = []
    while blob:
        header, _, rest = blob.partition(b"\r\n\r\n")
        length = int(header.split(b":")[1])
        out.append(json.loads(rest[:length]))
        blob = rest[length:]
    return out


def _fake_remote(
    initialize_response: dict | None = None,
) -> tuple[asyncio.StreamReader, SinkWriter]:
    """Return a (reader, writer) pair that impersonates an rdbg DAP server."""
    reader = asyncio.StreamReader()
    if initialize_response is not None:
        reader.feed_data(encode_message(initialize_response))
    reader.feed_eof()
    return reader, SinkWriter()


class _FakeProcess:
    def __init__(self, exit_code: int = 0) -> None:
        self.returncode: int | None = None
        self._exit_code = exit_code
        reader = asyncio.StreamReader()
        reader.feed_eof()
        self.stdout = reader
        self.stderr = reader

    def terminate(self) -> None:
        return None

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code


def _command_from(writer: SinkWriter, command: str) -> dict:
    """Return the last request/response in ``writer`` with the given command."""
    for msg in reversed(_messages(writer)):
        if msg.get("command") == command:
            return msg
    raise AssertionError(f"no {command!r} message captured")


@pytest.mark.parametrize("stop_on_entry", [True, False])
async def test_launch_never_passes_nonstop_flag(monkeypatch, stop_on_entry) -> None:
    """`--nonstop` makes rdbg run to completion before tdb can attach.

    Stop-on-entry must instead be controlled through the DAP attach
    request's `nonstop` argument.  The bridge must never put `--nonstop`
    on the rdbg command line.
    """
    monkeypatch.setattr(shutil, "which", lambda _: "rdbg")
    commands: list[tuple] = []

    async def fake_spawn(*args, **kwargs):
        commands.append(args)
        return _FakeProcess()

    async def fake_connect(self, host, port) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(RubyDapBridge, "_connect", fake_connect)

    bridge = RubyDapBridge(None)
    await bridge._launch_rdbg(
        {
            "program": "/tmp/hello.rb",
            "args": [],
            "cwd": "/tmp",
            "stopOnEntry": stop_on_entry,
        },
        SinkWriter(),
    )
    assert commands, "rdbg was never spawned"
    assert "--nonstop" not in commands[0]


def _spawn_args(monkeypatch, arguments: dict) -> tuple[list[tuple], dict]:
    """Run `_launch_rdbg` with a fake subprocess; return (commands, spawn_kwargs)."""
    monkeypatch.setattr(shutil, "which", lambda _: "rdbg")
    commands: list[tuple] = []
    spawn_kwargs: dict = {}

    async def fake_spawn(*args, **kwargs):
        commands.append(args)
        spawn_kwargs.update(kwargs)
        return _FakeProcess()

    async def fake_connect(self, host, port) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(RubyDapBridge, "_connect", fake_connect)

    async def _go() -> tuple[list[tuple], dict]:
        bridge = RubyDapBridge(None)
        await bridge._launch_rdbg(arguments, SinkWriter())
        return commands, spawn_kwargs

    import asyncio as _asyncio

    return _asyncio.run(_go())


@pytest.mark.parametrize(
    "bad_arguments",
    [
        {},  # no program key at all
        {"program": ""},  # empty program
        {"program": 123},  # non-string program
        {"program": "/tmp/hello.rb", "args": [1]},  # non-string arg
        {"program": "/tmp/hello.rb", "args": "nope"},  # args not a list
        {"program": "/tmp/hello.rb", "debugPort": "abc"},  # non-int port
        {"program": "/tmp/hello.rb", "debugPort": 65536},  # port too high
    ],
)
def test_launch_validates_arguments(monkeypatch, bad_arguments) -> None:
    """Malformed launch arguments must raise ValueError, not spawn rdbg."""
    monkeypatch.setattr(shutil, "which", lambda _: "rdbg")

    async def fake_spawn(*args, **kwargs):
        raise AssertionError("rdbg must not be spawned for invalid arguments")

    async def fake_connect(self, host, port) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(RubyDapBridge, "_connect", fake_connect)

    async def _go() -> None:
        bridge = RubyDapBridge(None)
        with pytest.raises(ValueError):
            await bridge._launch_rdbg(bad_arguments, SinkWriter())

    import asyncio as _asyncio

    _asyncio.run(_go())


def test_launch_raises_when_rdbg_missing(monkeypatch) -> None:
    """A missing rdbg executable must surface as FileNotFoundError."""
    monkeypatch.setattr(shutil, "which", lambda _: None)

    async def fake_spawn(*args, **kwargs):
        raise AssertionError("rdbg must not be spawned when executable missing")

    async def fake_connect(self, host, port) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(RubyDapBridge, "_connect", fake_connect)

    async def _go() -> None:
        bridge = RubyDapBridge(None)
        with pytest.raises(FileNotFoundError):
            await bridge._launch_rdbg({"program": "/tmp/hello.rb"}, SinkWriter())

    import asyncio as _asyncio

    _asyncio.run(_go())


def test_launch_command_plain(monkeypatch) -> None:
    """A plain (non-bundler) launch builds `rdbg --open --port N script args`."""
    commands, spawn_kwargs = _spawn_args(
        monkeypatch,
        {
            "program": "/tmp/hello.rb",
            "args": ["--verbose", "3"],
            "cwd": "/tmp",
        },
    )
    assert commands, "rdbg was never spawned"
    command = list(commands[0])
    assert command[0] == "rdbg"
    assert command[1:3] == ["--open", "--port"]
    assert command[3].isdigit()  # auto-allocated port
    assert command[4] == "/tmp/hello.rb"
    # args come after the script, no bundler wrapping
    assert "bundle" not in command
    assert command[5:] == ["--verbose", "3"]


def test_launch_command_with_bundler(monkeypatch) -> None:
    """`useBundler: True` wraps the command as `bundle exec ruby script args`."""
    commands, _ = _spawn_args(
        monkeypatch,
        {
            "program": "/tmp/app.rb",
            "args": ["server"],
            "useBundler": True,
        },
    )
    command = list(commands[0])
    # rdbg --open --port N -c -- bundle exec ruby /tmp/app.rb server
    assert command[0] == "rdbg"
    assert command[1:3] == ["--open", "--port"]
    assert command[3].isdigit()  # auto-allocated port
    assert command[4:6] == ["-c", "--"]
    assert command[6:10] == ["bundle", "exec", "ruby", "/tmp/app.rb"]
    assert command[10:] == ["server"]


def test_launch_merges_env(monkeypatch) -> None:
    """The `env` dict is merged onto the child environment."""
    _, spawn_kwargs = _spawn_args(
        monkeypatch,
        {
            "program": "/tmp/hello.rb",
            "args": [],
            "env": {"RUBY_DEBUG_TEST": "1", "GEM_HOME": "/custom/gems"},
        },
    )
    child_env = spawn_kwargs["env"]
    assert child_env["RUBY_DEBUG_TEST"] == "1"
    assert child_env["GEM_HOME"] == "/custom/gems"
    # The parent environment is inherited as a base.
    assert "PATH" in child_env


def test_launch_injects_rubyopt_stdout_sync(monkeypatch) -> None:
    """The child env must set RUBYOPT to require the stdout-sync helper.

    Ruby's STDOUT is fully buffered on a pipe, so `puts` output would only
    arrive at process exit.  The bridge injects `-r <helper>` via RUBYOPT
    to stream program output live.
    """
    _, spawn_kwargs = _spawn_args(
        monkeypatch,
        {
            "program": "/tmp/hello.rb",
            "args": [],
        },
    )
    rubyopt = spawn_kwargs["env"]["RUBYOPT"]
    assert rubyopt.endswith("-r" + str(Path(__file__).resolve().parent.parent.parent / "src" / "tdb" / "adapters" / "ruby" / "stdout_sync.rb"))


def test_launch_keeps_existing_rubyopt(monkeypatch) -> None:
    """A user-provided RUBYOPT is preserved and the sync helper appended."""
    _, spawn_kwargs = _spawn_args(
        monkeypatch,
        {
            "program": "/tmp/hello.rb",
            "args": [],
            "env": {"RUBYOPT": "-w -I/tmp/lib"},
        },
    )
    rubyopt = spawn_kwargs["env"]["RUBYOPT"]
    assert rubyopt.startswith("-w -I/tmp/lib")
    assert "stdout_sync.rb" in rubyopt


@pytest.mark.parametrize("exit_code", [0, 3])
async def test_bridge_synthesizes_exited_event(exit_code) -> None:
    """rdbg sends no `exited` event; the bridge synthesizes one from the
    debuggee process's real return code so tdb can record it."""
    bridge = RubyDapBridge(None)
    bridge._ruby_process = _FakeProcess(exit_code=exit_code)
    writer = SinkWriter()
    await bridge._watch_exit(writer)

    exited = [m for m in _messages(writer) if m.get("event") == "exited"]
    assert len(exited) == 1
    assert exited[0]["body"]["exitCode"] == exit_code


@pytest.mark.parametrize(
    ("stop_on_entry", "expected_nonstop"),
    [(True, False), (False, True), (None, False)],
)
async def test_attach_request_carries_nonstop_from_stop_on_entry(
    monkeypatch, stop_on_entry, expected_nonstop
) -> None:
    """The DAP attach sent to rdbg mirrors the launch body's stopOnEntry.

    rdbg's `attach` `nonstop` argument (default false) decides whether the
    debuggee stops at entry once attached: false → `stopped` event on
    configurationDone, true → the program keeps running.
    """
    launch_args: dict = {
        "type": "rdbg",
        "request": "launch",
        "program": "/tmp/hello.rb",
        "args": [],
        "cwd": "/tmp",
        "console": "internalConsole",
    }
    if stop_on_entry is not None:
        launch_args["stopOnEntry"] = stop_on_entry

    remote_reader, remote_writer = _fake_remote(
        initialize_response={
            "seq": 1,
            "type": "response",
            "request_seq": 1,
            "command": "initialize",
            "success": True,
        }
    )

    async def fake_launch(self, arguments, client_writer) -> None:
        self._remote_reader = remote_reader
        self._remote_writer = remote_writer
        self._ruby_process = _FakeProcess()

    monkeypatch.setattr(RubyDapBridge, "_launch_rdbg", fake_launch)

    client_reader = asyncio.StreamReader()
    client_reader.feed_data(
        encode_message(
            {
                "seq": 1,
                "type": "request",
                "command": "initialize",
                "arguments": {"adapterID": "rdbg"},
            }
        )
    )
    client_reader.feed_data(
        encode_message({"seq": 2, "type": "request", "command": "launch", "arguments": launch_args})
    )
    client_reader.feed_eof()
    client_writer = SinkWriter()

    bridge = RubyDapBridge(None)
    await asyncio.wait_for(bridge.run(reader=client_reader, writer=client_writer), timeout=5)

    attach = _command_from(remote_writer, "attach")
    assert attach["command"] == "attach"
    assert attach["arguments"]["localfs"] is True
    assert attach["arguments"]["nonstop"] is expected_nonstop


def _frame(message: dict) -> bytes:
    """Encode ``message`` as a DAP Content-Length-framed payload."""
    return encode_message(message)


def _remote_stream(chunks: list[bytes]) -> asyncio.StreamReader:
    """Feed raw bytes into a StreamReader impersonating the rdbg DAP socket."""
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


async def test_read_remote_message_skips_leading_console_lines() -> None:
    """`out ...` / `input ...` console lines before a frame must be skipped."""
    frame = _frame({"type": "event", "event": "stopped", "body": {}})
    reader = _remote_stream([b"out foo\n", b"input bar\n", frame])
    assert await _read_remote_message(reader) == {
        "type": "event",
        "event": "stopped",
        "body": {},
    }


async def test_read_remote_message_skips_console_lines_between_frames() -> None:
    """Console lines interleaved between two frames must not corrupt parsing."""
    frame = _frame({"type": "event", "event": "stopped", "body": {}})
    reader = _remote_stream([b"out foo\n", frame, b"input bar\n", frame])
    assert await _read_remote_message(reader) == {
        "type": "event",
        "event": "stopped",
        "body": {},
    }
    assert await _read_remote_message(reader) == {
        "type": "event",
        "event": "stopped",
        "body": {},
    }


async def test_read_remote_message_reads_consecutive_frames() -> None:
    """Back-to-back frames with no interleaved console lines parse normally."""
    first = {"type": "event", "event": "stopped", "body": {"threadId": 1}}
    second = {"type": "event", "event": "exited", "body": {"exitCode": 0}}
    reader = _remote_stream([_frame(first), _frame(second)])
    assert await _read_remote_message(reader) == first
    assert await _read_remote_message(reader) == second


async def test_read_remote_message_resyncs_after_malformed_frame() -> None:
    """A bad Content-Length separator must not derail parsing of the next frame."""
    frame = _frame({"type": "event", "event": "stopped", "body": {}})
    # 'Content-Length: 999' followed by a non-\r\n separator, then garbage,
    # then a well-formed frame. The parser must resync and find the frame.
    reader = _remote_stream([b"Content-Length: 999XX\njunk\n", frame])
    assert await _read_remote_message(reader) == {
        "type": "event",
        "event": "stopped",
        "body": {},
    }


async def test_read_remote_message_raises_on_closed_stream() -> None:
    """EOF with no Content-Length header surfaces as ConnectionError."""
    reader = _remote_stream([b"out foo\n"])
    with pytest.raises(ConnectionError):
        await _read_remote_message(reader)


# --- _forward_stream / _emit_output ------------------------------------------


def _output_events(writer: SinkWriter) -> list[dict]:
    return [m for m in _messages(writer) if m.get("event") == "output"]


async def test_forward_stream_emits_one_event_per_line() -> None:
    """Chunks are split on newlines; each complete line becomes an output event."""
    bridge = RubyDapBridge(None)
    reader = asyncio.StreamReader()
    reader.feed_data(b"line one\nline two\n")
    reader.feed_eof()
    writer = SinkWriter()
    await bridge._forward_stream(reader, "stdout", writer)

    events = _output_events(writer)
    assert [e["body"]["category"] for e in events] == ["stdout", "stdout"]
    assert [e["body"]["output"] for e in events] == ["line one\n", "line two\n"]


async def test_forward_stream_buffers_across_chunk_boundaries() -> None:
    """A line split across reads must be reassembled before emitting."""
    bridge = RubyDapBridge(None)
    reader = asyncio.StreamReader()
    reader.feed_data(b"part1-")
    reader.feed_data(b"part2\nnext\n")
    reader.feed_eof()
    writer = SinkWriter()
    await bridge._forward_stream(reader, "stderr", writer)

    events = _output_events(writer)
    assert [e["body"]["output"] for e in events] == ["part1-part2\n", "next\n"]


async def test_forward_stream_flushes_partial_line_at_eof() -> None:
    """A trailing line without a newline is flushed when the pipe closes."""
    bridge = RubyDapBridge(None)
    reader = asyncio.StreamReader()
    reader.feed_data(b"complete\npartial-no-newline")
    reader.feed_eof()
    writer = SinkWriter()
    await bridge._forward_stream(reader, "stdout", writer)

    events = _output_events(writer)
    assert [e["body"]["output"] for e in events] == ["complete\n", "partial-no-newline"]


async def test_emit_output_increments_event_seq_and_decodes() -> None:
    """_emit_output assigns an incrementing seq and UTF-8-decodes with
    invalid-byte replacement."""
    bridge = RubyDapBridge(None)
    writer = SinkWriter()
    await bridge._emit_output(writer, "stdout", "héllo\n".encode("utf-8"))
    await bridge._emit_output(writer, "stderr", b"\xff\xfe broken")

    events = _output_events(writer)
    assert [e["seq"] for e in events] == [1, 2]
    assert events[0]["body"]["category"] == "stdout"
    assert events[0]["body"]["output"] == "héllo\n"
    # Invalid UTF-8 bytes become U+FFFD replacement characters.
    assert events[1]["body"]["category"] == "stderr"
    assert events[1]["body"]["output"].startswith("\ufffd\ufffd broken")


# --- run() lifecycle / error responses ---------------------------------------


def _client_reader(messages: list[dict]) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for msg in messages:
        reader.feed_data(encode_message(msg))
    reader.feed_eof()
    return reader


async def test_run_rejects_requests_before_session_started() -> None:
    """A command sent before launch/attach is rejected with a clear message."""
    client_reader = _client_reader(
        [
            {"seq": 1, "type": "request", "command": "initialize", "arguments": {}},
            {"seq": 2, "type": "request", "command": "threads", "arguments": {}},
        ]
    )
    client_writer = SinkWriter()
    bridge = RubyDapBridge(None)
    await asyncio.wait_for(bridge.run(reader=client_reader, writer=client_writer), timeout=5)

    threads_response = _command_from(client_writer, "threads")
    assert threads_response["success"] is False
    assert threads_response["message"] == "Ruby session has not been launched"


async def test_run_reports_launch_failure_as_error_response(monkeypatch) -> None:
    """A launch that raises (e.g. missing program) surfaces as success: False."""
    client_reader = _client_reader(
        [
            {"seq": 1, "type": "request", "command": "initialize", "arguments": {}},
            # No program key → _launch_rdbg raises ValueError.
            {"seq": 2, "type": "request", "command": "launch", "arguments": {}},
        ]
    )
    client_writer = SinkWriter()
    bridge = RubyDapBridge(None)
    await asyncio.wait_for(bridge.run(reader=client_reader, writer=client_writer), timeout=5)

    launch_response = _command_from(client_writer, "launch")
    assert launch_response["success"] is False
    assert "program path" in launch_response["message"]