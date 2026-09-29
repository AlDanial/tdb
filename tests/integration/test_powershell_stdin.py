"""Console-view input reaches the debugged PowerShell script.

The proxy owns pwsh's stdin pipe; the tdb-private `tdbStdin` request
writes into it (text) or closes it (eof). Read-Host, $Host.UI.ReadLine()
and [Console]::ReadLine() all read that pipe (the first two only because
pwsh is started without -NonInteractive, which makes Read-Host throw).
"""

import asyncio

import pytest

from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    output_text,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")


async def _wait_output(client, needle: str, timeout: float = 30.0) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while needle not in output_text(client):
        assert loop.time() < deadline, (
            f"{needle!r} never appeared in output: {output_text(client)!r}"
        )
        await asyncio.sleep(0.05)
    return output_text(client)


async def test_read_host_round_trip():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "read_host.ps1"), stop_on_entry=False
        )
        # Read-Host's own prompt has no newline, so it cannot cross the
        # line-based stdout pump until the answer completes the line; the
        # preceding Write-Host is what proves the script is at the prompt.
        await _wait_output(client, "before")
        assert "got:" not in output_text(client)
        resp = await client.request("tdbStdin", {"text": "42\n"})
        assert resp["success"] is True
        text = await _wait_output(client, "got:42")
        # pwsh echoes the prompt and the line it read when stdin is a pipe.
        assert "enter: 42" in text
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 0
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_console_readline_round_trip():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "console_readline.ps1"), stop_on_entry=False
        )
        await _wait_output(client, "before")
        resp = await client.request("tdbStdin", {"text": "hello world\n"})
        assert resp["success"] is True
        await _wait_output(client, "got:hello world")
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_input_sent_while_stopped_is_kept_for_the_script():
    """Lines written before the script reads must not be eaten by PSES's
    console while the debuggee is stopped."""
    client = await start_powershell_adapter()
    try:
        program = str(FIXTURES / "read_host.ps1")
        await launch_stopped(client, program)
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        resp = await client.request("tdbStdin", {"text": "early\n"})
        assert resp["success"] is True
        await client.request("continue", {"threadId": 1})
        await _wait_output(client, "got:early")
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_eof_unblocks_reads_and_is_idempotent():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "read_host_eof.ps1"), stop_on_entry=False
        )
        await _wait_output(client, "before")
        resp = await client.request("tdbStdin", {"eof": True})
        assert resp["success"] is True
        text = await _wait_output(client, "null:True")
        assert "got:[]" in text  # Read-Host returns empty at EOF, no error
        # A second eof is a no-op success; text after eof is an error.
        resp = await client.request("tdbStdin", {"eof": True})
        assert resp["success"] is True
        resp = await client.request("tdbStdin", {"text": "late\n"})
        assert resp["success"] is False
        assert "closed" in resp["message"]
        await client.wait_event("terminated")
    finally:
        await client.stop()


async def test_stdin_before_launch_is_an_error():
    client = await start_powershell_adapter()
    try:
        resp = await client.request("tdbStdin", {"text": "42\n"})
        assert resp["success"] is False
        assert resp["message"]
        resp = await client.request("tdbStdin", {"eof": True})
        assert resp["success"] is False
    finally:
        await client.stop()


async def test_stdin_after_exit_is_an_error():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, str(FIXTURES / "exit7.ps1"), stop_on_entry=False)
        await client.wait_event("exited")
        await client.wait_event("terminated")
        resp = await client.request("tdbStdin", {"text": "42\n"})
        assert resp["success"] is False
        assert "exited" in resp["message"]
    finally:
        await client.stop()
