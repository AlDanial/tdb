"""Terminal-mode (runInTerminal) launches of the perl adapter.

The test IS the DAP client: it receives the adapter's runInTerminal
reverse request and spawns the command itself — no emulator needed.
"""

import asyncio
import shutil
import subprocess

import pytest

from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)


class TerminalSpawner:
    def __init__(self):
        self.requests: list[dict] = []
        self.proc = None

    async def __call__(self, body: dict) -> dict:
        self.requests.append(body)
        args = body["arguments"]
        self.proc = await asyncio.create_subprocess_exec(
            *args["args"],
            cwd=args.get("cwd"),
            env=args.get("env") or None,
            stdin=asyncio.subprocess.DEVNULL,
        )
        return {}


@pytest.fixture
async def client():
    c = AdapterClient()
    await c.start()
    yield c
    await c.stop()


async def test_terminal_launch_steps_and_reports_exit_code(client, tmp_path):
    script = tmp_path / "t.pl"
    script.write_text("my $x = 1;\nmy $y = 2;\nexit 7;\n")
    spawner = TerminalSpawner()
    client.on_reverse_request = spawner
    await client.request(
        "initialize",
        {"adapterID": "perl-tdb", "supportsRunInTerminalRequest": True},
    )
    launch_fut = client.send(
        "launch",
        {
            "program": str(script),
            "args": [],
            "cwd": str(tmp_path),
            "stopOnEntry": True,
            "console": "externalTerminal",
        },
    )
    await client.wait_event("initialized")
    await client.request("configurationDone")
    assert (await asyncio.wait_for(launch_fut, 30))["success"] is True
    assert spawner.requests[0]["command"] == "runInTerminal"
    assert spawner.requests[0]["arguments"]["kind"] == "external"
    await client.wait_event("stopped")
    await client.request("next")
    await client.wait_event("stopped")
    await client.request("continue")
    exited = await client.wait_event("exited")
    assert exited["body"]["exitCode"] == 7
    await client.wait_event("terminated")


async def test_terminal_launch_without_capability_fails(client, tmp_path):
    script = tmp_path / "t.pl"
    script.write_text("my $x = 1;\n")
    await client.request("initialize", {"adapterID": "perl-tdb"})
    resp = await client.request(
        "launch",
        {
            "program": str(script),
            "cwd": str(tmp_path),
            "console": "externalTerminal",
        },
    )
    assert resp["success"] is False
    assert "runInTerminal" in resp["message"]
