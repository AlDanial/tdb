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

LOOP_SCRIPT = "my $i = 0;\nwhile (1) { $i++; select undef, undef, undef, 0.01 }\n"


@pytest.fixture
async def running_loop(tmp_path):
    p = tmp_path / "loop.pl"
    p.write_text(LOOP_SCRIPT)
    c = AdapterClient()
    await c.start()
    await c.request("initialize", {"adapterID": "perl-tdb"})
    fut = c.send(
        "launch",
        {"program": str(p), "args": [], "cwd": str(tmp_path), "stopOnEntry": False},
    )
    await c.wait_event("initialized")
    await c.request("configurationDone")
    await asyncio.wait_for(fut, 30)
    await asyncio.sleep(1.0)  # let it spin
    yield c, str(p)
    await c.stop()


async def test_pause_stops_running_program(running_loop):
    c, _ = running_loop
    resp = await c.request("pause", {"threadId": 1})
    assert resp["success"] is True
    stopped = await c.wait_event("stopped")
    assert stopped["body"]["reason"] == "pause"


async def test_source_request_serves_compiled_file(running_loop):
    c, path = running_loop
    await c.request("pause", {"threadId": 1})
    await c.wait_event("stopped")
    resp = await c.request("source", {"source": {"path": path}})
    assert "while (1)" in resp["body"]["content"]
