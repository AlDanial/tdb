"""Console-view input reaches the debugged perl program's STDIN via the
tdb-private `tdbStdin` DAP request (launch mode, adapter-owned pipe)."""

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

# The prompt has no trailing newline on purpose: the adapter must deliver
# partial stdout chunks before the program blocks in <STDIN>.
READ_SCRIPT = (
    'print "enter: "; $| = 1;\n'
    "my $x = <STDIN>;\n"
    'if (defined $x) { chomp $x; print "got:$x\\n"; } else { print "eof\\n"; }\n'
)


@pytest.fixture
def script(tmp_path):
    p = tmp_path / "stdin.pl"
    p.write_text(READ_SCRIPT)
    return str(p)


@pytest.fixture
async def client():
    c = AdapterClient()
    await c.start()
    yield c
    await c.stop()


async def _output_until(
    client: AdapterClient, needle: str, timeout: float = 30.0
) -> str:
    """Drain `output` events until their concatenation contains `needle`."""
    text = ""
    while needle not in text:
        ev = await client.wait_event("output", timeout=timeout)
        text += ev["body"]["output"]
    return text


async def _launch_running(client: AdapterClient, script: str, cwd: str) -> None:
    await client.request("initialize", {"adapterID": "perl-tdb"})
    launch_fut = client.send(
        "launch",
        {"program": script, "args": [], "cwd": cwd, "stopOnEntry": False},
    )
    await client.wait_event("initialized")
    await client.request("configurationDone")
    launch_resp = await asyncio.wait_for(launch_fut, 30)
    assert launch_resp["success"] is True


async def test_text_reaches_program_stdin(client, script, tmp_path):
    await _launch_running(client, script, str(tmp_path))
    await _output_until(client, "enter: ")
    resp = await client.request("tdbStdin", {"text": "42\n"})
    assert resp["success"] is True
    assert "got:42" in await _output_until(client, "got:42")
    await client.wait_event("terminated")


async def test_eof_makes_stdin_return_undef(client, script, tmp_path):
    await _launch_running(client, script, str(tmp_path))
    await _output_until(client, "enter: ")
    resp = await client.request("tdbStdin", {"eof": True})
    assert resp["success"] is True
    assert "eof" in await _output_until(client, "eof")
    await client.wait_event("terminated")
    # A second EOF is a harmless no-op.
    resp = await client.request("tdbStdin", {"eof": True})
    assert resp["success"] is True


async def test_stdin_before_launch_is_an_error(client):
    await client.request("initialize", {"adapterID": "perl-tdb"})
    resp = await client.request("tdbStdin", {"text": "x\n"})
    assert resp["success"] is False
    assert resp["message"]
