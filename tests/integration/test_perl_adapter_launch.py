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

SCRIPT = 'my $x = 1;\nmy $y = 2;\nprint "sum=", $x + $y, "\\n";\n'


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "toy.pl"
    p.write_text(SCRIPT)
    return str(p)


@pytest.fixture
async def client():
    c = AdapterClient()
    await c.start()
    yield c
    await c.stop()


async def test_launch_stop_on_entry_step_and_run_to_exit(client, script, tmp_path):
    await client.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = client.send(
        "launch",
        {
            "program": script,
            "args": [],
            "cwd": str(tmp_path),
            "stopOnEntry": True,
        },
    )
    await client.wait_event("initialized")
    await client.request("configurationDone")
    launch_resp = await asyncio.wait_for(launch_fut, 30)
    assert launch_resp["success"] is True
    stopped = await client.wait_event("stopped")
    assert stopped["body"]["reason"] == "entry"
    threads = await client.request("threads")
    assert threads["body"]["threads"] == [{"id": 1, "name": "main"}]
    await client.request("next")
    stopped = await client.wait_event("stopped")
    assert stopped["body"]["reason"] == "step"
    await client.request("continue")
    ev = await client.wait_event("output")
    outputs = [ev]
    while "sum=3" not in "".join(o["body"]["output"] for o in outputs):
        outputs.append(await client.wait_event("output"))
    await client.wait_event("terminated")


async def test_launch_missing_perl_program_errors(client, tmp_path):
    await client.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = client.send(
        "launch",
        {
            "program": str(tmp_path / "nope.pl"),
            "args": [],
            "cwd": str(tmp_path),
            "stopOnEntry": True,
        },
    )
    resp = await asyncio.wait_for(launch_fut, 30)
    assert resp["success"] is False
