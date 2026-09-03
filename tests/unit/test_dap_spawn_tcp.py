"""tests/unit/test_dap_spawn_tcp.py"""

import re
import sys
import textwrap

import pytest

from tdb.dap.client import DAPClient
from tdb.languages.base import AdapterSpec

# Serves one TCP connection and speaks just enough DAP to answer an
# `initialize` request, so the client's read loop has a real stream.
_SERVER_SCRIPT = textwrap.dedent(
    """
    import socket, sys
    srv = socket.create_server(("127.0.0.1", 0))
    print(f"DAP server listening at: 127.0.0.1:{srv.getsockname()[1]}", flush=True)
    conn, _ = srv.accept()
    conn.recv(65536)  # swallow the initialize request
    body = b'{"seq":1,"type":"response","request_seq":1,"command":"initialize","success":true,"body":{}}'
    conn.sendall(b"Content-Length: %d\\r\\n\\r\\n%s" % (len(body), body))
    conn.recv(65536)  # hold the connection until the client closes
    """
)


class _FakeTcpAdapter(AdapterSpec):
    id = "fake-tcp"
    connect_mode = "spawn_tcp"
    listen_regex = re.compile(r"DAP server listening at: (\S+):(\d+)")

    def __init__(self, script: str) -> None:
        self._script = script

    def command(self) -> list[str]:
        return [sys.executable, "-c", self._script]


@pytest.mark.asyncio
async def test_spawn_tcp_connects_and_talks():
    client = DAPClient(_FakeTcpAdapter(_SERVER_SCRIPT))
    await client.start()
    try:
        caps = await client.initialize()
        assert caps is not None
        assert client._writer is not None  # TCP stream, not stdin
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_spawn_tcp_times_out_without_listen_line(monkeypatch):
    monkeypatch.setattr("tdb._timeouts.ADAPTER_LISTEN", 0.5)
    silent = "import time; time.sleep(30)"
    client = DAPClient(_FakeTcpAdapter(silent))
    with pytest.raises(ConnectionError):
        await client.start()
    await client.stop()


@pytest.mark.asyncio
async def test_spawn_tcp_surfaces_stderr_on_early_death():
    dying = "import sys; sys.stderr.write('bad flag'); sys.exit(3)"
    client = DAPClient(_FakeTcpAdapter(dying))
    with pytest.raises(ConnectionError) as exc:
        await client.start()
    assert "bad flag" in str(exc.value)
    await client.stop()


def test_default_connect_mode_is_stdio():
    assert AdapterSpec.connect_mode == "stdio"
    assert AdapterSpec.listen_regex is None
