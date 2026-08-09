"""DAP-level: launch, stopOnEntry, output, exit code."""

import pytest

from tests.integration.bash_adapter_harness import (
    FIXTURES,
    bash_ok,
    launch_stopped,
    start_bash_adapter,
)

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")


@pytest.mark.asyncio
async def test_launch_stop_on_entry_continue_exit():
    client = await start_bash_adapter()
    try:
        await launch_stopped(client, str(FIXTURES / "bash_hello.sh"))
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        await client.request("continue", {"threadId": 1})
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_output_events_carry_stdout():
    client = await start_bash_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "bash_hello.sh"), stop_on_entry=False
        )
        await client.wait_event("exited")
        text = "".join(
            e["body"].get("output", "")
            for e in list(client.events)
            if e["event"] == "output"
        )
        assert "hello from bash" in text
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_launch_missing_program_fails():
    client = await start_bash_adapter()
    try:
        fut = client.send(
            "launch",
            {
                "type": "bash",
                "program": "/nonexistent/x.sh",
                "args": [],
                "cwd": "/tmp",
                "stopOnEntry": True,
            },
        )
        resp = await fut
        assert resp["success"] is False
        assert "not found" in resp["message"]
    finally:
        await client.stop()
