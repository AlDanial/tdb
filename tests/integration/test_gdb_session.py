"""End-to-end: real `gdb -i dap` debugging a real compiled C++ binary.

Mirrors tests/integration/test_cpp_session.py (Task 15) but selects the
gdb adapter via build_cpp_profile(adapter="gdb"). Skipped wholesale when
gdb < 14 (its DAP mode) or a C++ compiler is missing.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess

import pytest

from tdb.dap.types import SourceBreakpoint
from tdb.languages.cpp import build_cpp_profile
from tdb.server.event_handler import ServerEventHandler
from tdb.session.controller import DebugController


def _gdb_supports_dap() -> bool:
    gdb = shutil.which("gdb")
    if gdb is None:
        return False
    out = subprocess.run([gdb, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.\d+", out)
    return bool(m) and int(m.group(1)) >= 14


pytestmark = pytest.mark.skipif(
    not _gdb_supports_dap()
    or (shutil.which("g++") is None and shutil.which("clang++") is None),
    reason="gdb >= 14 or C++ compiler not installed",
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


# --- live-session fixtures/helpers, copied+adapted from
# --- test_cpp_session.py: controller built with profile=build_cpp_profile(
# --- adapter="gdb"), _launch() calls ctrl.start(program=binary, ...) with
# --- no python= kwarg.


@pytest.fixture
async def session():
    """(controller, handler) pair with guaranteed teardown."""
    handler = ServerEventHandler()
    ctrl = DebugController(handler, profile=build_cpp_profile(adapter="gdb"))
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


async def test_breakpoint_hit_and_evaluate(session, cpp_binary):
    binary, src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, breakpoints=[(src, BP_LINE + 1)])
    await _resume_and_wait(ctrl, handler, "continue_")
    # stopped at `int y = add(x, 7);` — x is already assigned
    frame = ctrl.state.stack_frames[0]
    assert frame.line == BP_LINE + 1
    # gdb's DAP evaluate with context="repl" is a raw CLI passthrough (no
    # implicit print, unlike lldb's REPL) — a bare "x" is parsed as gdb's
    # own `x` (examine memory) command and fails with "Argument required
    # (starting display address)." rather than evaluating the C variable.
    # `print <expr>` sidesteps this since "print" is the explicit command
    # and its argument is parsed as an expression. Confirmed via manual
    # DAP probe against gdb -i dap 17.1.
    result = await ctrl.evaluate("print x")
    # gdb's evaluate renders ints as "$1 = 5\n" -> containment assert.
    assert "5" in result


async def test_run_to_completion_captures_output(session, cpp_binary):
    binary, _src = cpp_binary
    ctrl, handler = session
    await _launch(ctrl, handler, binary, stop_on_entry=True)
    await _continue_to_exit(ctrl, handler)
    assert "total=12" in handler.drain_output()
