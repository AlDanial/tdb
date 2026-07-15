"""End-to-end: real lldb-dap debugging a real compiled C++ binary.

Skipped wholesale when lldb-dap or a C++ compiler is missing, so CI
without LLVM still passes.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from tdb.languages import registry
from tdb.languages.cpp import build_cpp_profile
from tdb.dap.types import SourceBreakpoint
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController
from tdb.session.state import SessionPhase

pytestmark = pytest.mark.skipif(
    shutil.which("lldb-dap") is None
    or (shutil.which("g++") is None and shutil.which("clang++") is None),
    reason="lldb-dap or C++ compiler not installed",
)

WAIT = 20.0  # generous ceiling for adapter spawn + debuggee start

CPP_SRC = """\
#include <cstdio>

int add(int a, int b) {
    int result = a + b;
    return result;
}

int main() {
    int x = 5;
    int y = add(x, 7);
    printf("total=%d\\n", y);
    return 0;
}
"""
BP_LINE = 9  # int x = 5;


@pytest.fixture(scope="module")
def cpp_binary(tmp_path_factory):
    src = tmp_path_factory.mktemp("cppsrc") / "main.cpp"
    src.write_text(CPP_SRC)
    binary = src.parent / "main"
    cxx = shutil.which("g++") or shutil.which("clang++")
    subprocess.run([cxx, "-g", "-O0", "-o", str(binary), str(src)], check=True)
    return str(binary), str(src)


def test_registry_detects_compiled_binary_as_cpp(cpp_binary):
    binary, _src = cpp_binary
    assert registry.detect(binary) == "cpp"


# --- live-session fixtures/helpers, copied+adapted from
# --- test_dap_session.py: controller built with profile=build_cpp_profile(),
# --- _launch() calls ctrl.start(program=binary, ...) with no python= kwarg.


@pytest.fixture
async def session():
    """(controller, handler) pair with guaranteed teardown."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_cpp_profile())
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
    stop_on_entry) wait for the entry stop + fetch stop info.

    Unlike the debugpy suite, breakpoints here are given as (source, line)
    pairs since cpp tests set breakpoints in a source file distinct from
    the launched binary.
    """
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


async def _continue_to_exit(ctrl, handler) -> None:
    handler.reset_for_continue()
    await ctrl.continue_()
    await asyncio.wait_for(handler.terminated_event.wait(), WAIT)


# --- live-session tests ---------------------------------------------------


async def test_breakpoint_hit_and_locals(session, cpp_binary):
    binary, src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE + 1)])
    await _resume_and_wait(ctrl, handler, "continue_")
    # stopped at `int y = add(x, 7);` — x is already assigned
    frame = ctrl.state.stack_frames[0]
    assert frame.line == BP_LINE + 1
    result = await ctrl.evaluate("x")
    assert "5" in result


async def test_step_into_and_out(session, cpp_binary):
    binary, src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE + 1)])
    await _resume_and_wait(ctrl, handler, "continue_")
    await _resume_and_wait(ctrl, handler, "step_in")
    assert ctrl.state.stack_frames[0].name.startswith("add")
    await _resume_and_wait(ctrl, handler, "step_out")
    assert ctrl.state.stack_frames[0].name.startswith("main")


async def test_run_to_completion_captures_output(session, cpp_binary):
    binary, _src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, stop_on_entry=True)
    await _continue_to_exit(ctrl, handler)
    assert "total=12" in handler.drain_output()


async def test_stop_terminates_debuggee(session, cpp_binary):
    binary, src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE)])
    await ctrl.stop()  # must not hang or raise
    assert ctrl.state.phase is SessionPhase.TERMINATED
