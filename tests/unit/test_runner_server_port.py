"""`tdb --headless` claims its RPC port before it starts the session.

Starting a session can take seconds (a pid attach injects into the
target), and both tdb and the debug adapter open ephemeral ports of
their own meanwhile. A port that is only bound afterwards can be lost
in that window, and is then lost with the target already injected.
"""

from __future__ import annotations

import socket

import pytest

from tdb.server import runner
from tdb.server.runner import run_headless


async def test_busy_port_fails_before_the_session_starts(monkeypatch, capsys):
    started = []

    async def setup(*args, **kwargs):
        started.append(True)
        raise AssertionError("session started with the port unavailable")

    monkeypatch.setattr(runner, "setup_headless_session", setup)
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        with pytest.raises(SystemExit) as exc_info:
            await run_headless(program="prog", port=port)
    assert exc_info.value.code == 2
    assert not started
    assert f"tdb: cannot listen on 127.0.0.1:{port}" in capsys.readouterr().err


async def test_port_is_held_while_the_session_starts(monkeypatch):
    class Stop(Exception):
        pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    async def setup(*args, **kwargs):
        # Nobody else can take the port during session setup.
        with pytest.raises(OSError):
            socket.create_server(("127.0.0.1", port))
        raise Stop

    monkeypatch.setattr(runner, "setup_headless_session", setup)
    with pytest.raises(Stop):
        await run_headless(program="prog", port=port)
    # ...and a failed setup lets go of it.
    socket.create_server(("127.0.0.1", port)).close()
