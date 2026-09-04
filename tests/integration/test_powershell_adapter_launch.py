"""DAP-level: launch, entry stop, nonstop, output pump, exit code, args, env."""

import shutil
import subprocess
import time

import pytest

from tdb.languages.errors import parse_powershell_error
from tests.integration.powershell_adapter_harness import (
    FIXTURES,
    launch_stopped,
    output_text,
    pwsh_ok,
    start_powershell_adapter,
)

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")


async def test_stop_on_entry_lands_on_first_statement():
    client = await start_powershell_adapter()
    try:
        program = str(FIXTURES / "simple.ps1")
        await launch_stopped(client, program)
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "entry"
        st = await client.request("stackTrace", {"threadId": 1, "levels": 20})
        top = st["body"]["stackFrames"][0]
        assert top["source"]["path"] == program
        assert top["line"] == 2
        await client.request("continue", {"threadId": 1})
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
        assert "sum=2" in output_text(client) and "out=2" in output_text(client)
    finally:
        await client.stop()


async def test_nonstop_runs_to_completion_and_filters_prompt():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, str(FIXTURES / "exit7.ps1"), stop_on_entry=False)
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 7
        await client.wait_event("terminated")
        text = output_text(client)
        assert "bye" in text
        assert "PS " not in text and "tdb_launch" not in text
        assert "tdb-exit" not in text
    finally:
        await client.stop()


async def test_args_with_spaces_and_quotes():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client,
            str(FIXTURES / "functions.ps1"),
            stop_on_entry=False,
            args=["one two", "it's", "three"],
        )
        await client.wait_event("terminated")
        assert "args=one two|it's|three" in output_text(client)
    finally:
        await client.stop()


async def test_env_reaches_script(tmp_path):
    p = tmp_path / "env.ps1"
    p.write_text('Write-Host "K=$env:TDB_PS_TEST"\n')
    client = await start_powershell_adapter()
    try:
        fut = client.send(
            "launch",
            {
                "type": "powershell",
                "request": "launch",
                "program": str(p),
                "args": [],
                "cwd": str(tmp_path),
                "stopOnEntry": False,
                "console": "internalConsole",
                "env": {"TDB_PS_TEST": "hello"},
            },
        )
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        assert "K=hello" in output_text(client)
    finally:
        await client.stop()


async def test_cwd_is_honoured(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    p = tmp_path / "cwd.ps1"
    p.write_text("Write-Host (Get-Location).Path\n")
    client = await start_powershell_adapter()
    try:
        fut = client.send(
            "launch",
            {
                "type": "powershell",
                "request": "launch",
                "program": str(p),
                "args": [],
                "cwd": str(sub),
                "stopOnEntry": False,
                "console": "internalConsole",
            },
        )
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        assert str(sub) in output_text(client)
    finally:
        await client.stop()


async def test_uncaught_throw_exit_1_and_stderr_block():
    client = await start_powershell_adapter()
    try:
        program = str(FIXTURES / "throws.ps1")
        await launch_stopped(client, program, stop_on_entry=False)
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 1
        await client.wait_event("terminated")
        err = output_text(client, "stderr")
        assert err.startswith(f"Exception: {program}:1")
        assert "kaboom" in err
        assert "\x1b[" not in err  # NO_COLOR honoured
        assert "before" in output_text(client, "stdout")
        assert "after" not in output_text(client)
    finally:
        await client.stop()


async def test_write_error_is_not_fatal():
    client = await start_powershell_adapter()
    try:
        await launch_stopped(
            client, str(FIXTURES / "writes_error.ps1"), stop_on_entry=False
        )
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 0
        await client.wait_event("terminated")
        assert "still here" in output_text(client, "stdout")
        # PowerShell renders a non-terminating error as a ConciseView block,
        # so it IS tagged stderr — what matters is that tdb's fatal-error
        # parser ignores it, because the script exited 0.
        assert parse_powershell_error(output_text(client, "stderr"), 0) is None
    finally:
        await client.stop()


def _pses_pids() -> set[str]:
    out = subprocess.run(
        ["pgrep", "-f", "Start-EditorServices.ps1"], capture_output=True, text=True
    ).stdout
    return set(out.split())


@pytest.mark.skipif(shutil.which("pgrep") is None, reason="needs pgrep")
async def test_disconnect_leaves_no_pwsh():
    before = _pses_pids()
    client = await start_powershell_adapter()
    try:
        await launch_stopped(client, str(FIXTURES / "loop.ps1"), stop_on_entry=False)
        # loop.ps1 announces itself before spinning: pausing earlier can beat
        # PSES to the start of the script, and the pause is then lost.
        await client.wait_event("output")
        await client.request("pause", {"threadId": 1})
        ev = await client.wait_event("stopped")
        assert ev["body"]["reason"] == "pause"
        await client.request("disconnect")
        await client.proc.wait()
        leaked = _pses_pids() - before
        for _ in range(50):
            leaked = _pses_pids() - before
            if not leaked:
                break
            time.sleep(0.1)
        assert not leaked, f"pwsh survived disconnect: {leaked}"
    finally:
        await client.stop()
