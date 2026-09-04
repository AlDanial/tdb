"""Drive PowerShellDapServer end-to-end over real pipes against the
fake PSES (tests/unit/fake_pses.py). POSIX only."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from tdb.adapters.powershell.server import (
    CAPABILITIES,
    build_pwsh_command,
    connect_debug_service,
)
from tests.integration.perl_adapter_harness import AdapterClient
from tests.unit.fake_pses import make_fake_pwsh

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh shim")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    shim, pses = make_fake_pwsh(tmp_path)
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_PSES_LOG", str(log))
    monkeypatch.delenv("FAKE_PSES_MODE", raising=False)
    script = tmp_path / "s.ps1"
    script.write_text("Write-Host 1\n")
    return {
        "pwsh": shim,
        "pses": str(pses),
        "log": log,
        "script": str(script),
        "tmp": tmp_path,
    }


def requests_seen(log: Path, command: str) -> list[dict]:
    if not log.exists():
        return []
    return [
        json.loads(line)
        for line in log.read_text().splitlines()
        if json.loads(line).get("command") == command
    ]


async def start_proxy() -> AdapterClient:
    client = AdapterClient()
    await client.start(module="tdb.adapters.powershell")
    return client


def launch_args(fake, **extra) -> dict:
    return {
        "type": "powershell",
        "request": "launch",
        "program": fake["script"],
        "args": [],
        "cwd": str(fake["tmp"]),
        "stopOnEntry": False,
        "console": "internalConsole",
        "pwsh": fake["pwsh"],
        "pses": fake["pses"],
        **extra,
    }


async def test_initialize_is_answered_statically():
    client = await start_proxy()
    try:
        resp = await client.request("initialize", {"adapterID": "pses"})
        assert resp["success"] and resp["body"] == CAPABILITIES
        assert resp["body"]["supportsTerminateRequest"] is True
    finally:
        await client.stop()


async def test_launch_forwards_launcher_and_quoted_args(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake, args=["one two", "it's"]))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        assert (await fut)["success"]
        exited = await client.wait_event("exited")
        assert exited["body"]["exitCode"] == 3  # from the fake's sentinel
        await client.wait_event("terminated")
        [launch] = requests_seen(fake["log"], "launch")
        a = launch["arguments"]
        assert a["script"].endswith("tdb_launch.ps1")
        assert a["args"] == [f"'{fake['script']}'", "'one two'", "'it''s'"]
        assert a["cwd"] == str(fake["tmp"])
        assert "stopOnEntry" not in a and "env" not in a
        [init] = requests_seen(fake["log"], "initialize")
        assert init["arguments"]["adapterID"] == "pses"
    finally:
        await client.stop()


async def test_stdout_becomes_output_events_without_prompt_or_sentinel(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        outs = [e["body"] for e in client.events if e["event"] == "output"]
        text = "".join(o["output"] for o in outs)
        assert "hello from fake\n" in text
        assert "PS /tmp/fake>" not in text
        assert "tdb-exit" not in text
        assert all(o["category"] == "stdout" for o in outs)
    finally:
        await client.stop()


async def test_exited_precedes_terminated(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request("configurationDone")
        await fut
        await client.wait_event("terminated")
        names = [e["event"] for e in client.events] + ["terminated"]
        # wait_event removed "terminated"; "exited" must have come earlier
        assert "exited" in names
    finally:
        await client.stop()


async def test_missing_pwsh_fails_launch_with_hint(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake, pwsh="/nonexistent/pwsh"))
        assert resp["success"] is False
        assert "pwsh" in resp["message"]
    finally:
        await client.stop()


async def test_missing_pses_fails_launch_with_hint(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake, pses="/nonexistent/pses"))
        assert resp["success"] is False
        assert "PowerShellEditorServices.zip" in resp["message"]
    finally:
        await client.stop()


async def test_missing_program_fails_launch(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send(
            "launch", launch_args(fake, program="/nonexistent/x.ps1")
        )
        assert resp["success"] is False and "not found" in resp["message"]
    finally:
        await client.stop()


async def test_pwsh_dying_early_surfaces_its_output(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PSES_MODE", "die")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake))
        assert resp["success"] is False
        assert "boom: bad module" in resp["message"]
    finally:
        await client.stop()


async def test_session_file_timeout(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PSES_MODE", "no-session-file")
    monkeypatch.setenv("TDB_PSES_SESSION_TIMEOUT", "1.0")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake))
        assert resp["success"] is False
        assert "session file" in resp["message"]
    finally:
        await client.stop()


async def test_old_powershell_is_refused(fake, monkeypatch):
    monkeypatch.setenv("FAKE_PSES_MODE", "old-version")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        resp = await client.send("launch", launch_args(fake))
        assert resp["success"] is False
        assert "5.1" in resp["message"] and "7" in resp["message"]
    finally:
        await client.stop()


async def test_disconnect_kills_pwsh(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request(
            "setBreakpoints",
            {"source": {"path": fake["script"]}, "breakpoints": [{"line": 6}]},
        )
        await client.request("configurationDone")
        await fut
        await client.wait_event("stopped")
        resp = await client.request("disconnect")
        assert resp["success"]
        await client.proc.wait()  # proxy exits after disconnect
        # The fake pwsh (an sh shim that exec'd python fake_pses.py) must be
        # gone. Match on this test's own session dir so a concurrent test or
        # a stale fake from another run cannot make the assertion flaky.
        import subprocess
        import time

        needle = f"fake_pses.py.*{fake['tmp']}"
        for _ in range(30):
            out = subprocess.run(
                ["pgrep", "-f", needle], capture_output=True, text=True
            ).stdout
            if not out.strip():
                break
            time.sleep(0.1)
        assert not out.strip(), f"fake pwsh survived disconnect: {out}"
    finally:
        await client.stop()


async def test_terminate_is_answered_locally_and_ends_session(fake):
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request(
            "setBreakpoints",
            {"source": {"path": fake["script"]}, "breakpoints": [{"line": 6}]},
        )
        await client.request("configurationDone")
        await fut
        await client.wait_event("stopped")
        resp = await client.request("terminate")
        assert resp["success"]
        await client.wait_event("exited")
        await client.wait_event("terminated")
        assert not requests_seen(fake["log"], "terminate"), (
            "terminate must not reach PSES"
        )
    finally:
        await client.stop()


async def test_socket_death_without_terminated_ends_the_session(fake, monkeypatch):
    """PSES's socket dies mid-session with no `terminated` while the pwsh
    host survives: the proxy must still end the session rather than hang."""
    monkeypatch.setenv("FAKE_PSES_MODE", "socket-die")
    client = await start_proxy()
    try:
        await client.request("initialize", {"adapterID": "pses"})
        fut = client.send("launch", launch_args(fake))
        await client.wait_event("initialized")
        await client.request(
            "setBreakpoints",
            {"source": {"path": fake["script"]}, "breakpoints": [{"line": 6}]},
        )
        await client.request("configurationDone")
        await fut
        await client.wait_event("stopped")
        client.send("pause", {"threadId": 1})  # the fake never answers this
        await client.wait_event("exited", timeout=10)
        await client.wait_event("terminated", timeout=10)
        resp = await client.request("threads", timeout=10)
        assert resp["success"] is False
        assert "no debug session" in resp["message"]
    finally:
        await client.stop()


def test_build_pwsh_command(tmp_path):
    cmd = build_pwsh_command(
        "/bin/pwsh",
        tmp_path / "PSES",
        tmp_path / "s.json",
        tmp_path / "log",
        "tdb-pses-1",
    )
    assert cmd[:6] == [
        "/bin/pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(tmp_path / "PSES" / "Start-EditorServices.ps1"),
    ]
    assert "-DebugServiceOnly" in cmd
    assert cmd[cmd.index("-DebugServicePipeName") + 1] == "tdb-pses-1"
    assert cmd[cmd.index("-BundledModulesPath") + 1] == str(tmp_path)
    assert cmd[cmd.index("-LogLevel") + 1] == "None"
    assert cmd[cmd.index("-SessionDetailsPath") + 1] == str(tmp_path / "s.json")
    assert "-Stdio" not in cmd


async def test_connect_debug_service_posix(tmp_path):
    sock = str(tmp_path / "s")

    async def echo(r, w):
        w.write(await r.read(5))
        await w.drain()
        w.close()

    server = await asyncio.start_unix_server(echo, path=sock)
    async with server:
        r, w = await connect_debug_service({"debugServicePipeName": sock})
        w.write(b"hello")
        await w.drain()
        assert await r.read(5) == b"hello"
        w.close()


async def test_connect_debug_service_windows_branch_is_selected(monkeypatch):
    calls = []
    monkeypatch.setattr("tdb.adapters.powershell.server.sys.platform", "win32")

    async def fake_pipe(name):
        calls.append(name)
        return ("r", "w")

    monkeypatch.setattr(
        "tdb.adapters.powershell.server._connect_windows_pipe", fake_pipe
    )
    assert await connect_debug_service({"debugServicePipeName": r"\\.\pipe\tdb-x"}) == (
        "r",
        "w",
    )
    assert calls == [r"\\.\pipe\tdb-x"]
