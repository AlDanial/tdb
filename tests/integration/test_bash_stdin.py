"""DAP-level: the tdb-private `tdbStdin` request feeds the debuggee's
stdin pipe (Console-view input -> bash `read`)."""

import asyncio

import pytest

from tests.integration.bash_adapter_harness import (
    FIXTURES,
    bash_ok,
    launch_stopped,
    start_bash_adapter,
)

pytestmark = pytest.mark.skipif(not bash_ok(), reason="needs bash >= 4.4")

PROGRAM = str(FIXTURES / "bash_read_stdin.sh")


def _output(client) -> str:
    return "".join(
        e["body"].get("output", "")
        for e in list(client.events)
        if e["event"] == "output"
    )


async def wait_output(client, needle: str, timeout: float = 30.0) -> None:
    for _ in range(int(timeout * 10)):
        if needle in _output(client):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{needle!r} never appeared in output: {_output(client)!r}")


@pytest.mark.asyncio
async def test_text_reaches_read_builtin():
    client = await start_bash_adapter()
    try:
        await launch_stopped(client, PROGRAM, stop_on_entry=False)
        # the prompt has no trailing newline: the pump must deliver
        # partial chunks or the script blocks in `read` before we ever
        # see "enter: "
        await wait_output(client, "enter: ")
        assert "got:" not in _output(client)
        resp = await client.request("tdbStdin", {"text": "42\n"})
        assert resp["success"] is True
        await wait_output(client, "got:42")
        await client.wait_event("terminated")
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_eof_makes_read_fail():
    client = await start_bash_adapter()
    try:
        await launch_stopped(client, PROGRAM, stop_on_entry=False)
        await wait_output(client, "enter: ")
        resp = await client.request("tdbStdin", {"eof": True})
        assert resp["success"] is True
        await wait_output(client, "eof")
        # a second EOF is a harmless no-op
        resp = await client.request("tdbStdin", {"eof": True})
        assert resp["success"] is True
        await client.wait_event("terminated")
        # ... but text after EOF is an error, not a silent drop
        resp = await client.request("tdbStdin", {"text": "late\n"})
        assert resp["success"] is False
        assert resp["message"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_stdin_before_launch_is_an_error():
    client = await start_bash_adapter()
    try:
        resp = await client.request("tdbStdin", {"text": "x\n"})
        assert resp["success"] is False
        assert "stdin" in resp["message"]
    finally:
        await client.stop()
