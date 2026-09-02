"""End-to-end: real dlv (Delve) DAP server debugging real Go programs.

Mirrors the harness idioms of test_cpp_session.py (controller +
ServerEventHandler, breakpoints seeded into controller.state before
start()) and test_ocaml_native_session.py's stop_on_entry=False style
(launch running, let the seeded breakpoint produce the first stop).
Skipped wholesale when the `go` toolchain or `dlv` is missing.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.go_concurrency.collector import GoConcurrencyCollector
from tdb.go_concurrency.models import GoFindingKind, GoroutineState
from tdb.languages import registry
from tdb.languages.errors import parse_go_error
from tdb.languages.go import build_go_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController

pytestmark = pytest.mark.skipif(
    shutil.which("go") is None or shutil.which("dlv") is None,
    reason="go toolchain or dlv not installed",
)

WAIT = 20.0  # generous ceiling for adapter spawn + debuggee start

FIXTURES = Path(__file__).parent / "fixtures"
GO_SIMPLE_SRC = FIXTURES / "go_simple" / "main.go"
GO_BLOCKED_SRC = FIXTURES / "go_blocked" / "main.go"
GO_TESTMODE_DIR = FIXTURES / "go_testmode"
GO_TESTMODE_TEST_SRC = GO_TESTMODE_DIR / "mathy_test.go"

BP_LINE_SIMPLE = 7  # `return result` in go_simple/main.go
BP_LINE_BLOCKED = 30  # `fmt.Println("marker =", marker)` in go_blocked/main.go
BP_LINE_TESTMODE = 6  # `got := Double(21)` in go_testmode/mathy_test.go


# --- module fixtures: build fixtures into tmp_path_factory dirs, never
# --- into the repo -----------------------------------------------------


@pytest.fixture(scope="module")
def go_simple_binary(tmp_path_factory):
    d = tmp_path_factory.mktemp("go_simple_bin")
    binary = d / "go_simple"
    subprocess.run(
        ["go", "build", "-gcflags=all=-N -l", "-o", str(binary), str(GO_SIMPLE_SRC)],
        check=True,
    )
    return str(binary)


def test_registry_detects_built_go_binary(go_simple_binary):
    assert registry.detect(go_simple_binary) == "go"


# --- live-session fixtures/helpers, adapted from test_cpp_session.py and
# --- test_ocaml_native_session.py --------------------------------------


@pytest.fixture
async def session():
    """(controller, handler) pair with guaranteed teardown."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_go_profile())
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
    breakpoints: list[tuple[str, int]] | None = None,
) -> None:
    """Launch running (stop_on_entry=False) and wait for the seeded
    breakpoint to produce the first stop."""
    if breakpoints:
        for source, line in breakpoints:
            ctrl.state.breakpoints.setdefault(source, []).append(
                SourceBreakpoint(line=line)
            )
    await ctrl.start(program=program, stop_on_entry=False)
    await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
    await ctrl.do_configure()
    assert await handler.wait_for_stop(timeout=WAIT)
    await ctrl.fetch_stop_info()


async def _resume_and_wait(ctrl, handler, action_name: str) -> None:
    handler.reset_for_continue()
    await getattr(ctrl, action_name)()
    assert await handler.wait_for_stop(timeout=WAIT)
    if not ctrl.state.is_terminated:
        await ctrl.fetch_stop_info()


async def _continue_to_exit(ctrl, handler) -> None:
    handler.reset_for_continue()
    await ctrl.continue_()
    await asyncio.wait_for(handler.terminated_event.wait(), WAIT)


# --- live-session tests -------------------------------------------------


async def test_debug_mode_breakpoint_and_evaluate(session):
    # NOTE: dlv's breakpoint on `return result` stops *after* the
    # preceding `result := a + b` has already executed and materialized
    # in scope (unlike the plan's assumption that a step would be
    # needed first) — `result` is readable immediately. The step here
    # instead exercises step_over crossing the function return, and
    # `y` (the call's assignee back in `main`) is what becomes visible
    # only afterwards.
    ctrl, handler = session
    src = str(GO_SIMPLE_SRC)
    await _launch(ctrl, handler, src, breakpoints=[(src, BP_LINE_SIMPLE)])
    frame = ctrl.state.stack_frames[0]
    assert "add" in frame.name
    result = await ctrl.evaluate("a + b")
    assert "12" in result
    result_var = await ctrl.evaluate("result")
    assert "12" in result_var
    # Two step_overs: the first lands back in `main` right after the
    # call returns (line 12, `y` not yet live in dlv's scope info); the
    # second reaches line 13 where `y` is visible.
    await _resume_and_wait(ctrl, handler, "step_over")
    assert "main" in ctrl.state.stack_frames[0].name
    await _resume_and_wait(ctrl, handler, "step_over")
    y_var = await ctrl.evaluate("y")
    assert "12" in y_var
    await _continue_to_exit(ctrl, handler)
    assert "total = 12" in handler.drain_output()


async def test_exec_mode_prebuilt_binary(session, go_simple_binary):
    ctrl, handler = session
    src = str(GO_SIMPLE_SRC)
    await _launch(ctrl, handler, go_simple_binary, breakpoints=[(src, BP_LINE_SIMPLE)])
    frame = ctrl.state.stack_frames[0]
    assert "add" in frame.name
    result = await ctrl.evaluate("a + b")
    assert "12" in result
    await _continue_to_exit(ctrl, handler)
    assert "total = 12" in handler.drain_output()


async def test_test_mode_runs_test_binary():
    handler = ServerEventHandler()
    ctrl = DebugController(
        handler, profile=build_go_profile(program=str(GO_TESTMODE_DIR), test=True)
    )
    try:
        await _launch(
            ctrl,
            handler,
            str(GO_TESTMODE_DIR),
            breakpoints=[(str(GO_TESTMODE_TEST_SRC), BP_LINE_TESTMODE)],
        )
        frame = ctrl.state.stack_frames[0]
        assert "TestDouble" in frame.name
        await _continue_to_exit(ctrl, handler)
    finally:
        try:
            await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
        except Exception:
            pass


async def test_goroutine_snapshot_states_and_findings(session):
    ctrl, handler = session
    src = str(GO_BLOCKED_SRC)
    await _launch(ctrl, handler, src, breakpoints=[(src, BP_LINE_BLOCKED)])

    snapshot = await GoConcurrencyCollector().collect(ctrl)

    assert len(snapshot.goroutines) >= 5

    recv_waiters = [
        g for g in snapshot.goroutines if g.state is GoroutineState.CHAN_RECV
    ]
    assert len(recv_waiters) >= 3
    resource_ids = {g.resource_id for g in recv_waiters}
    assert None not in resource_ids
    assert len(resource_ids) == 1

    mutex_waiters = [
        g for g in snapshot.goroutines if g.state is GoroutineState.MUTEX_WAIT
    ]
    assert len(mutex_waiters) >= 1

    recv_tids = {g.thread_id for g in recv_waiters}
    stuck_channel_findings = [
        f for f in snapshot.findings if f.kind is GoFindingKind.STUCK_CHANNEL
    ]
    assert any(recv_tids.issubset(set(f.thread_ids)) for f in stuck_channel_findings)

    # Let teardown kill the debuggee rather than waiting out its 10s sleep.
    await ctrl.stop()


async def test_panic_parsed_into_error(tmp_path):
    boom_src = tmp_path / "boom.go"
    boom_src.write_text(
        "package main\n\n"
        "func divide(a, b int) int {\n"
        "\treturn a / b\n"
        "}\n\n"
        "func main() {\n"
        "\tzero := 0\n"
        "\t_ = divide(1, zero)\n"
        "}\n"
    )

    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_go_profile())
    try:
        await ctrl.start(program=str(boom_src), stop_on_entry=False)
        await asyncio.wait_for(handler.initialized_event.wait(), WAIT)
        await ctrl.do_configure()
        # dlv's "Unrecovered Panics" exception breakpoint (advertised
        # default-on, picked up by the base pick_exception_filters) stops
        # the process at the panic site before it unwinds — the program
        # hasn't terminated yet, mirroring every other DAP adapter's
        # unhandled-exception behavior. Continue once more to let the
        # runtime finish printing "panic: ..." and the goroutine dump,
        # then exit.
        assert await handler.wait_for_stop(timeout=WAIT)
        handler.reset_for_continue()
        await ctrl.continue_()
        await asyncio.wait_for(handler.terminated_event.wait(), WAIT)
        stderr_text = handler.drain_output()
    finally:
        try:
            await asyncio.wait_for(ctrl.stop(), timeout=WAIT)
        except Exception:
            pass

    parsed = parse_go_error(stderr_text)
    assert parsed is not None
    assert any("divide" in f.func for f in parsed.frames)
