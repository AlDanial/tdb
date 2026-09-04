"""End-to-end through DebugController: entry stop, stack, variables,
fatal-error modal data, and pause. Modeled on test_ruby_pause_frame.py
and test_go_session.py (real handler/state API, not DAP-level scripting).
"""

from __future__ import annotations

import asyncio

import pytest

from tdb.languages.errors import parse_powershell_error
from tdb.languages.powershell import build_powershell_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

from tests.integration.powershell_adapter_harness import FIXTURES, pwsh_ok

pytestmark = pytest.mark.skipif(not pwsh_ok(), reason="needs pwsh + PSES")

WAIT = 30.0
PAUSE_TIMEOUT = 10.0


async def _start(
    program: str, stop_on_entry: bool
) -> tuple[DebugController, ServerEventHandler]:
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_powershell_profile())
    await ctrl.start(program=program, stop_on_entry=stop_on_entry)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    return ctrl, handler


async def _stop(ctrl: DebugController) -> None:
    try:
        await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
    except Exception:
        pass  # already stopped / adapter already gone


async def _wait_exit(handler: ServerEventHandler) -> int:
    """Wait for `terminated`, then for the (independent) `exited` event
    that carries the exit code."""
    await asyncio.wait_for(handler.terminated_event.wait(), WAIT)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while handler.exit_code is None and loop.time() < deadline:
        await asyncio.sleep(0.05)
    assert handler.exit_code is not None, "no `exited` event after `terminated`"
    return handler.exit_code


async def _wait_output(handler: ServerEventHandler, needle: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT
    while needle not in handler.peek_output():
        assert loop.time() < deadline, f"{needle!r} never appeared in program output"
        await asyncio.sleep(0.05)


async def test_entry_stop_stack_and_variables():
    program = str(FIXTURES / "functions.ps1")
    ctrl, handler = await _start(program, True)
    try:
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()
        # PSES parks the entry stop on the first executable *statement*
        # of functions.ps1 -- line 9 (`$x = 1`), past the two function
        # definitions (spec addendum 2).
        assert ctrl.state.stop_reason == "entry"
        frames = ctrl.state.stack_frames
        assert frames, "no stack at the entry stop"
        assert frames[0].line == 9
        assert frames[0].source is not None
        assert frames[0].source.path == program

        # Continue into a breakpoint inside Add() and read its locals.
        handler.reset_for_continue()
        await ctrl.add_breakpoint(program, 3)
        await ctrl.continue_()
        assert await handler.wait_for_stop(timeout=WAIT)
        await ctrl.fetch_stop_info()
        frames = ctrl.state.stack_frames
        assert frames[0].line == 3
        byname = {
            v.name: v.value
            for variables in ctrl.state.variables.values()
            for v in variables
        }
        assert byname.get("$a") == "1"
        assert byname.get("$b") == "2"
        assert byname.get("$s") == "3"
        assert "3" in await ctrl.evaluate("$a + $b")
    finally:
        await _stop(ctrl)


async def test_fatal_error_yields_modal_data():
    program = str(FIXTURES / "throws.ps1")
    ctrl, handler = await _start(program, False)
    try:
        exit_code = await _wait_exit(handler)
        output = handler.drain_output()
        assert exit_code == 1
        err = parse_powershell_error(output, exit_code)
        assert err is not None
        assert err.frames[0].path == program
        assert err.frames[0].line == 1
        assert "kaboom" in err.message
    finally:
        await _stop(ctrl)


async def test_write_error_yields_no_modal():
    ctrl, handler = await _start(str(FIXTURES / "writes_error.ps1"), False)
    try:
        exit_code = await _wait_exit(handler)
        output = handler.drain_output()
        assert exit_code == 0
        assert "still here" in output
        # `Write-Error` renders the same ConciseView block a fatal error
        # does (and PSES tags it stderr), so "no stderr" would be wrong:
        # the exit code is what keeps the modal shut.
        assert parse_powershell_error(output, exit_code) is None
    finally:
        await _stop(ctrl)


async def test_pause_while_running_then_evaluate():
    ctrl, handler = await _start(str(FIXTURES / "loop.ps1"), False)
    try:
        # loop.ps1 prints "looping" before spinning: a pause sent before
        # the script is actually running is dropped by PSES.
        await _wait_output(handler, "looping")
        assert await ctrl.pause(timeout=PAUSE_TIMEOUT) is True
        await ctrl.fetch_stop_info()
        assert ctrl.state.stop_reason == "pause"
        assert ctrl.state.stack_frames, "no stack after pause"
        result = await ctrl.evaluate("$i")
        assert int(result.strip()) >= 1
    finally:
        await _stop(ctrl)
