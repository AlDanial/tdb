"""Remote attach is DIRECT: tdb TCP-connects to a user-started
`rdbg --open --port N`; no proxy involved. This test plays tdb's part
with a raw DAP-over-TCP client using the exact attach body
RdbgAdapter.attach_body produces."""

import asyncio
import json

import pytest

from tdb.languages.ruby import RdbgAdapter
from tests.integration.ruby_adapter_harness import FIXTURES, rdbg_ok
from tdb.adapters.ruby.server import _free_port

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")


class TcpDap:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.seq = 0
        self.events = []

    def send(self, command, arguments=None):
        self.seq += 1
        body = json.dumps(
            {
                "seq": self.seq,
                "type": "request",
                "command": command,
                "arguments": arguments or {},
            }
        ).encode()
        self.writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

    async def recv(self):
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            header += await self.reader.readexactly(1)
        length = int(header.split(b":")[1])
        return json.loads(await self.reader.readexactly(length))

    async def wait(self, *, event=None, command=None, timeout=15.0):
        async def _loop():
            while True:
                m = await self.recv()
                if event and m.get("type") == "event" and m["event"] == event:
                    return m
                if command and m.get("type") == "response" and m["command"] == command:
                    return m

        return await asyncio.wait_for(_loop(), timeout)


async def test_direct_tcp_attach_stop_inspect_continue(tmp_path):
    port = _free_port()
    rdbg = await asyncio.create_subprocess_exec(
        "rdbg",
        "--open",
        "--port",
        str(port),
        "--host",
        "127.0.0.1",
        str(FIXTURES / "ruby_sleep.rb"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):  # rdbg needs a moment to listen
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            pytest.fail("could not connect to rdbg")
        dap = TcpDap(reader, writer)
        dap.send("initialize", {"adapterID": "rdbg"})
        await dap.wait(command="initialize")
        dap.send(
            "attach", RdbgAdapter().attach_body(host="127.0.0.1", port=port, opts={})
        )
        await dap.wait(command="attach")
        dap.send("configurationDone")
        # non-nonstop attach: rdbg stops the waiting debuggee right after
        # configurationDone (stopped reason "pause")
        stopped = await dap.wait(event="stopped")
        assert stopped["body"]["reason"] == "pause"
        dap.send("stackTrace", {"threadId": stopped["body"].get("threadId", 1)})
        st = await dap.wait(command="stackTrace")
        assert st["body"]["stackFrames"], "expected a live stack"
        dap.send("continue", {"threadId": 1})
        await dap.wait(command="continue")
        writer.close()
    finally:
        if rdbg.returncode is None:
            rdbg.kill()
            await rdbg.wait()
