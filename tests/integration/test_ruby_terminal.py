"""externalTerminal launch: proxy sends runInTerminal with the full rdbg
argv; the 'terminal' (this test) spawns it; session proceeds normally."""

import asyncio
import os
import shutil
import subprocess
import time

import pytest

from tdb.adapters.ruby.server import _ready_hook_env
from tests.integration.perl_adapter_harness import AdapterClient
from tests.integration.ruby_adapter_harness import (
    FIXTURES,
    rdbg_ok,
)

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")


async def test_external_terminal_handshake():
    client = AdapterClient()
    spawned: list[asyncio.subprocess.Process] = []
    env_seen: list[dict] = []

    async def fake_terminal(req):
        args = req["arguments"]
        assert args["kind"] == "external"
        assert "rdbg" in args["args"][0]
        assert "--open" in args["args"]
        # Merge env over ours, as both real launchers do
        # (tdb.session.terminal): it carries the ready-hook preload.
        env_seen.append(dict(args.get("env") or {}))
        proc = await asyncio.create_subprocess_exec(
            *args["args"], cwd=args["cwd"], env={**os.environ, **args["env"]}
        )
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
        (env,) = env_seen
        assert env["TDB_RDBG_READY_FILE"]
        assert "-rtdb_rdbg_ready" in env["RUBYOPT"].split()
        assert env["RUBYLIB"].split(os.pathsep)[0].endswith("ready")
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


def test_ready_hook_signals_only_once_parked(tmp_path):
    """The preloaded hook (ready/tdb_rdbg_ready.rb) creates the ready file
    when the real debug gem announces it is waiting for a client, i.e.
    once the session thread is parked and connecting is race-free."""
    ready = tmp_path / "ready"
    sock = tmp_path / "s"
    env = dict(os.environ)
    env.update(_ready_hook_env(env, str(ready)))
    proc = subprocess.Popen(
        [
            shutil.which("rdbg"),
            "--open",
            "--sock-path",
            str(sock),
            "--",
            str(FIXTURES / "ruby_hello.rb"),
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and proc.poll() is None:
            assert time.monotonic() < deadline, "ready file never appeared"
            time.sleep(0.05)
        assert ready.exists(), "rdbg exited before signalling ready"
        # Parked: nothing connected, so the program has not run.
        time.sleep(0.3)
        assert proc.poll() is None
    finally:
        proc.kill()
        out, err = proc.communicate()
    assert "wait for debugger connection..." in err
    assert "hello from ruby" not in out
