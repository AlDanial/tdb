"""Integration tests: DAPClient + DebugController against real debugpy.

Unlike the RPC integration tests (which spawn a full `tdb --headless`
process), these run the client/controller **in-process** and let them
spawn a real debugpy adapter and debug real Python subprocesses. This
covers the paths the unit fakes can't reach: `DAPClient.start()` over
stdio pipes, the adapter-death watcher against a genuinely dead
process, the full launch/configure handshake, real stepping and
breakpoint hits, pause of a running program, remote attach with the
pre-armed pause, and detach-versus-terminate on quit.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time

import pytest

from tdb.dap.client import DAPClient
from tdb.dap.types import SourceBreakpoint
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase

WAIT = 20.0  # generous ceiling for adapter spawn + debuggee start

SIMPLE_SRC = """\
def add(a, b):
    result = a + b
    return result


def main():
    x = 5
    y = 7
    total = add(x, y)
    print(f"total={total}")


main()
"""
BP_LINE = 9  # total = add(x, y)

LOOP_SRC = """\
import time

i = 0
while True:
    i += 1
    time.sleep(0.01)
"""


@pytest.fixture
def simple_script(tmp_path) -> str:
    path = tmp_path / "simple.py"
    path.write_text(SIMPLE_SRC)
    return str(path)


@pytest.fixture
def loop_script(tmp_path) -> str:
    path = tmp_path / "loop.py"
    path.write_text(LOOP_SRC)
    return str(path)


@pytest.fixture
async def session():
    """(controller, handler) pair with guaranteed teardown."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler)
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
    breakpoints: list[int] | None = None,
) -> None:
    """Mirror run_headless: start, wait initialized, configure, and (for
    stop_on_entry) wait for the entry stop + fetch stop info."""
    if breakpoints:
        ctrl.state.breakpoints[program] = [
            SourceBreakpoint(line=ln) for ln in breakpoints
        ]
    await ctrl.start(program=program, stop_on_entry=stop_on_entry)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    if stop_on_entry:
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()


async def _resume_and_wait(ctrl, handler, action) -> None:
    """Reset the stop latch, run a continue/step action, await the next
    stop, and refresh stop info — the RPC dispatch loop in miniature."""
    handler.reset_for_continue()
    await action()
    assert await handler.wait_for_stop(timeout=WAIT)
    if not ctrl.state.is_terminated:
        await ctrl.fetch_stop_info()


def _location(ctrl) -> tuple[str, int]:
    frame = ctrl.state.stack_frames[0]
    return (frame.source.path if frame.source else "?", frame.line)


# --- DAPClient against a real adapter ----------------------------------


async def test_client_start_initialize_stop():
    """`start()` spawns a live debugpy adapter over stdio and the
    initialize handshake yields real capabilities."""
    client = DAPClient()
    await client.start()
    try:
        caps = await client.initialize()
        assert caps.supports_configuration_done_request is True
        assert caps.supports_conditional_breakpoints is True
    finally:
        await client.stop()
    assert client._process.returncode is not None  # adapter really gone


async def test_client_adapter_death_fails_pending_requests():
    """Kill the real adapter out from under the client: the death
    watcher must fail pending futures with ConnectionError instead of
    leaving callers hanging."""
    client = DAPClient()
    await client.start()
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    client._pending[999] = fut
    client._process.kill()
    with pytest.raises(ConnectionError, match="adapter died"):
        await asyncio.wait_for(fut, WAIT)
    await client.stop()  # must cope with the already-dead process


# --- Controller: launch lifecycle ---------------------------------------


async def test_launch_stop_on_entry_lands_in_program(session, simple_script):
    ctrl, handler = session
    await _launch(ctrl, handler, simple_script)
    assert ctrl.state.phase is SessionPhase.STOPPED
    path, line = _location(ctrl)
    assert path == simple_script and line == 1
    # Stop info is fully populated from the live session.
    assert ctrl.state.threads
    assert ctrl.state.scopes
    assert ctrl.state.variables


async def test_breakpoint_hit_and_evaluate(session, simple_script):
    ctrl, handler = session
    await _launch(ctrl, handler, simple_script, breakpoints=[BP_LINE])
    await _resume_and_wait(ctrl, handler, ctrl.continue_)
    assert _location(ctrl) == (simple_script, BP_LINE)
    assert ctrl.state.stop_reason == "breakpoint"
    assert await ctrl.evaluate("x") == "5"
    assert await ctrl.evaluate("x + y") == "12"


async def test_step_over_and_step_in(session, simple_script):
    ctrl, handler = session
    await _launch(ctrl, handler, simple_script, breakpoints=[BP_LINE])
    await _resume_and_wait(ctrl, handler, ctrl.continue_)
    # Step INTO add(): lands on its first executable line.
    await _resume_and_wait(ctrl, handler, ctrl.step_in)
    assert ctrl.state.stack_frames[0].name == "add"
    # Step over `result = a + b` and check the assignment really ran.
    await _resume_and_wait(ctrl, handler, ctrl.step_over)
    assert await ctrl.evaluate("result") == "12"
    # Step OUT back into main().
    await _resume_and_wait(ctrl, handler, ctrl.step_out)
    assert ctrl.state.stack_frames[0].name == "main"


async def test_add_breakpoint_mid_session(session, simple_script):
    """Breakpoints set while stopped (not just preloaded) reach the
    live adapter and fire."""
    ctrl, handler = session
    await _launch(ctrl, handler, simple_script)
    await ctrl.add_breakpoint(simple_script, BP_LINE)
    await _resume_and_wait(ctrl, handler, ctrl.continue_)
    assert _location(ctrl) == (simple_script, BP_LINE)


async def test_conditional_breakpoint_respected(session, tmp_path):
    src = tmp_path / "cond.py"
    src.write_text("for i in range(10):\n    x = i * 2\nprint(x)\n")
    ctrl, handler = session
    await _launch(ctrl, handler, str(src))
    await ctrl.add_breakpoint(str(src), 2, condition="i == 7")
    await _resume_and_wait(ctrl, handler, ctrl.continue_)
    assert await ctrl.evaluate("i") == "7"  # skipped iterations 0-6


async def test_run_to_completion_captures_output(session, simple_script):
    ctrl, handler = session
    await _launch(ctrl, handler, simple_script)
    handler.reset_for_continue()
    await ctrl.continue_()
    await asyncio.wait_for(handler.terminated_event.wait(), WAIT)
    assert ctrl.state.is_terminated
    assert "total=12" in handler.drain_output()


async def test_pause_interrupts_running_program(session, loop_script):
    ctrl, handler = session
    await _launch(ctrl, handler, loop_script)
    handler.reset_for_continue()
    await ctrl.continue_()  # loop runs forever
    await asyncio.sleep(0.3)
    assert await ctrl.pause(timeout=WAIT) is True
    await ctrl.fetch_stop_info()
    assert ctrl.state.phase is SessionPhase.STOPPED
    assert int(await ctrl.evaluate("i")) > 0  # genuinely mid-loop


async def test_stop_terminates_launched_debuggee(session, loop_script):
    ctrl, handler = session
    await _launch(ctrl, handler, loop_script)
    await asyncio.wait_for(ctrl.stop(), WAIT)
    assert ctrl.state.phase is SessionPhase.TERMINATED


# --- Remote attach --------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


REMOTE_SRC = """\
import pathlib
import time

import debugpy

debugpy.listen(("127.0.0.1", {port}))
pathlib.Path({sentinel!r}).touch()
i = 0
while True:
    i += 1
    time.sleep(0.02)
"""


@pytest.fixture
def remote_debuggee(tmp_path):
    """A real subprocess running debugpy.listen(); yields (proc, port).

    Readiness is signaled via a sentinel file written after listen()
    returns. Do NOT probe the port with a throwaway connection —
    debugpy's listener services a single client, and a probe consumes
    it, so the real attach afterwards gets connection-refused.
    """
    port = _free_port()
    sentinel = tmp_path / "listening"
    script = tmp_path / "remote.py"
    script.write_text(REMOTE_SRC.format(port=port, sentinel=str(sentinel)))
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _, err = proc.communicate(timeout=2)
            pytest.fail(f"remote debuggee died: {err.decode(errors='replace')}")
        if sentinel.exists():
            break
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("debugpy.listen never came up")
    yield proc, port
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)


async def test_remote_attach_prearmed_pause_stops_debuggee(session, remote_debuggee):
    """Attach to a live debugpy server: the pause pre-armed before
    configurationDone must deterministically produce the first stop
    (debugpy ignores stopOnEntry for attach)."""
    ctrl, handler = session
    proc, port = remote_debuggee
    await ctrl.remote_attach("127.0.0.1", port)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    assert await handler.wait_for_stop(timeout=WAIT)
    await ctrl.fetch_stop_info()
    assert ctrl.state.phase is SessionPhase.STOPPED
    assert ctrl.is_remote_attach is True
    assert ctrl.supports_restart is False
    assert int(await ctrl.evaluate("i")) >= 0


async def test_remote_quit_detaches_without_killing_debuggee(session, remote_debuggee):
    """Quitting tdb in remote-attach mode must leave the user's program
    running (the tdb.breakpoint() contract)."""
    ctrl, handler = session
    proc, port = remote_debuggee
    await ctrl.remote_attach("127.0.0.1", port)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    assert await handler.wait_for_stop(timeout=WAIT)
    await asyncio.wait_for(ctrl.stop(), WAIT)
    await asyncio.sleep(0.5)  # give a would-be termination time to land
    assert proc.poll() is None  # still alive: detached, not terminated
