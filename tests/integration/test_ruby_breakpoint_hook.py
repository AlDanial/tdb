"""Tdb.breakpoint (require 'tdb'): the Ruby analog of `tdb.breakpoint()`.

rdbg is a DAP server, so the tests play tdb's part with a raw TCP DAP
client, exactly as tdb's own `-r --lang ruby` path does. See hook_harness
for the fake-tdb / pty arrangement.
"""

import os
from pathlib import Path

import pytest

from tests.integration.hook_harness import HookDebuggee, TcpDapClient, remote_port
from tests.integration.ruby_adapter_harness import rdbg_ok

pytestmark = pytest.mark.skipif(not rdbg_ok(), reason="needs rdbg (debug gem >= 1.9)")

RUBY_DIR = Path(__file__).resolve().parents[2] / "src/tdb/adapters/ruby"

PROG = """\
require 'tdb'
counter = 10
Tdb.breakpoint
counter += 1
counter += 20
Tdb.breakpoint
counter += 300
puts "counter=#{counter}"
"""


def _debuggee(tmp_path, **kw) -> HookDebuggee:
    prog = tmp_path / "hooked.rb"
    prog.write_text(PROG)
    d = HookDebuggee(
        tmp_path, ["ruby", str(prog)], env={"RUBYLIB": str(RUBY_DIR)}, **kw
    )
    d.prog = prog
    return d


@pytest.fixture
def debuggee(tmp_path):
    d = _debuggee(tmp_path)
    yield d
    d.close()


async def _attach(c: TcpDapClient, port: int) -> dict:
    await c.connect("127.0.0.1", port)
    await c.request("initialize", {"adapterID": "rdbg"})
    attach_fut = c.send("attach", {"type": "ruby", "request": "attach"})
    await c.wait_event("initialized")
    await c.request("configurationDone")
    resp = await attach_fut
    assert resp["success"] is True, resp
    stopped = await c.wait_event("stopped")
    # The program was already parked at binding.break's one-shot line
    # breakpoint when we connected; rdbg reports that first stop as a
    # "pause" to a freshly attached client.
    assert stopped["body"]["reason"] in ("pause", "breakpoint"), stopped
    return stopped


async def _top_line(c: TcpDapClient, prog: Path) -> int:
    st = await c.request("stackTrace", {"threadId": 1})
    top = st["body"]["stackFrames"][0]
    assert top["source"]["path"] == str(prog), top
    return top["line"]


async def _eval(c: TcpDapClient, expr: str) -> str:
    ev = await c.request(
        "evaluate", {"expression": expr, "context": "repl", "frameId": 1}
    )
    if not ev["success"]:
        st = await c.request("stackTrace", {"threadId": 1})
        fid = st["body"]["stackFrames"][0]["id"]
        ev = await c.request(
            "evaluate", {"expression": expr, "context": "repl", "frameId": fid}
        )
    assert ev["success"] is True, ev
    return ev["body"]["result"]


async def test_breakpoint_spawns_tdb_and_stops_on_next_line(debuggee):
    port = remote_port(debuggee.wait_spawn(1), "ruby")
    c = TcpDapClient()
    try:
        await _attach(c, port)
        # The stop is the statement after Tdb.breakpoint, in the caller --
        # not inside tdb.rb and not one line further.
        assert await _top_line(c, debuggee.prog) == 4
        assert await _eval(c, "counter") == "10"
        await c.request("next", {"threadId": 1})
        await c.wait_event("stopped")
        assert await _top_line(c, debuggee.prog) == 5
    finally:
        await c.stop()


async def test_second_breakpoint_reuses_live_tdb(debuggee):
    port = remote_port(debuggee.wait_spawn(1), "ruby")
    c = TcpDapClient()
    try:
        await _attach(c, port)
        await c.request("continue", {"threadId": 1})
        stopped = await c.wait_event("stopped")
        assert stopped["body"]["reason"] == "breakpoint", stopped
        assert await _top_line(c, debuggee.prog) == 7
        assert await _eval(c, "counter") == "31"
        assert len(debuggee.spawns()) == 1
        resp = await c.request("disconnect", {"terminateDebuggee": False})
        assert resp["success"] is True, resp
    finally:
        await c.stop()
    out, err = debuggee.finish()
    assert debuggee.proc.returncode == 0, (debuggee.proc.returncode, out, err)
    assert "counter=331" in out, out


async def test_breakpoint_respawns_when_tdb_lingers_after_disconnect(debuggee):
    """After tdb disconnects (its TUI still tearing down: the fake stays
    alive), the next Tdb.breakpoint must get a fresh tdb rather than leave
    rdbg blocked forever on "wait for debugger connection..."."""
    port = remote_port(debuggee.wait_spawn(1), "ruby")
    c = TcpDapClient()
    try:
        await _attach(c, port)
        resp = await c.request("disconnect", {"terminateDebuggee": False})
        assert resp["success"] is True, resp
    finally:
        await c.stop()
    port2 = remote_port(debuggee.wait_spawn(2, timeout=20), "ruby")
    c = TcpDapClient()
    try:
        await _attach(c, port2)
        assert await _top_line(c, debuggee.prog) == 7
        assert await _eval(c, "counter") == "31"
        resp = await c.request("disconnect", {"terminateDebuggee": False})
        assert resp["success"] is True, resp
    finally:
        await c.stop()
    out, err = debuggee.finish()
    assert debuggee.proc.returncode == 0, (debuggee.proc.returncode, out, err)
    assert "counter=331" in out, out


def test_breakpoint_is_noop_without_tty(tmp_path):
    import subprocess

    prog = tmp_path / "hooked.rb"
    prog.write_text(PROG)
    proc = subprocess.run(
        ["ruby", str(prog)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "RUBYLIB": str(RUBY_DIR), "TDB": "/nonexistent/tdb"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "counter=331\n"
    # No debug server, no "wait for debugger connection..." chatter.
    assert "DEBUGGER" not in proc.stderr, proc.stderr


def test_breakpoint_warns_and_continues_when_tdb_is_missing(tmp_path):
    d = _debuggee(tmp_path, tdb="/nonexistent/dir/tdb")
    try:
        out, err = d.finish(timeout=30)
        assert d.proc.returncode == 0, (d.proc.returncode, out, err)
        assert "counter=331" in out, out
        assert "tdb" in err, err
    finally:
        d.close()
