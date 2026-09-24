"""Proxy-mode launches wait for rdbg to be *ready* before connecting.

Debug gem 1.11's UI_ServerBase#sock reads @sock without the accept
mutex, so a client that connects before the debuggee's session thread
has reached its first line can be handed REPL-mode traffic on the DAP
socket (or nothing at all). rdbg prints "wait for debugger connection..."
on stderr exactly when that session thread has parked itself on the
mutex-protected slow path — the ordering that makes the handshake
safe. In proxy mode the adapter owns rdbg's stderr, so it gates the
connect on that line (bounded, with the old connect-on-socket behavior
as the fallback).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from tdb.adapters.ruby import server as ruby_server

from tests.unit.test_ruby_proxy_units import _messages, _server

MARKER = "DEBUGGER: wait for debugger connection...\n"


def _stderr(*lines: str, eof: bool = False) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line.encode())
    if eof:
        reader.feed_eof()
    return reader


# ---- _wait_for_rdbg_ready -------------------------------------------------


async def test_ready_gate_returns_at_marker_and_leaves_the_rest_for_the_pump():
    server = _server()
    stderr = _stderr(
        "DEBUGGER: Debugger can attach via UNIX domain socket (/tmp/x/s)\n",
        MARKER,
        "later line\n",
    )
    lines = await server._wait_for_rdbg_ready(stderr, timeout=5.0)
    assert lines == []
    assert await stderr.readline() == b"later line\n"


async def test_ready_gate_collects_non_banner_lines_seen_before_the_marker():
    server = _server()
    stderr = _stderr("warning: something odd\n", MARKER)
    lines = await server._wait_for_rdbg_ready(stderr, timeout=5.0)
    assert lines == ["warning: something odd\n"]


async def test_ready_gate_returns_on_eof():
    server = _server()
    stderr = _stderr("ruby: cannot load such file -- nope\n", eof=True)
    lines = await server._wait_for_rdbg_ready(stderr, timeout=5.0)
    assert lines == ["ruby: cannot load such file -- nope\n"]


async def test_ready_gate_falls_back_after_timeout_without_marker():
    server = _server()
    stderr = _stderr(
        "DEBUGGER: Debugger can attach via UNIX domain socket (/tmp/x/s)\n"
    )
    t0 = time.monotonic()
    lines = await server._wait_for_rdbg_ready(stderr, timeout=0.1)
    assert lines == []
    assert time.monotonic() - t0 < 2.0


async def test_ready_gate_does_not_split_a_partial_line_on_timeout():
    # A line still being written when the timeout fires must stay in the
    # reader intact so the stderr pump forwards it whole later.
    server = _server()
    stderr = _stderr("partial")
    await server._wait_for_rdbg_ready(stderr, timeout=0.1)
    stderr.feed_data(b" rest\n")
    assert await stderr.readline() == b"partial rest\n"


# ---- _finish_launch wiring -----------------------------------------------


class _FakeProc:
    def __init__(self, stderr: asyncio.StreamReader) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr = stderr
        self.returncode: int | None = None
        self.pid = 4242

    async def wait(self) -> int:
        await asyncio.Event().wait()  # never exits on its own
        return 0


def _launch_request() -> dict:
    return {
        "seq": 7,
        "type": "request",
        "command": "launch",
        "arguments": {"program": "/tmp/app.rb", "stopOnEntry": True},
    }


@pytest.fixture
async def launch_env(monkeypatch):
    """Fake out the subprocess spawn and rdbg-death path so
    `_finish_launch` can run without rdbg installed. Async so the
    stream readers belong to the test's event loop."""
    server = _server()
    order: list[str] = []
    stderr = _stderr("warning: pre-connect noise\n", MARKER, eof=True)
    proc = _FakeProc(stderr)

    async def fake_exec(*cmd, **kwargs):
        order.append("spawn")
        return proc

    async def fake_dead():
        pass

    monkeypatch.setattr(ruby_server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server, "_ensure_rdbg_dead", fake_dead)
    return server, order, stderr


async def test_finish_launch_waits_for_ready_before_connecting(launch_env, monkeypatch):
    server, order, stderr = launch_env

    unread_at_connect: list[bytes] = []

    async def fake_connect():
        order.append("connect")
        unread_at_connect.append(bytes(stderr._buffer))
        raise RuntimeError("stop here")

    monkeypatch.setattr(server, "_connect_with_retry", fake_connect)
    await server._finish_launch(
        _launch_request(), "rdbg", "/tmp/app.rb", {}, terminal=False
    )
    assert order == ["spawn", "connect"]
    # The gate must already have consumed the marker line by then.
    assert unread_at_connect == [b""]


async def test_finish_launch_error_detail_includes_pre_connect_stderr(
    launch_env, monkeypatch
):
    server, order, stderr = launch_env

    async def fake_connect():
        raise RuntimeError("rdbg exited with code 1 before accepting a connection")

    monkeypatch.setattr(server, "_connect_with_retry", fake_connect)
    await server._finish_launch(
        _launch_request(), "rdbg", "/tmp/app.rb", {}, terminal=False
    )
    (resp,) = [m for m in _messages(server._writer) if m["type"] == "response"]
    assert resp["success"] is False
    assert "rdbg exited with code 1" in resp["message"]
    assert "warning: pre-connect noise" in resp["message"]


async def test_finish_launch_forwards_pre_connect_stderr_as_output_on_success(
    launch_env, monkeypatch
):
    server, order, stderr = launch_env
    rdbg_reader = asyncio.StreamReader()  # never yields: pump just waits

    class _W:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

    async def fake_connect():
        return rdbg_reader, _W()

    async def fake_handshake(event, pump, timeout=None):
        return True

    monkeypatch.setattr(server, "_connect_with_retry", fake_connect)
    monkeypatch.setattr(server, "_await_handshake", fake_handshake)
    try:
        await server._finish_launch(
            _launch_request(), "rdbg", "/tmp/app.rb", {}, terminal=False
        )
        outputs = [
            m["body"]
            for m in _messages(server._writer)
            if m["type"] == "event" and m["event"] == "output"
        ]
        assert {
            "category": "stderr",
            "output": "warning: pre-connect noise\n",
        } in outputs
    finally:
        for task in list(server._tasks):
            task.cancel()
        await asyncio.gather(*server._tasks, return_exceptions=True)


def test_ready_marker_matches_what_rdbg_prints():
    # Pinned to the debug gem's exact wording (server.rb, UI_ServerBase#sock).
    assert ruby_server._RDBG_READY_MARKER == "wait for debugger connection..."
    assert json.dumps(ruby_server._RDBG_READY_TIMEOUT)  # a plain number
