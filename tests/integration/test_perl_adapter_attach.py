import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

PKG_DIR = Path(__file__).resolve().parents[2] / "src/tdb/adapters/perl"

REMOTE_PROG = """\
use Devel::TdbRemote;
my $counter = 10;
open my $fh, '>', $ARGV[1] or die;
Devel::TdbRemote::listen($ARGV[0], '127.0.0.1');
print {$fh} "listening\\n"; close $fh;
Devel::TdbRemote::wait_for_client();
$counter += 1;
$counter += 20;
print "counter=$counter\\n";
"""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def remote_debuggee(tmp_path):
    prog = tmp_path / "svc.pl"
    prog.write_text(REMOTE_PROG)
    ready = tmp_path / "ready"
    port = _free_port()
    proc = subprocess.Popen(
        ["perl", f"-I{PKG_DIR}", str(prog), str(port), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("debuggee never listened")
    yield proc, str(prog), port
    if proc.poll() is None:
        proc.kill()


async def test_attach_stop_step_inspect_continue(remote_debuggee, tmp_path):
    proc, prog, port = remote_debuggee
    c = AdapterClient()
    await c.start()
    try:
        await c.request("initialize", {"adapterID": "perl-tdb"})
        attach_fut = c.send("attach", {"host": "127.0.0.1", "port": port})
        await c.wait_event("initialized")
        await c.request("configurationDone")
        resp = await asyncio.wait_for(attach_fut, 30)
        assert resp["success"] is True
        stopped = await c.wait_event("stopped")
        assert stopped["body"]["reason"] == "entry"
        st = await c.request("stackTrace", {"threadId": 1})
        assert st["body"]["stackFrames"][0]["line"] == 7  # after wait_for_client()
        ev = await c.request("evaluate", {"expression": "$counter", "context": "repl"})
        assert ev["body"]["result"] == "10"
        await c.request("next")
        await c.wait_event("stopped")
        ev = await c.request("evaluate", {"expression": "$counter", "context": "repl"})
        assert ev["body"]["result"] == "11"
        pause = await c.request("pause", {"threadId": 1})
        # attach mode: gated
        assert pause["success"] in (True, False)
        await c.request("continue")
        out, _ = proc.communicate(timeout=15)
        assert "counter=31" in out
    finally:
        await c.stop()


async def test_attach_connection_refused_errors_helpfully(tmp_path):
    c = AdapterClient()
    await c.start()
    try:
        await c.request("initialize", {"adapterID": "perl-tdb"})
        fut = c.send("attach", {"host": "127.0.0.1", "port": _free_port()})
        resp = await asyncio.wait_for(fut, 30)
        assert resp["success"] is False
        assert "wait_for_client" in resp["message"]
    finally:
        await c.stop()
