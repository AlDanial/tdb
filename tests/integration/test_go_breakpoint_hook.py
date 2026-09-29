"""tdb.Breakpoint() (go/tdb): the Go analog of `tdb.breakpoint()`.

The hook spawns `tdb --lang go -a PID --no-pause-on-attach`, waits for
Delve to ptrace-attach, then traps with runtime.Breakpoint. The tests play
tdb's part with a real `dlv dap` and a raw TCP DAP client, exactly as
tdb's own `-a` path does. See hook_harness for the fake-tdb / pty
arrangement. Linux-only, like `-a` itself (TracerPid, PR_SET_PTRACER).
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tdb.languages.go import is_breakpoint_hook_frame
from tests.integration.hook_harness import HookDebuggee, TcpDapClient

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or shutil.which("go") is None
    or shutil.which("dlv") is None,
    reason="Go breakpoint hook needs Linux, the go toolchain and dlv",
)

REPO = Path(__file__).resolve().parents[2]
GO_TDB_DIR = REPO / "go" / "tdb"
MODULE = "github.com/AlDanial/tdb/go/tdb"

PROG = f"""\
package main

import (
\t"fmt"

\t"{MODULE}"
)

func main() {{
\tcounter := 10
\ttdb.Breakpoint()
\tcounter += 1
\tcounter += 20
\ttdb.Breakpoint()
\tcounter += 300
\tfmt.Println("counter =", counter)
}}
"""
LINE_AFTER_FIRST = 12  # `counter += 1`
LINE_AFTER_SECOND = 15  # `counter += 300`

GO_MOD = f"""\
module hooked

go 1.21

require {MODULE} v0.0.0

replace {MODULE} => {GO_TDB_DIR}
"""


def _build(d: Path, prog: str) -> Path:
    """Build `prog` against the checkout's go/tdb, unoptimized so dlv can
    read `counter`."""
    (d / "main.go").write_text(prog)
    (d / "go.mod").write_text(GO_MOD)
    binary = d / "hooked"
    subprocess.run(
        ["go", "build", "-gcflags=all=-N -l", "-o", str(binary), "."],
        cwd=d,
        check=True,
        env={**os.environ, "GOFLAGS": "-mod=mod"},
    )
    return binary


@pytest.fixture(scope="module")
def hooked_binary(tmp_path_factory) -> Path:
    return _build(tmp_path_factory.mktemp("go_hooked"), PROG)


@pytest.fixture(scope="module")
def sleepy_binary(tmp_path_factory) -> Path:
    """PROG with a pause between the two calls, so a test can detach while
    the program is running rather than stopped."""
    prog = PROG.replace('\t"fmt"\n', '\t"fmt"\n\t"time"\n').replace(
        "\tcounter += 20\n", "\tcounter += 20\n\ttime.Sleep(time.Second)\n"
    )
    return _build(tmp_path_factory.mktemp("go_sleepy"), prog)


@pytest.fixture
def debuggee(tmp_path, hooked_binary):
    d = HookDebuggee(tmp_path, [str(hooked_binary)])
    d.prog = hooked_binary.with_name("main.go")
    yield d
    d.close()


class Dlv:
    """One `dlv dap` server plus a client attached to a pid, the way
    `tdb -a PID --no-pause-on-attach` composes them."""

    def __init__(self):
        self.proc = None
        self.client = TcpDapClient()

    async def attach(self, pid: int) -> dict:
        self.proc = subprocess.Popen(
            ["dlv", "dap", "--listen=127.0.0.1:0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        line = self.proc.stdout.readline()
        m = re.search(r"listening at: [^:]+:(\d+)", line)
        assert m, f"unexpected dlv banner: {line!r}"
        c = self.client
        await c.connect("127.0.0.1", int(m.group(1)))
        await c.request("initialize", {"adapterID": "go", "linesStartAt1": True})
        attach_fut = c.send(
            "attach", {"mode": "local", "processId": pid, "stopOnEntry": False}
        )
        # dlv answers a failed attach with an error response and no
        # `initialized`; surface the reason instead of a bare timeout.
        for _ in range(300):
            if attach_fut.done() or any(e["event"] == "initialized" for e in c.events):
                break
            await asyncio.sleep(0.1)
        if attach_fut.done() and not attach_fut.result()["success"]:
            pytest.fail(f"dlv attach to {pid} failed: {attach_fut.result()}")
        await c.wait_event("initialized")
        await c.request("configurationDone")
        resp = await attach_fut
        assert resp["success"] is True, resp
        stopped = await c.wait_event("stopped")
        assert stopped["body"]["reason"] == "breakpoint", stopped
        return stopped

    async def top(self, tid: int) -> tuple[str, int, str]:
        st = await self.client.request("stackTrace", {"threadId": tid})
        f = st["body"]["stackFrames"][0]
        return f["name"], f["line"], f["source"]["path"], f["id"]

    async def step_out_of_hook(self, stopped: dict) -> tuple[int, int]:
        """Assert the stop is inside tdb.Breakpoint (what tdb's step-out
        rule keys on), step out, and return (threadId, frameId) of the
        caller's frame now on top."""
        tid = stopped["body"]["threadId"]
        name, _line, _path, _fid = await self.top(tid)
        # dlv reports the short "pkg.Func" form; tdb's rule accepts both.
        assert is_breakpoint_hook_frame(SimpleNamespace(name=name)), name
        await self.client.request("stepOut", {"threadId": tid})
        ev = await self.client.wait_event("stopped")
        return ev["body"]["threadId"]

    async def eval(self, expr: str, fid: int) -> str:
        ev = await self.client.request(
            "evaluate", {"expression": expr, "context": "repl", "frameId": fid}
        )
        assert ev["success"] is True, ev
        return ev["body"]["result"]

    async def detach(self):
        await self.client.request("disconnect", {"terminateDebuggee": False})
        await self.client.stop()

    def close(self):
        """`dlv dap` exits on its own once its client has disconnected; give
        it time to finish detaching (killing it mid-detach can leave
        threads stopped) before resorting to SIGKILL."""
        if self.proc is None:
            return
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def _spawn_argv(debuggee: HookDebuggee, n: int) -> list[str]:
    argv = debuggee.wait_spawn(n)
    assert argv == [
        "--lang",
        "go",
        "-a",
        str(debuggee.proc.pid),
        "--no-pause-on-attach",
    ], argv
    return argv


async def test_breakpoint_spawns_tdb_and_stops_on_next_line(debuggee):
    _spawn_argv(debuggee, 1)
    dlv = Dlv()
    try:
        stopped = await dlv.attach(debuggee.proc.pid)
        tid = await dlv.step_out_of_hook(stopped)
        name, line, path, fid = await dlv.top(tid)
        assert name == "main.main"
        assert Path(path) == debuggee.prog
        assert line == LINE_AFTER_FIRST
        assert await dlv.eval("counter", fid) == "10"
        await dlv.client.request("next", {"threadId": tid})
        await dlv.client.wait_event("stopped")
        _name, line, _path, fid = await dlv.top(tid)
        assert line == LINE_AFTER_FIRST + 1
        assert await dlv.eval("counter", fid) == "11"
        # Run on: the second call traps into the same tdb, and continuing
        # past it lets the program finish under the debugger.
        await dlv.client.request("continue", {"threadId": tid})
        stopped = await dlv.client.wait_event("stopped")
        tid = await dlv.step_out_of_hook(stopped)
        await dlv.client.request("continue", {"threadId": tid})
        await dlv.client.wait_event("terminated")
        await dlv.client.stop()
    finally:
        dlv.close()
    out, err = debuggee.finish()
    assert "counter = 331" in out, (out, err)
    assert debuggee.proc.returncode == 0
    assert len(debuggee.spawns()) == 1


async def test_second_breakpoint_reuses_live_tdb(debuggee):
    _spawn_argv(debuggee, 1)
    dlv = Dlv()
    try:
        stopped = await dlv.attach(debuggee.proc.pid)
        tid = await dlv.step_out_of_hook(stopped)
        await dlv.client.request("continue", {"threadId": tid})
        # Still traced: the second call traps at once, no new tdb.
        stopped = await dlv.client.wait_event("stopped")
        assert stopped["body"]["reason"] == "breakpoint"
        tid = await dlv.step_out_of_hook(stopped)
        _name, line, _path, fid = await dlv.top(tid)
        assert line == LINE_AFTER_SECOND
        assert await dlv.eval("counter", fid) == "31"
        assert len(debuggee.spawns()) == 1
        await dlv.detach()
    finally:
        dlv.close()
    out, _err = debuggee.finish()
    assert "counter = 331" in out
    assert debuggee.proc.returncode == 0


async def test_respawns_tdb_after_it_quit(debuggee):
    """Ctrl+q: dlv detaches, tdb exits. The next call must notice (TracerPid
    back to 0) and start a fresh tdb rather than trap into nothing -- an
    untraced runtime.Breakpoint would kill the program with SIGTRAP."""
    _spawn_argv(debuggee, 1)
    dlv = Dlv()
    try:
        stopped = await dlv.attach(debuggee.proc.pid)
        await dlv.step_out_of_hook(stopped)
        os.kill(debuggee.fake_pid(1), 9)
        await dlv.detach()
    finally:
        dlv.close()
    _spawn_argv(debuggee, 2)
    dlv = Dlv()
    try:
        stopped = await dlv.attach(debuggee.proc.pid)
        tid = await dlv.step_out_of_hook(stopped)
        _name, line, _path, _fid = await dlv.top(tid)
        assert line == LINE_AFTER_SECOND
        await dlv.detach()
    finally:
        dlv.close()
    out, _err = debuggee.finish()
    assert "counter = 331" in out
    assert debuggee.proc.returncode == 0


async def test_respawns_when_tdb_lingers_after_disconnect(debuggee):
    """The real TUI takes a moment to exit after dlv has detached. The hook
    waits briefly for it, then spawns anyway rather than hanging."""
    _spawn_argv(debuggee, 1)
    dlv = Dlv()
    try:
        stopped = await dlv.attach(debuggee.proc.pid)
        await dlv.step_out_of_hook(stopped)
        await dlv.detach()  # fake tdb #1 stays alive
    finally:
        dlv.close()
    assert debuggee.wait_spawn(2, timeout=20.0)[:2] == ["--lang", "go"]
    dlv = Dlv()
    try:
        stopped = await dlv.attach(debuggee.proc.pid)
        await dlv.step_out_of_hook(stopped)
        await dlv.detach()
    finally:
        dlv.close()
    out, _err = debuggee.finish()
    assert "counter = 331" in out


async def test_detach_while_running_then_rehooks(tmp_path, sleepy_binary):
    """Ctrl+q while the program runs (dlv halts, detaches, resumes it): the
    program must keep going, and its next call must start a fresh tdb --
    the untraced thread may never execute the trap."""
    d = HookDebuggee(tmp_path, [str(sleepy_binary)])
    try:
        _spawn_argv(d, 1)
        dlv = Dlv()
        try:
            stopped = await dlv.attach(d.proc.pid)
            tid = await dlv.step_out_of_hook(stopped)
            await dlv.client.request("continue", {"threadId": tid})
            await asyncio.sleep(0.2)  # inside time.Sleep
            os.kill(d.fake_pid(1), 9)
            await dlv.detach()
        finally:
            dlv.close()
        _spawn_argv(d, 2)
        dlv = Dlv()
        try:
            stopped = await dlv.attach(d.proc.pid)
            tid = await dlv.step_out_of_hook(stopped)
            _name, line, _path, fid = await dlv.top(tid)
            assert await dlv.eval("counter", fid) == "31"
            await dlv.detach()
        finally:
            dlv.close()
        out, err = d.finish()
    finally:
        d.close()
    assert "counter = 331" in out, (out, err)
    assert d.proc.returncode == 0


def test_breakpoint_is_noop_without_tty(hooked_binary, tmp_path):
    log = tmp_path / "log"
    r = subprocess.run(
        [str(hooked_binary)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "TDB": "/nonexistent/tdb", "FAKE_TDB_LOG": str(log)},
    )
    assert r.returncode == 0, r
    assert "counter = 331" in r.stdout
    assert r.stderr == ""
    assert not log.exists()


def test_warns_and_continues_when_tdb_is_missing(tmp_path, hooked_binary):
    d = HookDebuggee(tmp_path, [str(hooked_binary)], tdb="/nonexistent/tdb")
    try:
        out, err = d.finish()
    finally:
        d.close()
    assert d.proc.returncode == 0
    assert "counter = 331" in out
    assert "tdb" in err and "/nonexistent/tdb" in err


def test_continues_when_tdb_exits_before_attaching(tmp_path, hooked_binary):
    """A tdb that dies during startup (bad config, missing dlv) must not
    leave the program waiting for an attach that will never come."""
    dying = tmp_path / "dying_tdb"
    dying.write_text("#!/bin/sh\necho 'tdb: boom' >&2\nexit 2\n")
    dying.chmod(0o755)
    d = HookDebuggee(tmp_path, [str(hooked_binary)], tdb=str(dying))
    try:
        out, err = d.finish()
    finally:
        d.close()
    assert d.proc.returncode == 0
    assert "counter = 331" in out
    assert "exited before attaching" in err


def test_go_module_vets_and_tests_clean():
    """The module's own checks: gofmt, go vet, and its unit tests (which
    run without a tty, so Breakpoint must be a no-op there)."""
    fmt = subprocess.run(
        ["gofmt", "-l", "."], cwd=GO_TDB_DIR, capture_output=True, text=True
    )
    assert fmt.stdout == "", f"gofmt would reformat: {fmt.stdout}"
    subprocess.run(["go", "vet", "./..."], cwd=GO_TDB_DIR, check=True)
    subprocess.run(["go", "test", "./..."], cwd=GO_TDB_DIR, check=True)
