"""externalTerminal launch: proxy sends runInTerminal with the full rdbg
argv; the 'terminal' (this test) spawns it; session proceeds normally."""

import asyncio

import pytest

from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    rdbg_ok,
)
from tests.integration.perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")


async def test_external_terminal_handshake():
    client = AdapterClient()
    spawned: list[asyncio.subprocess.Process] = []

    async def fake_terminal(req):
        args = req["arguments"]
        assert args["kind"] == "external"
        assert "rdbg" in args["args"][0]
        assert "--open" in args["args"]
        proc = await asyncio.create_subprocess_exec(*args["args"], cwd=args["cwd"])
        spawned.append(proc)
        return {}

    client.on_reverse_request = fake_terminal
    await client.start(module="tdb.adapters.ruby")
    try:
        await client.request(
            "initialize",
            {"adapterID": "rdbg", "supportsRunInTerminalRequest": True},
        )
        program = str(FIXTURES / "ruby_hello.rb")
        launch_fut = client.send(
            "launch",
            {
                "type": "ruby",
                "request": "launch",
                "program": program,
                "args": [],
                "cwd": str(FIXTURES),
                "stopOnEntry": True,
                "console": "externalTerminal",
            },
        )
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await launch_fut
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        await client.request("continue", {"threadId": 1})
        await client.wait_event("terminated")
    finally:
        for proc in spawned:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        await client.stop()


async def test_external_terminal_requires_client_support():
    client = AdapterClient()
    await client.start(module="tdb.adapters.ruby")
    try:
        await client.request(
            "initialize",
            {"adapterID": "rdbg", "supportsRunInTerminalRequest": False},
        )
        resp = await client.send(
            "launch",
            {
                "type": "ruby",
                "program": str(FIXTURES / "ruby_hello.rb"),
                "args": [],
                "cwd": str(FIXTURES),
                "stopOnEntry": True,
                "console": "externalTerminal",
            },
        )
        assert resp["success"] is False
        assert "runInTerminal" in resp["message"]
    finally:
        await client.stop()
