"""Devel::TdbRemote::breakpoint(): the Perl analog of `tdb.breakpoint()`.

See hook_harness for the fake-tdb / pty arrangement shared by every
language's hook tests.
"""

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from tests.integration.hook_harness import HookDebuggee, remote_port

from .perl_adapter_harness import AdapterClient

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

PKG_DIR = Path(__file__).resolve().parents[2] / "src/tdb/adapters/perl"

PROG = """\
use Devel::TdbRemote;
my $counter = 10;
Devel::TdbRemote::breakpoint();
$counter += 1;
$counter += 20;
Devel::TdbRemote::breakpoint();
$counter += 300;
print "counter=$counter\\n";
"""


def _debuggee(tmp_path, **kw) -> HookDebuggee:
    prog = tmp_path / "hooked.pl"
    prog.write_text(PROG)
    d = HookDebuggee(tmp_path, ["perl", f"-I{PKG_DIR}", str(prog)], **kw)
    d.prog = prog
    return d


@pytest.fixture
def debuggee(tmp_path):
    d = _debuggee(tmp_path)
    yield d
    d.close()


def _port(argv: list[str]) -> int:
    return remote_port(argv, "perl")


async def _attach(c: AdapterClient, port: int) -> dict:
    await c.request("initialize", {"adapterID": "perl-tdb"})
    attach_fut = c.send("attach", {"host": "127.0.0.1", "port": port})
    await c.wait_event("initialized")
    await c.request("configurationDone")
    resp = await asyncio.wait_for(attach_fut, 30)
    assert resp["success"] is True, resp
    stopped = await c.wait_event("stopped")
    assert stopped["body"]["reason"] == "entry"
    return stopped


async def _top_line(c: AdapterClient, prog: Path) -> int:
    st = await c.request("stackTrace", {"threadId": 1})
    top = st["body"]["stackFrames"][0]
    assert top["source"]["path"] == str(prog), top
    return top["line"]


async def _eval(c: AdapterClient, expr: str) -> str:
    ev = await c.request("evaluate", {"expression": expr, "context": "repl"})
    assert ev["success"] is True, ev
    return ev["body"]["result"]


async def test_breakpoint_spawns_tdb_and_stops_on_next_line(debuggee):
    argv = debuggee.wait_spawn(1)
    port = _port(argv)
    c = AdapterClient()
    await c.start()
    try:
        await _attach(c, port)
        # The entry stop is the statement after breakpoint(), in the
        # caller -- not inside TdbRemote.pm and not one line further.
        assert await _top_line(c, debuggee.prog) == 4
        assert await _eval(c, "$counter") == "10"
        await c.request("next")
        await c.wait_event("stopped")
        assert await _top_line(c, debuggee.prog) == 5
    finally:
        await c.stop()


async def test_second_breakpoint_reuses_live_tdb(debuggee):
    port = _port(debuggee.wait_spawn(1))
    c = AdapterClient()
    await c.start()
    try:
        await _attach(c, port)
        await c.request("continue")
        # Second breakpoint(): tdb is still alive, so no new spawn -- the
        # existing connection gets an unsolicited stop on line 7.
        stopped = await c.wait_event("stopped")
        assert stopped["body"]["reason"] in ("breakpoint", "pause", "step")
        assert await _top_line(c, debuggee.prog) == 7
        assert await _eval(c, "$counter") == "31"
        assert len(debuggee.spawns()) == 1
        resp = await c.request("disconnect")
        assert resp["success"] is True, resp
        out, err = debuggee.finish()
        assert debuggee.proc.returncode == 0, (debuggee.proc.returncode, out, err)
        assert "counter=331" in out, out
    finally:
        await c.stop()


async def test_breakpoint_respawns_tdb_after_it_quit(debuggee):
    port = _port(debuggee.wait_spawn(1))
    c = AdapterClient()
    await c.start()
    try:
        await _attach(c, port)
        # Simulate the user quitting tdb: the TUI process goes away and
        # the DAP client disconnects. The program must keep running.
        os.kill(debuggee.fake_pid(1), signal.SIGKILL)
        resp = await c.request("disconnect")
        assert resp["success"] is True, resp
    finally:
        await c.stop()
    # Second breakpoint(): previous tdb is dead -> spawn a new one and
    # wait for it to attach.
    port2 = _port(debuggee.wait_spawn(2))
    c = AdapterClient()
    await c.start()
    try:
        await _attach(c, port2)
        assert await _top_line(c, debuggee.prog) == 7
        assert await _eval(c, "$counter") == "31"
        resp = await c.request("disconnect")
        assert resp["success"] is True, resp
        out, err = debuggee.finish()
        assert debuggee.proc.returncode == 0, (debuggee.proc.returncode, out, err)
        assert "counter=331" in out, out
        # Re-attaching re-adopts the helpers; that must be silent.
        assert "redefined" not in err, err
    finally:
        await c.stop()


def test_breakpoint_warns_and_continues_when_tdb_is_missing(tmp_path):
    d = _debuggee(tmp_path, tdb="/nonexistent/dir/tdb")
    try:
        out, err = d.finish()
        assert d.proc.returncode == 0, (d.proc.returncode, out, err)
        assert "counter=331" in out, out
        assert "Devel::TdbRemote" in err and "tdb" in err, err
    finally:
        d.close()


async def test_breakpoint_respawns_when_tdb_lingers_after_disconnect(debuggee):
    """The real tdb tears its TUI down for a while after it has sent
    `disconnect`. A breakpoint() reached in that window must notice the
    debug socket is gone (not merely ask whether the old process is still
    alive) and attach a fresh tdb -- otherwise perl5db writes its prompt
    to the dead socket and the program dies of SIGPIPE."""
    port = _port(debuggee.wait_spawn(1))
    c = AdapterClient()
    await c.start()
    try:
        await _attach(c, port)
        resp = await c.request("disconnect")
        assert resp["success"] is True, resp
    finally:
        await c.stop()
    # NOTE: the fake tdb is deliberately left running here.
    port2 = _port(debuggee.wait_spawn(2, timeout=20))
    c = AdapterClient()
    await c.start()
    try:
        await _attach(c, port2)
        assert await _top_line(c, debuggee.prog) == 7
        resp = await c.request("disconnect")
        assert resp["success"] is True, resp
        out, err = debuggee.finish()
        assert debuggee.proc.returncode == 0, (debuggee.proc.returncode, out, err)
        assert "counter=331" in out, out
    finally:
        await c.stop()


async def test_stop_location_echo_goes_to_debug_socket_not_tty(debuggee):
    """perl5db echoes `main::(file:N):` + the source line at every stop to
    its LINEINFO handle. In attach mode that must be the debug socket (the
    adapter discards it), never the tty tdb is drawing on."""
    port = _port(debuggee.wait_spawn(1))
    c = AdapterClient()
    await c.start()
    try:
        await _attach(c, port)
        await c.request("continue")
        await c.wait_event("stopped")  # second breakpoint(), line 7
        resp = await c.request("disconnect")
        assert resp["success"] is True, resp
        out, err = debuggee.finish()
        assert debuggee.proc.returncode == 0, (debuggee.proc.returncode, out, err)
        # Under the test harness perl5db's console is stderr rather than
        # the pty, so check both.
        assert "main::(" not in out, out
        assert "main::(" not in err, err
        assert "counter=331" in out, out
    finally:
        await c.stop()
