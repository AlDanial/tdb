"""End-to-end: DebugController + bundled perl adapter + real perl.

Skipped wholesale when perl is missing or too old, mirroring the other
Perl integration tests.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tdb.languages import registry
from tdb.languages.perl import build_perl_profile
from tdb.dap.types import SourceBreakpoint
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase
from tdb._timeouts import DAP_INITIALIZED, DAP_STOP_ON_ENTRY

pytestmark = pytest.mark.skipif(
    shutil.which("perl") is None
    or subprocess.run(["perl", "-e", "require v5.18"]).returncode != 0,
    reason="perl >= 5.18 required",
)

WAIT = 20.0  # generous ceiling for adapter spawn + debuggee start

PERL_SRC = """\
sub add {
    my ($a, $b) = @_;
    my $result = $a + $b;
    return $result;
}
my $x = 5;
my $y = add($x, 7);
print "total=$y\\n";
"""
BP_LINE = 6  # my $x = 5;

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


@pytest.fixture(scope="module")
def perl_script(tmp_path_factory):
    src = tmp_path_factory.mktemp("perlsrc") / "main.pl"
    src.write_text(PERL_SRC)
    return str(src)


def test_registry_detects_pl_as_perl(perl_script):
    assert registry.detect(perl_script) == "perl"


# --- live-session fixtures/helpers, adapted from test_cpp_session.py ------


@pytest.fixture
async def session():
    """(controller, handler) pair with guaranteed teardown."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_perl_profile())
    yield ctrl, handler
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass  # already stopped / adapter already gone


async def _launch(
    ctrl: DebugController,
    handler: ServerEventHandler,
    program: str,
    *,
    stop_on_entry: bool = True,
    breakpoints: list[tuple[str, int]] | None = None,
) -> None:
    """Mirror run_headless: start, wait initialized, configure, and (for
    stop_on_entry) wait for the entry stop + fetch stop info."""
    if breakpoints:
        for source, line in breakpoints:
            ctrl.state.breakpoints.setdefault(source, []).append(
                SourceBreakpoint(line=line)
            )
    await ctrl.start(program=program, stop_on_entry=stop_on_entry)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    if stop_on_entry:
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()


async def _resume_and_wait(ctrl, handler, action_name: str) -> None:
    """Reset the stop latch, run a continue/step action, await the next
    stop, and refresh stop info — the RPC dispatch loop in miniature."""
    handler.reset_for_continue()
    await getattr(ctrl, action_name)()
    assert await handler.wait_for_stop(timeout=WAIT)
    if not ctrl.state.is_terminated:
        await ctrl.fetch_stop_info()


# --- live-session tests ----------------------------------------------------


async def test_launch_entry_stop_breakpoint_variables(session, perl_script):
    ctrl, handler = session
    await _launch(ctrl, handler, perl_script, stop_on_entry=True)
    assert ctrl.state.phase is SessionPhase.STOPPED
    # Entry stop already lands on BP_LINE (`sub add {...}` is a compile-time
    # definition, not a runtime statement, so `my $x = 5;` is the first line
    # perl5db actually stops at). Add the dynamic breakpoint one line further
    # so `continue_` demonstrates a real breakpoint hit rather than the
    # entry-stop coincidence — mirrors test_cpp_session's BP_LINE+1 idiom.
    await ctrl.add_breakpoint(perl_script, BP_LINE + 1)
    await _resume_and_wait(ctrl, handler, "continue_")
    frame = ctrl.state.stack_frames[0]
    assert frame.line == BP_LINE + 1
    result = await ctrl.evaluate("1 + 2")
    assert result == "3"


async def test_step_into_and_out(session, perl_script):
    ctrl, handler = session
    await _launch(ctrl, handler, perl_script, breakpoints=[(perl_script, BP_LINE + 1)])
    await _resume_and_wait(ctrl, handler, "continue_")
    assert ctrl.state.stack_frames[0].line == BP_LINE + 1
    await _resume_and_wait(ctrl, handler, "step_in")
    assert "add" in ctrl.state.stack_frames[0].name
    await _resume_and_wait(ctrl, handler, "step_out")
    assert ctrl.state.stack_frames[0].name != ""


# --- remote-attach test, using the Task 14 REMOTE_PROG fixture ------------


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


async def test_attach_via_tdbremote(remote_debuggee):
    proc, _prog, port = remote_debuggee
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_perl_profile())
    try:
        await ctrl.remote_attach(host="127.0.0.1", port=port)
        await asyncio.wait_for(handler.initialized_event.wait(), DAP_INITIALIZED)
        await ctrl.do_configure()
        assert await handler.wait_for_stop(timeout=DAP_STOP_ON_ENTRY)
        await ctrl.fetch_stop_info()

        result = await ctrl.evaluate("$counter")
        assert result == "10"

        handler.reset_for_continue()
        await ctrl.continue_()
        await asyncio.wait_for(handler.terminated_event.wait(), WAIT)
        out, _err = proc.communicate(timeout=15)
        assert "counter=31" in out
    finally:
        try:
            await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
        except Exception:
            pass
